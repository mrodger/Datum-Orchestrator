"""Report ingestion — extract findings from drone output, geocode, embed, store.

Every extracted fact gets full provenance:
  run_id → orchestration_runs.id (which session produced this)
  drone_task_id → original drone task
  extraction_index → position in extraction batch
  raw_excerpt → verbatim source text from drone output
"""

from __future__ import annotations

import json
import os
from uuid import UUID

import httpx

from .db import get_pool
from .models import ExtractedFinding, ExtractionResult

LITELLM_URL = os.environ.get("LITELLM_BASE_URL", "http://host.docker.internal:4000")
LITELLM_KEY = os.environ.get("LITELLM_API_KEY", "drone2026")
ORCH_MODEL = os.environ.get("ORCHESTRATOR_MODEL", "gpt-5.4")

_llm_client: httpx.AsyncClient | None = None


def _get_llm_client() -> httpx.AsyncClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = httpx.AsyncClient(
            base_url=LITELLM_URL,
            headers={"Authorization": f"Bearer {LITELLM_KEY}"},
            timeout=60.0,
        )
    return _llm_client


# ── Extraction ───────────────────────────────────────────────────

EXTRACTION_PROMPT = """\
You are a structured data extractor. Given a drone research report, extract
every distinct finding as a JSON array.

For each finding, produce:
- "content": the finding in one clear sentence
- "location_text": geographic location mentioned (null if none)
- "lat": latitude if determinable (null otherwise)
- "lon": longitude if determinable (null otherwise)
- "confidence": 0.0-1.0 how confident the finding is (based on source quality, hedging language)
- "category": topic tag (e.g. "environmental", "infrastructure", "species", "hazard", "economic")
- "raw_excerpt": the verbatim sentence(s) from the report this finding came from

Rules:
- One finding per distinct factual claim. Split compound claims.
- If a finding has no geographic anchor, set location fields to null.
- Preserve the original meaning. Do not infer beyond what the text states.
- Return ONLY valid JSON: {"findings": [...], "extraction_notes": "..."}
"""


async def extract_findings(drone_output: str) -> ExtractionResult:
    """Use GPT-5.4 to extract structured findings from raw drone output."""
    client = _get_llm_client()

    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": ORCH_MODEL,
            "messages": [
                {"role": "system", "content": EXTRACTION_PROMPT},
                {"role": "user", "content": drone_output},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": 4096,
        },
    )
    resp.raise_for_status()
    data = resp.json()

    content = data["choices"][0]["message"]["content"]
    parsed = json.loads(content)

    findings = [ExtractedFinding(**f) for f in parsed.get("findings", [])]
    return ExtractionResult(
        findings=findings,
        extraction_notes=parsed.get("extraction_notes"),
    )


# ── Embedding ────────────────────────────────────────────────────

async def embed_text(text: str) -> list[float]:
    """Get embedding vector via LiteLLM (OpenAI-compatible endpoint)."""
    client = _get_llm_client()
    resp = await client.post(
        "/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": text},
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


# ── Contradiction Detection ──────────────────────────────────────

async def detect_contradictions(
    pool, finding: ExtractedFinding, finding_id: UUID, radius_m: float = 2000.0
) -> int:
    """Check if a new finding contradicts existing active facts nearby.

    Uses LLM to compare semantically similar facts at the same location.
    Returns number of facts invalidated.
    """
    if finding.lat is None or finding.lon is None:
        return 0

    # Get candidate facts: same area, still active
    candidates = await pool.fetch(
        """
        SELECT id, content FROM knowledge_facts
        WHERE invalid_at IS NULL
          AND geom IS NOT NULL
          AND id != $1
          AND ST_DWithin(
              geom::geography,
              ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
              $4
          )
        ORDER BY ST_Distance(
            geom::geography,
            ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography
        )
        LIMIT 20
        """,
        finding_id, finding.lon, finding.lat, radius_m,
    )

    if not candidates:
        return 0

    # Ask LLM which (if any) are contradicted by the new finding
    candidate_list = "\n".join(
        f"[{r['id']}] {r['content']}" for r in candidates
    )
    prompt = f"""Given this NEW finding:
"{finding.content}"

Which of these EXISTING facts does it contradict or supersede?
A contradiction means the new finding makes the old fact no longer true.
Minor updates or additions are NOT contradictions.

Existing facts:
{candidate_list}

Return JSON: {{"contradicted": ["id1", "id2", ...], "reasons": {{"id1": "reason", ...}}}}
If none are contradicted, return {{"contradicted": [], "reasons": {{}}}}"""

    client = _get_llm_client()
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": ORCH_MODEL,
            "messages": [
                {"role": "system", "content": "You detect factual contradictions. Be conservative — only flag true contradictions, not refinements or additions."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": 1024,
        },
    )
    resp.raise_for_status()
    result = json.loads(resp.json()["choices"][0]["message"]["content"])

    contradicted_ids = result.get("contradicted", [])
    reasons = result.get("reasons", {})
    invalidated = 0

    for cid in contradicted_ids:
        try:
            await pool.execute(
                """
                UPDATE knowledge_facts
                SET invalid_at = NOW(),
                    invalidated_by = $1,
                    invalidation_reason = $2
                WHERE id = $3::uuid AND invalid_at IS NULL
                """,
                finding_id,
                reasons.get(cid, "Contradicted by newer finding"),
                cid,
            )
            invalidated += 1
        except Exception:
            pass  # invalid UUID or already invalidated

    return invalidated


