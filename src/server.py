"""Datum Orchestrator — FastAPI server.

Accepts tasks from Datum, enriches with spatial context from PostGIS,
dispatches to drones, ingests results, monitors drift.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query

from .db import close_pool, get_pool
from .dispatch import check_drone_health, close_client as close_dispatch_client
from .drift import get_drift_status, run_drift_sweep
from .ingest import close_clients as close_ingest_clients, ingest_report
from .models import (
    HealthResponse,
    KnowledgeFact,
    OrchestrateRequest,
    RunStatus,
)
from .orchestrator import get_run, orchestrate


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: warm the pool
    await get_pool()
    yield
    # Shutdown: close all connections
    await close_ingest_clients()
    await close_dispatch_client()
    await close_pool()


app = FastAPI(
    title="Datum Orchestrator",
    description="Spatial self-learning orchestrator for the Datum drone fleet",
    version="0.1.0",
    lifespan=lifespan,
)


# ── POST /orchestrate ────────────────────────────────────────────

@app.post("/orchestrate", response_model=RunStatus)
async def handle_orchestrate(req: OrchestrateRequest):
    """Accept a task, enrich with spatial context, dispatch to drone,
    ingest results. Synchronous — returns when the full pipeline completes.
    """
    return await orchestrate(req)


# ── POST /orchestrate/async ──────────────────────────────────────

@app.post("/orchestrate/async")
async def handle_orchestrate_async(req: OrchestrateRequest, bg: BackgroundTasks):
    """Fire-and-forget orchestration. Returns run_id immediately.

    Use GET /orchestrate/{id} to poll status.
    """
    pool = await get_pool()
    import json
    run_id = await pool.fetchval(
        """
        INSERT INTO orchestration_runs (
            source, source_request, task_description, task_instructions,
            drone_model, drone_status, ingestion_status
        ) VALUES ($1, $2, $3, $4, $5, 'pending', 'pending')
        RETURNING id
        """,
        req.source,
        json.dumps(req.model_dump()),
        req.description,
        req.instructions,
        req.model,
    )
    bg.add_task(_run_orchestration_bg, run_id, req)
    return {"run_id": str(run_id), "status": "accepted"}


async def _run_orchestration_bg(run_id: UUID, req: OrchestrateRequest):
    """Background orchestration task."""
    try:
        await orchestrate(req, existing_run_id=run_id)
    except Exception as e:
        pool = await get_pool()
        await pool.execute(
            "UPDATE orchestration_runs SET drone_status = 'failed', scoring_notes = $1 WHERE id = $2",
            str(e), run_id,
        )


# ── GET /orchestrate/{id} ────────────────────────────────────────

@app.get("/orchestrate/{run_id}", response_model=RunStatus)
async def handle_get_run(run_id: UUID):
    """Get status of an orchestrated task."""
    result = await get_run(run_id)
    if not result:
        raise HTTPException(status_code=404, detail="Run not found")
    return result


# ── POST /callback ───────────────────────────────────────────────

@app.post("/callback")
async def handle_drone_callback(payload: dict, bg: BackgroundTasks):
    """Webhook endpoint for drone completion callbacks.

    Triggers ingestion in the background.
    """
    task_id = payload.get("taskId")
    if not task_id:
        raise HTTPException(status_code=400, detail="taskId required")

    pool = await get_pool()
    run = await pool.fetchrow(
        "SELECT id, drone_status FROM orchestration_runs WHERE drone_task_id = $1",
        task_id,
    )
    if not run:
        # Callback for a task we didn't dispatch — ignore
        return {"status": "ignored", "reason": "unknown task"}

    # Update drone status
    status = payload.get("status", "complete")
    output = payload.get("output", "")
    await pool.execute(
        """
        UPDATE orchestration_runs
        SET drone_status = $1, drone_output_raw = $2, drone_completed_at = NOW()
        WHERE id = $3
        """,
        status, output, run["id"],
    )

    # Trigger ingestion in background
    if status == "complete" and output:
        bg.add_task(_safe_ingest, run["id"], task_id, output)

    return {"status": "accepted", "run_id": str(run["id"])}


async def _safe_ingest(run_id: UUID, task_id: str, output: str):
    """Wrapper for background ingestion with error handling."""
    import logging
    logger = logging.getLogger(__name__)
    try:
        await ingest_report(run_id, task_id, output)
    except Exception as e:
        logger.error("Background ingestion failed for run %s: %s", run_id, e)
        pool = await get_pool()
        await pool.execute(
            "UPDATE orchestration_runs SET ingestion_status = 'failed', scoring_notes = $1 WHERE id = $2",
            f"Callback ingestion error: {e}", run_id,
        )


# ── GET /knowledge ───────────────────────────────────────────────

@app.get("/knowledge")
async def handle_knowledge(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    radius_m: float = Query(default=5000.0, ge=1, le=50000),
    limit: int = Query(default=20, ge=1, le=100),
    include_invalidated: bool = False,
):
    """Query spatial knowledge store."""
    pool = await get_pool()

    where_clause = "" if include_invalidated else "AND invalid_at IS NULL"
    rows = await pool.fetch(
        f"""
        SELECT id, content, category, location_text, confidence,
               valid_at, invalid_at, invalidation_reason,
               drone_task_id, run_id,
               ST_Y(geom) AS lat, ST_X(geom) AS lon,
               ST_Distance(
                   geom::geography,
                   ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
               ) AS distance_m
        FROM knowledge_facts
        WHERE geom IS NOT NULL
          {where_clause}
          AND ST_DWithin(
              geom::geography,
              ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
              $3
          )
        ORDER BY distance_m ASC
        LIMIT $4
        """,
        lon, lat, radius_m, limit,
    )

    return {
        "facts": [
            KnowledgeFact(
                id=r["id"], content=r["content"], category=r["category"],
                location_text=r["location_text"], lat=r["lat"], lon=r["lon"],
                confidence=r["confidence"], valid_at=r["valid_at"],
                invalid_at=r["invalid_at"], invalidation_reason=r["invalidation_reason"],
                drone_task_id=r["drone_task_id"], run_id=r["run_id"],
                distance_m=r["distance_m"],
            )
            for r in rows
        ],
        "count": len(rows),
    }


# ── GET /drift ───────────────────────────────────────────────────

@app.get("/drift")
async def handle_drift(cell_id: str | None = None):
    """Current drift status across coverage cells."""
    statuses = await get_drift_status(cell_id)
    return {"cells": [s.model_dump() for s in statuses]}


# ── POST /drift/sweep ────────────────────────────────────────────

@app.post("/drift/sweep")
async def handle_drift_sweep():
    """Manually trigger a drift sweep across all coverage cells."""
    events = await run_drift_sweep()
    return {
        "events_detected": len(events),
        "events": [e.model_dump() for e in events],
    }


# ── GET /runs ────────────────────────────────────────────────────

@app.get("/runs")
async def handle_list_runs(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    """List orchestration runs (newest first). Full log reconstruction."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, source, task_description, drone_task_id, drone_status,
               ingestion_status, facts_extracted, contradictions_found,
               outcome_quality, created_at, dispatched_at,
               drone_completed_at, ingested_at
        FROM orchestration_runs
        ORDER BY created_at DESC
        LIMIT $1 OFFSET $2
        """,
        limit, offset,
    )
    total = await pool.fetchval("SELECT COUNT(*) FROM orchestration_runs")
    return {
        "runs": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# ── GET /runs/{id}/provenance ────────────────────────────────────

@app.get("/runs/{run_id}/provenance")
async def handle_provenance(run_id: UUID):
    """Full provenance for a run — the original request, injected context,
    drone output, extracted facts, and any contradictions caused.
    """
    pool = await get_pool()

    run = await pool.fetchrow("SELECT * FROM orchestration_runs WHERE id = $1", run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    facts = await pool.fetch(
        """
        SELECT id, content, category, location_text, raw_excerpt,
               confidence, valid_at, invalid_at, invalidation_reason,
               extraction_index, ST_Y(geom) AS lat, ST_X(geom) AS lon
        FROM knowledge_facts
        WHERE run_id = $1
        ORDER BY extraction_index
        """,
        run_id,
    )

    invalidations = await pool.fetch(
        """
        SELECT old.id AS old_id, old.content AS old_content,
               new.id AS new_id, new.content AS new_content,
               old.invalidation_reason
        FROM knowledge_facts old
        JOIN knowledge_facts new ON new.id = old.invalidated_by
        WHERE new.run_id = $1
        """,
        run_id,
    )

    return {
        "run": dict(run),
        "facts_produced": [dict(f) for f in facts],
        "invalidations_caused": [dict(i) for i in invalidations],
    }


# ── POST /ingest (manual) ────────────────────────────────────────

@app.post("/ingest")
async def handle_manual_ingest(payload: dict):
    """Manual ingestion for testing/backfill.

    Expects: {"drone_task_id": "...", "output": "..."}
    """
    drone_task_id = payload.get("drone_task_id")
    output = payload.get("output")
    if not drone_task_id or not output:
        raise HTTPException(status_code=400, detail="drone_task_id and output required")

    pool = await get_pool()
    import json

    run_id = await pool.fetchval(
        """
        INSERT INTO orchestration_runs (
            source, source_request, task_description, task_instructions,
            drone_task_id, drone_status, drone_output_raw,
            drone_completed_at, ingestion_status
        ) VALUES ('manual', $1, 'Manual ingestion', '', $2, 'complete', $3, NOW(), 'pending')
        RETURNING id
        """,
        json.dumps(payload),
        drone_task_id,
        output,
    )

    facts, contradictions = await ingest_report(run_id, drone_task_id, output)

    return {
        "run_id": str(run_id),
        "facts_extracted": facts,
        "contradictions_found": contradictions,
    }


# ── GET /health ──────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def handle_health():
    """Health check — DB connectivity + drone status."""
    db_status = "ok"
    try:
        pool = await get_pool()
        await pool.fetchval("SELECT 1")
    except Exception as e:
        db_status = str(e)

    drone_status = await check_drone_health()

    overall = "healthy" if db_status == "ok" and drone_status == "ok" else "degraded"

    return HealthResponse(
        status=overall,
        db=db_status,
        drone=drone_status,
        timestamp=datetime.now(timezone.utc),
    )
