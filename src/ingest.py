"""Report ingestion — extract findings from drone output, embed, store.

Every extracted fact gets full provenance:
  run_id → orchestration_runs.id (which session produced this)
  drone_task_id → original drone task
  extraction_index → position in extraction batch
  raw_excerpt → verbatim source text from drone output
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from uuid import UUID

import asyncpg
import httpx

from .db import get_pool
from .models import ExtractedFinding, ExtractionResult

logger = logging.getLogger(__name__)

LITELLM_URL = os.environ.get("LITELLM_BASE_URL", "http://drone-litellm:4000")
LITELLM_KEY = os.environ.get("LITELLM_API_KEY", "drone2026")
ORCH_MODEL = os.environ.get("ORCHESTRATOR_MODEL", "gpt-5.4")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_DIM = 1536  # text-embedding-3-small
_MAX_RETRIES = 3
_RETRY_DELAYS = [5.0, 15.0, 45.0]  # seconds; jitter added at runtime

_llm_client: httpx.AsyncClient | None = None
_embed_client: httpx.AsyncClient | None = None
_extraction_sem: asyncio.Semaphore | None = None  # limits concurrent LLM extraction calls


def _get_extraction_sem() -> asyncio.Semaphore:
    """Semaphore limiting concurrent fact-extraction LLM calls.

    Prevents rate-limit pile-ups when many ingestions are queued simultaneously.
    Lazily created on first use within the running event loop.
    """
    global _extraction_sem
    if _extraction_sem is None:
        _extraction_sem = asyncio.Semaphore(2)
    return _extraction_sem


def _get_llm_client() -> httpx.AsyncClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = httpx.AsyncClient(
            base_url=LITELLM_URL,
            headers={"Authorization": f"Bearer {LITELLM_KEY}"},
            timeout=120.0,
        )
    return _llm_client


def _get_embed_client() -> httpx.AsyncClient:
    global _embed_client
    if _embed_client is None:
        _embed_client = httpx.AsyncClient(
            base_url="https://api.openai.com",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            timeout=30.0,
        )
    return _embed_client


async def close_clients():
    """Close HTTP clients. Called from server lifespan shutdown."""
    global _llm_client, _embed_client
    if _llm_client:
        await _llm_client.aclose()
        _llm_client = None
    if _embed_client:
        await _embed_client.aclose()
        _embed_client = None


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
    """Use GPT-5.4 to extract structured findings from raw drone output.

    Retries up to _MAX_RETRIES times on 429 with exponential backoff + jitter.
    All other HTTP errors raise immediately.
    """
    client = _get_llm_client()
    # Truncate very long outputs to avoid hitting context limits.
    # 120k chars ≈ 30k tokens — leaves room for the system prompt and JSON response.
    MAX_INPUT_CHARS = 120_000
    if len(drone_output) > MAX_INPUT_CHARS:
        logger.warning(
            "Drone output is %d chars — truncating to %d for extraction",
            len(drone_output), MAX_INPUT_CHARS,
        )
        drone_output = drone_output[:MAX_INPUT_CHARS] + "\n\n[TRUNCATED]"

    payload = {
        "model": ORCH_MODEL,
        "messages": [
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": drone_output},
        ],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": 32768,
    }

    resp = None
    for attempt in range(_MAX_RETRIES):
        resp = await client.post("/v1/chat/completions", json=payload)
        if resp.status_code == 429:
            if attempt == _MAX_RETRIES - 1:
                resp.raise_for_status()
            delay = _RETRY_DELAYS[attempt] + random.uniform(0, 2)
            logger.warning(
                "Rate limited (429) on extraction attempt %d/%d — retrying in %.1fs",
                attempt + 1, _MAX_RETRIES, delay,
            )
            await asyncio.sleep(delay)
            continue
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            # LiteLLM can return 200 with empty choices under load — treat as retriable
            if attempt == _MAX_RETRIES - 1:
                raise ValueError(f"LLM returned empty choices after {_MAX_RETRIES} attempts: {resp.text[:200]}")
            delay = _RETRY_DELAYS[attempt] + random.uniform(0, 2)
            logger.warning(
                "Empty choices on extraction attempt %d/%d — retrying in %.1fs",
                attempt + 1, _MAX_RETRIES, delay,
            )
            await asyncio.sleep(delay)
            continue
        content = choices[0]["message"]["content"]
        parsed = json.loads(content)
        usage = data.get("usage") or {}
        break
    else:
        raise ValueError(f"Extraction failed after {_MAX_RETRIES} attempts")

    findings = [ExtractedFinding(**f) for f in parsed.get("findings", [])]
    return ExtractionResult(
        findings=findings,
        extraction_notes=parsed.get("extraction_notes"),
        tokens_input=usage.get("prompt_tokens"),
        tokens_output=usage.get("completion_tokens"),
    )


# ── Embedding ────────────────────────────────────────────────────

async def embed_text(text: str) -> list[float]:
    """Get embedding vector via OpenAI API directly."""
    client = _get_embed_client()
    resp = await client.post(
        "/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": text},
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


# ── Contradiction Detection ──────────────────────────────────────

async def detect_contradictions(
    conn, finding: ExtractedFinding, finding_id: UUID, radius_m: float = 2000.0
) -> int:
    """Check if a new finding contradicts existing active facts nearby.

    Uses LLM to compare semantically similar facts at the same location.
    Returns number of facts invalidated.
    """
    if finding.lat is None or finding.lon is None:
        return 0

    # Get candidate facts: same area, still active
    candidates = await conn.fetch(
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

    # Only accept IDs that were in the candidate set (LLM may hallucinate others)
    candidate_id_set = {str(r["id"]) for r in candidates}
    contradicted_ids = [cid for cid in result.get("contradicted", []) if cid in candidate_id_set]
    reasons = result.get("reasons", {})
    invalidated = 0

    for cid in contradicted_ids:
        try:
            update_result = await conn.execute(
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
            if update_result == "UPDATE 1":
                invalidated += 1
                # Decrement active_fact_count on affected coverage cell
                await _decrement_coverage_count(conn, cid)
        except (asyncpg.InvalidTextRepresentationError, asyncpg.ForeignKeyViolationError) as e:
            logger.warning("Skipping invalidation for fact %s: %s", cid, e)
        except Exception as e:
            logger.error("Unexpected error invalidating fact %s: %s", cid, e)

    return invalidated


def _cell_id(lat: float, lon: float) -> str:
    """Derive coverage cell ID from coordinates. 0.1-degree grid (~11km at equator)."""
    return f"{round(lat, 1)}_{round(lon, 1)}"


async def _decrement_coverage_count(conn, fact_id: str):
    """Decrement active_fact_count on the coverage cell containing this fact.

    Derives cell_id in Python (using _cell_id) to match insertion-time derivation.
    SQL ROUND uses round-half-up; Python round uses banker's rounding — deriving
    in Python ensures the same cell is targeted.
    """
    try:
        row = await conn.fetchrow(
            "SELECT ST_Y(geom) AS lat, ST_X(geom) AS lon FROM knowledge_facts WHERE id = $1::uuid AND geom IS NOT NULL",
            fact_id,
        )
        if not row:
            return
        cell = _cell_id(row["lat"], row["lon"])
        await conn.execute(
            "UPDATE coverage_cells SET active_fact_count = GREATEST(0, active_fact_count - 1) WHERE cell_id = $1",
            cell,
        )
    except Exception as e:
        logger.warning("Failed to decrement coverage for fact %s: %s", fact_id, e)


# ── Full Ingestion Pipeline ──────────────────────────────────────

async def _claim_ingestion(pool: asyncpg.Pool, run_id: UUID) -> bool:
    """Atomically claim ingestion for this run. Returns True if claimed.

    Prevents duplicate ingestion from poll + callback race.
    """
    result = await pool.fetchval(
        """
        UPDATE orchestration_runs
        SET ingestion_status = 'running'
        WHERE id = $1 AND ingestion_status = 'pending'
        RETURNING id
        """,
        run_id,
    )
    return result is not None


async def ingest_report(
    run_id: UUID,
    drone_task_id: str,
    drone_output: str,
) -> tuple[int, int]:
    """Full ingestion: extract → embed → store → detect contradictions.

    Returns (facts_extracted, contradictions_found).
    Idempotent: only runs if ingestion_status is 'pending'.
    """
    pool = await get_pool()

    # Idempotency guard — first caller wins
    if not await _claim_ingestion(pool, run_id):
        logger.info("Ingestion already claimed for run %s, skipping", run_id)
        row = await pool.fetchrow(
            "SELECT facts_extracted, contradictions_found FROM orchestration_runs WHERE id = $1",
            run_id,
        )
        return (row["facts_extracted"], row["contradictions_found"]) if row else (0, 0)

    # 1. Extract findings (semaphore limits concurrent LLM calls → avoids 429 pile-up)
    extraction = None
    try:
        async with _get_extraction_sem():
            extraction = await extract_findings(drone_output)
        # Persist extraction token usage immediately (best-effort)
        if extraction.tokens_input is not None or extraction.tokens_output is not None:
            await pool.execute(
                "UPDATE orchestration_runs SET extraction_tokens_input = $1, extraction_tokens_output = $2 WHERE id = $3",
                extraction.tokens_input, extraction.tokens_output, run_id,
            )
    except Exception as e:
        logger.error("Extraction failed for run %s: %s (%s)", run_id, e, type(e).__name__, exc_info=True)
        await pool.execute(
            "UPDATE orchestration_runs SET ingestion_status = 'failed', scoring_notes = $1 WHERE id = $2",
            f"Extraction error: {type(e).__name__}: {e}", run_id,
        )
        return 0, 0

    total_contradictions = 0

    # 2. Process each finding inside a transaction
    inserted_findings: list[ExtractedFinding] = []
    for idx, finding in enumerate(extraction.findings):
        # Embed — failures are non-fatal; store fact without vector
        embedding = None
        try:
            embedding = await embed_text(finding.content)
            if len(embedding) != EMBEDDING_DIM:
                logger.warning(
                    "Embedding dimension mismatch: got %d, expected %d — storing without vector",
                    len(embedding), EMBEDDING_DIM,
                )
                embedding = None
        except Exception as e:
            logger.warning("Embedding failed for finding %d: %s — storing without vector", idx, e)

        async with pool.acquire() as conn:
            async with conn.transaction():
                # Insert fact with full provenance
                has_geom = finding.lat is not None and finding.lon is not None
                if has_geom:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO knowledge_facts (
                            run_id, drone_task_id, extraction_index,
                            content, category, raw_excerpt,
                            geom, location_text, geocode_confidence,
                            embedding, confidence, source_type
                        ) VALUES (
                            $1, $2, $3,
                            $4, $5, $6,
                            ST_SetSRID(ST_MakePoint($7, $8), 4326), $9, NULL,
                            $10, $11, 'drone_report'
                        )
                        ON CONFLICT (run_id, extraction_index) DO NOTHING
                        RETURNING id
                        """,
                        run_id, drone_task_id, idx,
                        finding.content, finding.category, finding.raw_excerpt,
                        finding.lon, finding.lat,
                        finding.location_text,
                        embedding, finding.confidence,
                    )
                else:
                    row = await conn.fetchrow(
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
                        ON CONFLICT (run_id, extraction_index) DO NOTHING
                        RETURNING id
                        """,
                        run_id, drone_task_id, idx,
                        finding.content, finding.category, finding.raw_excerpt,
                        finding.location_text,
                        embedding, finding.confidence,
                    )

                if row is None:
                    logger.info("Fact already exists for run %s idx %d, skipping", run_id, idx)
                    continue

                inserted_findings.append(finding)
                fact_id = row["id"]

                # 3. Contradiction detection (same conn = same transaction)
                invalidated = await detect_contradictions(conn, finding, fact_id)
                total_contradictions += invalidated

    # 4. Update coverage cells only for successfully inserted geotagged findings
    try:
        await _update_coverage(pool, inserted_findings)
    except Exception as e:
        logger.warning("Coverage update failed for run %s: %s", run_id, e)

    # 5. Update the orchestration run — always reach a terminal state
    await pool.execute(
        """
        UPDATE orchestration_runs
        SET facts_extracted = $1,
            contradictions_found = $2,
            ingestion_status = 'complete',
            ingested_at = NOW()
        WHERE id = $3
        """,
        len(inserted_findings),
        total_contradictions,
        run_id,
    )

    return len(inserted_findings), total_contradictions


async def _update_coverage(pool, findings: list[ExtractedFinding]):
    """Update coverage cells for geotagged findings.

    Uses a simple 0.1-degree grid (~11km cells at equator).
    """
    for f in findings:
        if f.lat is None or f.lon is None:
            continue

        cell_lat = round(f.lat, 1)
        cell_lon = round(f.lon, 1)
        cell_id = _cell_id(f.lat, f.lon)
        hw = 0.05  # half-width in degrees

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