# ── Full Ingestion Pipeline ──────────────────────────────────────

async def ingest_report(
    run_id: UUID,
    drone_task_id: str,
    drone_output: str,
) -> tuple[int, int]:
    """Full ingestion: extract → geocode → embed → store → detect contradictions.

    Returns (facts_extracted, contradictions_found).
    """
    pool = await get_pool()

    # 1. Extract findings
    extraction = await extract_findings(drone_output)
    total_contradictions = 0

    # 2. Process each finding
    for idx, finding in enumerate(extraction.findings):
        # Embed
        embedding = await embed_text(finding.content)

        # Build geometry
        geom_sql = None
        if finding.lat is not None and finding.lon is not None:
            geom_sql = f"ST_SetSRID(ST_MakePoint({finding.lon}, {finding.lat}), 4326)"

        # Insert fact with full provenance
        if geom_sql:
            row = await pool.fetchrow(
                f"""
                INSERT INTO knowledge_facts (
                    run_id, drone_task_id, extraction_index,
                    content, category, raw_excerpt,
                    geom, location_text, geocode_confidence,
                    embedding, confidence, source_type
                ) VALUES (
                    $1, $2, $3,
                    $4, $5, $6,
                    {geom_sql}, $7, $8,
                    $9, $10, 'drone_report'
                )
                RETURNING id
                """,
                run_id, drone_task_id, idx,
                finding.content, finding.category, finding.raw_excerpt,
                finding.location_text, finding.confidence if finding.lat else None,
                embedding, finding.confidence,
            )
        else:
            row = await pool.fetchrow(
                """
                INSERT INTO knowledge_facts (
                    run_id, drone_task_id, extraction_index,
                    content, category, raw_excerpt,
                    location_text,
                    embedding, confidence, source_type
                ) VALUES (
                    $1, $2, $3,
                    $4, $5, $6,
                    $7,
                    $8, $9, 'drone_report'
                )
                RETURNING id
                """,
                run_id, drone_task_id, idx,
                finding.content, finding.category, finding.raw_excerpt,
                finding.location_text,
                embedding, finding.confidence,
            )

        fact_id = row["id"]

        # 3. Contradiction detection
        invalidated = await detect_contradictions(pool, finding, fact_id)
        total_contradictions += invalidated

    # 4. Update coverage cells for geotagged findings
    await _update_coverage(pool, extraction.findings)

    # 5. Update the orchestration run
    await pool.execute(
        """
        UPDATE orchestration_runs
        SET facts_extracted = $1,
            contradictions_found = $2,
            ingestion_status = 'complete',
            ingested_at = NOW()
        WHERE id = $3
        """,
        len(extraction.findings),
        total_contradictions,
        run_id,
    )

    return len(extraction.findings), total_contradictions


async def _update_coverage(pool, findings: list[ExtractedFinding]):
    """Update coverage cells for geotagged findings.

    Uses a simple 0.1-degree grid (~11km cells at equator).
    """
    for f in findings:
        if f.lat is None or f.lon is None:
            continue

        # Grid cell: round to 0.1 degree
        cell_lat = round(f.lat, 1)
        cell_lon = round(f.lon, 1)
        cell_id = f"{cell_lat}_{cell_lon}"

        # Half-width of cell in degrees
        hw = 0.05

        await pool.execute(
            """
            INSERT INTO coverage_cells (cell_id, geom, fact_count, active_fact_count, last_ingested_at, first_ingested_at)
            VALUES (
                $1,
                ST_MakeEnvelope($2, $3, $4, $5, 4326),
                1, 1, NOW(), NOW()
            )
            ON CONFLICT (cell_id) DO UPDATE SET
                fact_count = coverage_cells.fact_count + 1,
                active_fact_count = coverage_cells.active_fact_count + 1,
                last_ingested_at = NOW(),
                staleness_score = 0.0
            """,
            cell_id,
            cell_lon - hw, cell_lat - hw,
            cell_lon + hw, cell_lat + hw,
        )
