"""Core orchestration loop — receive task, enrich, dispatch, ingest."""

from __future__ import annotations

import json
import logging
import os
from uuid import UUID

from .db import get_pool
from .dispatch import dispatch, poll_until_done
from .drift import run_drift_sweep
from .ingest import ingest_report
from .models import DroneDispatchPayload, OrchestrateRequest, RunStatus
from .skills import render_skills_context, select_skills_for_task
from .spatial import build_spatial_context, get_fact_ids_for_context

logger = logging.getLogger(__name__)

CALLBACK_URL = os.environ.get(
    "ORCHESTRATOR_CALLBACK_URL", "http://datum-orchestrator:8000/callback"
)


async def orchestrate(req: OrchestrateRequest, existing_run_id: UUID | None = None) -> RunStatus:
    """Full orchestration pipeline:

    1. Create run record (or reuse existing from async path)
    2. Query PostGIS for spatial context (nearby facts, contradictions, gaps)
    3. Select + inject relevant skills
    4. Dispatch enriched task to drone
    5. Poll for completion
    6. Ingest drone output (extract, embed, contradiction-detect)
    7. Score the dispatch
    8. Run drift sweep
    9. Return final status
    """
    pool = await get_pool()

    # ── 1. Create orchestration run ──────────────────────────────

    lat = req.lat
    lon = req.lon

    if existing_run_id:
        run_id = existing_run_id
        if lat is not None and lon is not None:
            await pool.execute(
                """
                UPDATE orchestration_runs
                SET source_request = $1, task_geom = ST_SetSRID(ST_MakePoint($2, $3), 4326)
                WHERE id = $4
                """,
                json.dumps(req.model_dump()), lon, lat, run_id,
            )
        else:
            await pool.execute(
                "UPDATE orchestration_runs SET source_request = $1 WHERE id = $2",
                json.dumps(req.model_dump()), run_id,
            )
    else:
        if lat is not None and lon is not None:
            run_id = await pool.fetchval(
                """
                INSERT INTO orchestration_runs (
                    source, source_request, task_description, task_instructions,
                    task_geom, drone_model, drone_status, ingestion_status
                ) VALUES (
                    $1, $2, $3, $4,
                    ST_SetSRID(ST_MakePoint($5, $6), 4326), $7, 'pending', 'pending'
                )
                RETURNING id
                """,
                req.source, json.dumps(req.model_dump()),
                req.description, req.instructions,
                lon, lat, req.model,
            )
        else:
            run_id = await pool.fetchval(
                """
                INSERT INTO orchestration_runs (
                    source, source_request, task_description, task_instructions,
                    drone_model, drone_status, ingestion_status
                ) VALUES ($1, $2, $3, $4, $5, 'pending', 'pending')
                RETURNING id
                """,
                req.source, json.dumps(req.model_dump()),
                req.description, req.instructions, req.model,
            )

    # ── 2. Spatial context ────────────────────────────────────────

    spatial_context = None
    fact_ids: list[UUID] = []
    if lat is not None and lon is not None:
        spatial_context = await build_spatial_context(lat, lon)
        fact_ids = await get_fact_ids_for_context(lat, lon, 5000.0)

    # ── 3. Skills ─────────────────────────────────────────────────

    skill_names = select_skills_for_task(req.description)
    skills_text = render_skills_context(skill_names)

    # ── 4. Build context for drone prompt ─────────────────────────

    context_blocks = []
    if req.context:
        context_blocks.append(req.context)
    if spatial_context and spatial_context.context_text != "No prior knowledge for this area.":
        context_blocks.append(spatial_context.context_text)
    if skills_text:
        context_blocks.append(skills_text)

    full_context = "\n\n---\n\n".join(context_blocks) if context_blocks else None

    await pool.execute(
        """
        UPDATE orchestration_runs
        SET context_snapshot = $1,
            skills_used = $2,
            facts_injected = $3
        WHERE id = $4
        """,
        full_context,
        skill_names if skill_names else None,
        [str(fid) for fid in fact_ids] if fact_ids else None,
        run_id,
    )

    # ── 5. Dispatch to drone ──────────────────────────────────────

    payload = DroneDispatchPayload(
        description=req.description,
        instructions=req.instructions,
        context=full_context,
        model=req.model,
        maxTurns=req.max_turns,
        callbackUrl=CALLBACK_URL,
    )

    drone_resp = await dispatch(payload)

    await pool.execute(
        """
        UPDATE orchestration_runs
        SET drone_task_id = $1, drone_status = 'dispatched', dispatched_at = NOW()
        WHERE id = $2
        """,
        drone_resp.taskId,
        run_id,
    )

    # ── 6. Poll for completion ────────────────────────────────────

    result = await poll_until_done(drone_resp.taskId)

    await pool.execute(
        """
        UPDATE orchestration_runs
        SET drone_status = $1, drone_output_raw = $2, drone_completed_at = NOW()
        WHERE id = $3
        """,
        result.status,
        result.output,
        run_id,
    )

    # ── 7. Ingest ─────────────────────────────────────────────────

    facts_count = 0
    contradictions = 0
    if result.status == "complete" and result.output:
        try:
            facts_count, contradictions = await ingest_report(
                run_id, drone_resp.taskId, result.output
            )
        except Exception as e:
            logger.error("Ingestion failed for run %s: %s", run_id, e)
            await pool.execute(
                """
                UPDATE orchestration_runs
                SET ingestion_status = 'failed',
                    scoring_notes = $1
                WHERE id = $2
                """,
                f"Ingestion error: {e}",
                run_id,
            )

    # ── 8. Score ──────────────────────────────────────────────────

    quality = None
    if facts_count > 0:
        quality = min(1.0, facts_count / 20) * max(0.5, 1.0 - contradictions / max(facts_count, 1))
        await pool.execute(
            """
            UPDATE orchestration_runs
            SET outcome_quality = $1, scored_at = NOW()
            WHERE id = $2
            """,
            quality,
            run_id,
        )

        for skill in skill_names:
            await pool.execute(
                """
                INSERT INTO skill_scores (skill_name, run_id, outcome_quality, fact_yield, contradiction_count)
                VALUES ($1, $2, $3, $4, $5)
                """,
                skill, run_id, quality, facts_count, contradictions,
            )

    # ── 9. Drift sweep ────────────────────────────────────────────

    await run_drift_sweep()

    # ── 10. Return (single query instead of 4x fetchval) ──────────

    row = await pool.fetchrow(
        """
        SELECT created_at, dispatched_at, drone_completed_at, ingested_at
        FROM orchestration_runs WHERE id = $1
        """,
        run_id,
    )

    return RunStatus(
        id=run_id,
        task_description=req.description,
        drone_task_id=drone_resp.taskId,
        drone_status=result.status,
        ingestion_status="complete" if facts_count > 0 else "skipped",
        facts_extracted=facts_count,
        contradictions_found=contradictions,
        outcome_quality=quality,
        created_at=row["created_at"],
        dispatched_at=row["dispatched_at"],
        drone_completed_at=row["drone_completed_at"],
        ingested_at=row["ingested_at"],
    )


async def get_run(run_id: UUID) -> RunStatus | None:
    """Fetch a run by ID."""
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM orchestration_runs WHERE id = $1", run_id)
    if not row:
        return None
    return RunStatus(
        id=row["id"],
        task_description=row["task_description"],
        drone_task_id=row["drone_task_id"],
        drone_status=row["drone_status"],
        ingestion_status=row["ingestion_status"],
        facts_extracted=row["facts_extracted"],
        contradictions_found=row["contradictions_found"],
        outcome_quality=row["outcome_quality"],
        created_at=row["created_at"],
        dispatched_at=row["dispatched_at"],
        drone_completed_at=row["drone_completed_at"],
        ingested_at=row["ingested_at"],
    )
