"""Core orchestration loop — receive task, enrich, dispatch, ingest."""

from __future__ import annotations

import json
from uuid import UUID

from .db import get_pool
from .dispatch import dispatch, poll_until_done
from .drift import run_drift_sweep
from .ingest import ingest_report
from .models import DroneDispatchPayload, OrchestrateRequest, RunStatus
from .skills import render_skills_context, select_skills_for_task
from .spatial import build_spatial_context, get_fact_ids_for_context


async def orchestrate(req: OrchestrateRequest) -> RunStatus:
    """Full orchestration pipeline:

    1. Create run record (full provenance of the original request)
    2. Geocode task location
    3. Query PostGIS for spatial context
    4. Select + inject relevant skills
    5. Dispatch enriched task to drone
    6. Poll for completion
    7. Ingest drone output (extract, geocode, embed, contradiction-detect)
    8. Score the dispatch
    9. Run drift sweep
    10. Return final status
    """
    pool = await get_pool()

    # ── 1. Create orchestration run ──────────────────────────────

    lat = req.lat
    lon = req.lon

    geom_sql = "NULL"
    geom_params = []
    if lat is not None and lon is not None:
        geom_sql = "ST_SetSRID(ST_MakePoint($8, $9), 4326)"
        geom_params = [lon, lat]

    run_id = await pool.fetchval(
        f"""
        INSERT INTO orchestration_runs (
            source, source_request, task_description, task_instructions,
            task_geom, drone_model, drone_status, ingestion_status
        ) VALUES (
            $1, $2, $3, $4,
            {geom_sql}, $5, 'pending', 'pending'
        )
        RETURNING id
        """,
        req.source,
        json.dumps(req.model_dump()),
        req.description,
        req.instructions,
        req.model,
        *geom_params,
    )

    # ── 2 + 3. Spatial context ───────────────────────────────────

    spatial_context = None
    fact_ids: list[UUID] = []
    if lat is not None and lon is not None:
        spatial_context = await build_spatial_context(lat, lon)
        fact_ids = await get_fact_ids_for_context(lat, lon, 5000.0)

    # ── 4. Skills ────────────────────────────────────────────────

    skill_names = select_skills_for_task(req.description)
    skills_text = render_skills_context(skill_names)

    # ── 5. Build enriched instructions ───────────────────────────

    enriched_instructions = req.instructions

    context_blocks = []
    if req.context:
        context_blocks.append(req.context)
    if spatial_context and spatial_context.context_text != "No prior knowledge for this area.":
        context_blocks.append(spatial_context.context_text)
    if skills_text:
        context_blocks.append(skills_text)

    full_context = "\n\n---\n\n".join(context_blocks) if context_blocks else None

    # Store what we injected (provenance)
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

    # ── 6. Dispatch to drone ─────────────────────────────────────

    callback_url = req.callback_url or f"http://host.docker.internal:3020/callback"
    payload = DroneDispatchPayload(
        description=req.description,
        instructions=enriched_instructions,
        context=full_context,
        model=req.model,
        maxTurns=req.max_turns,
        callbackUrl=callback_url,
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

    # ── 7. Poll for completion ───────────────────────────────────

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

    # ── 8. Ingest ────────────────────────────────────────────────

    facts_count = 0
    contradictions = 0
    if result.status == "complete" and result.output:
        try:
            facts_count, contradictions = await ingest_report(
                run_id, drone_resp.taskId, result.output
            )
        except Exception as e:
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

    # ── 9. Score ─────────────────────────────────────────────────

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

        # Log skill scores
        for skill in skill_names:
            await pool.execute(
                """
                INSERT INTO skill_scores (skill_name, run_id, outcome_quality, fact_yield, contradiction_count)
                VALUES ($1, $2, $3, $4, $5)
                """,
                skill, run_id, quality, facts_count, contradictions,
            )

    # ── 10. Drift sweep ──────────────────────────────────────────

    await run_drift_sweep()

    # ── Return ───────────────────────────────────────────────────

    return RunStatus(
        id=run_id,
        task_description=req.description,
        drone_task_id=drone_resp.taskId,
        drone_status=result.status,
        ingestion_status="complete" if facts_count > 0 else "skipped",
        facts_extracted=facts_count,
        contradictions_found=contradictions,
        outcome_quality=quality,
        created_at=await pool.fetchval("SELECT created_at FROM orchestration_runs WHERE id = $1", run_id),
        dispatched_at=await pool.fetchval("SELECT dispatched_at FROM orchestration_runs WHERE id = $1", run_id),
        drone_completed_at=await pool.fetchval("SELECT drone_completed_at FROM orchestration_runs WHERE id = $1", run_id),
        ingested_at=await pool.fetchval("SELECT ingested_at FROM orchestration_runs WHERE id = $1", run_id),
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
