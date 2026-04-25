"""Drift monitor — detect spatial knowledge drift via three signals.

1. Embedding centroid drift per coverage cell
2. Contradiction rate vs baseline
3. Coverage staleness (cells with facts but no recent updates)
"""

from __future__ import annotations

import math

from .db import get_pool
from .models import DriftEvent, DriftStatus

# Thresholds
CENTROID_DRIFT_THRESHOLD = 0.15     # cosine distance
CONTRADICTION_RATE_THRESHOLD = 0.3  # 30% of facts in window contradicted
STALENESS_DAYS = 30                 # cells older than this are stale


async def get_drift_status(cell_id: str | None = None) -> list[DriftStatus]:
    """Get current drift scores for all cells (or a specific cell)."""
    pool = await get_pool()

    if cell_id:
        rows = await pool.fetch(
            "SELECT * FROM coverage_cells WHERE cell_id = $1", cell_id
        )
    else:
        rows = await pool.fetch(
            """
            SELECT * FROM coverage_cells
            WHERE fact_count > 0
            ORDER BY staleness_score DESC, contradiction_rate DESC
            LIMIT 50
            """
        )

    return [
        DriftStatus(
            cell_id=r["cell_id"],
            staleness_score=r["staleness_score"],
            contradiction_rate=r["contradiction_rate"],
            centroid_drift=r["centroid_drift"],
            fact_count=r["fact_count"],
            active_fact_count=r["active_fact_count"],
            last_ingested_at=r["last_ingested_at"],
        )
        for r in rows
    ]


async def run_drift_sweep() -> list[DriftEvent]:
    """Run all three drift checks across all coverage cells.

    Called periodically (e.g. after each ingestion or on a schedule).
    Returns list of new drift events detected.
    """
    pool = await get_pool()
    events: list[DriftEvent] = []

    cells = await pool.fetch(
        "SELECT cell_id FROM coverage_cells WHERE fact_count > 0"
    )

    for cell in cells:
        cid = cell["cell_id"]

        # 1. Staleness check
        stale = await _check_staleness(pool, cid)
        if stale:
            events.append(stale)

        # 2. Contradiction rate
        contra = await _check_contradiction_rate(pool, cid)
        if contra:
            events.append(contra)

        # 3. Centroid drift (requires enough embeddings)
        drift = await _check_centroid_drift(pool, cid)
        if drift:
            events.append(drift)

    return events


async def _check_staleness(pool, cell_id: str) -> DriftEvent | None:
    """Check if a cell's knowledge has gone stale."""
    row = await pool.fetchrow(
        """
        SELECT last_ingested_at,
               EXTRACT(EPOCH FROM NOW() - last_ingested_at) / 86400 AS days_stale
        FROM coverage_cells
        WHERE cell_id = $1 AND last_ingested_at IS NOT NULL
        """,
        cell_id,
    )
    if not row or row["days_stale"] < STALENESS_DAYS:
        return None

    staleness = min(1.0, row["days_stale"] / (STALENESS_DAYS * 3))

    # Update cell
    await pool.execute(
        "UPDATE coverage_cells SET staleness_score = $1 WHERE cell_id = $2",
        staleness, cell_id,
    )

    # Log event (only if not already logged recently)
    existing = await pool.fetchval(
        """
        SELECT id FROM drift_events
        WHERE cell_id = $1 AND event_type = 'coverage_gap'
          AND resolved_at IS NULL
        """,
        cell_id,
    )
    if existing:
        return None

    event_id = await pool.fetchval(
        """
        INSERT INTO drift_events (event_type, severity, cell_id, metric_value, threshold, details)
        VALUES ('coverage_gap', $1, $2, $3, $4, $5)
        RETURNING id
        """,
        "warning" if staleness > 0.6 else "info",
        cell_id,
        staleness,
        float(STALENESS_DAYS),
        {"days_stale": row["days_stale"], "last_ingested": row["last_ingested_at"].isoformat()},
    )

    return DriftEvent(
        id=event_id,
        event_type="coverage_gap",
        severity="warning" if staleness > 0.6 else "info",
        cell_id=cell_id,
        metric_value=staleness,
        threshold=float(STALENESS_DAYS),
        details={"days_stale": row["days_stale"]},
        detected_at=row["last_ingested_at"],
        resolved_at=None,
    )


async def _check_contradiction_rate(pool, cell_id: str) -> DriftEvent | None:
    """Check if contradiction rate in a cell exceeds threshold."""
    row = await pool.fetchrow(
        """
        WITH cell_facts AS (
            SELECT id, invalid_at
            FROM knowledge_facts
            WHERE geom IS NOT NULL
              AND ST_Within(geom, (SELECT geom FROM coverage_cells WHERE cell_id = $1))
              AND ingested_at > NOW() - INTERVAL '30 days'
        )
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE invalid_at IS NOT NULL) AS invalidated
        FROM cell_facts
        """,
        cell_id,
    )

    if not row or row["total"] < 3:
        return None

    rate = row["invalidated"] / row["total"]
    await pool.execute(
        "UPDATE coverage_cells SET contradiction_rate = $1 WHERE cell_id = $2",
        rate, cell_id,
    )

    if rate < CONTRADICTION_RATE_THRESHOLD:
        return None

    existing = await pool.fetchval(
        """
        SELECT id FROM drift_events
        WHERE cell_id = $1 AND event_type = 'contradiction_spike'
          AND resolved_at IS NULL AND detected_at > NOW() - INTERVAL '1 day'
        """,
        cell_id,
    )
    if existing:
        return None

    severity = "critical" if rate > 0.5 else "warning"
    event_id = await pool.fetchval(
        """
        INSERT INTO drift_events (event_type, severity, cell_id, metric_value, threshold, details)
        VALUES ('contradiction_spike', $1, $2, $3, $4, $5)
        RETURNING id
        """,
        severity, cell_id, rate, CONTRADICTION_RATE_THRESHOLD,
        {"total_facts": row["total"], "invalidated": row["invalidated"]},
    )

    return DriftEvent(
        id=event_id, event_type="contradiction_spike", severity=severity,
        cell_id=cell_id, metric_value=rate, threshold=CONTRADICTION_RATE_THRESHOLD,
        details={"total_facts": row["total"], "invalidated": row["invalidated"]},
        detected_at=None, resolved_at=None,
    )


async def _check_centroid_drift(pool, cell_id: str) -> DriftEvent | None:
    """Compare embedding centroid of recent vs reference facts in a cell.

    Reference window: 7-30 days ago. Current window: last 7 days.
    """
    # Get reference embeddings (7-30 days old)
    ref_rows = await pool.fetch(
        """
        SELECT embedding FROM knowledge_facts
        WHERE invalid_at IS NULL AND embedding IS NOT NULL
          AND geom IS NOT NULL
          AND ST_Within(geom, (SELECT geom FROM coverage_cells WHERE cell_id = $1))
          AND ingested_at BETWEEN NOW() - INTERVAL '30 days' AND NOW() - INTERVAL '7 days'
        """,
        cell_id,
    )

    # Get current embeddings (last 7 days)
    cur_rows = await pool.fetch(
        """
        SELECT embedding FROM knowledge_facts
        WHERE invalid_at IS NULL AND embedding IS NOT NULL
          AND geom IS NOT NULL
          AND ST_Within(geom, (SELECT geom FROM coverage_cells WHERE cell_id = $1))
          AND ingested_at > NOW() - INTERVAL '7 days'
        """,
        cell_id,
    )

    if len(ref_rows) < 3 or len(cur_rows) < 2:
        return None  # insufficient data

    # Compute centroids and cosine distance
    ref_centroid = _mean_vector([r["embedding"] for r in ref_rows])
    cur_centroid = _mean_vector([r["embedding"] for r in cur_rows])
    drift = _cosine_distance(ref_centroid, cur_centroid)

    await pool.execute(
        "UPDATE coverage_cells SET centroid_drift = $1 WHERE cell_id = $2",
        drift, cell_id,
    )

    if drift < CENTROID_DRIFT_THRESHOLD:
        return None

    severity = "critical" if drift > 0.3 else "warning"
    event_id = await pool.fetchval(
        """
        INSERT INTO drift_events (event_type, severity, cell_id, metric_value, threshold, details)
        VALUES ('centroid_shift', $1, $2, $3, $4, $5)
        RETURNING id
        """,
        severity, cell_id, drift, CENTROID_DRIFT_THRESHOLD,
        {"ref_count": len(ref_rows), "cur_count": len(cur_rows)},
    )

    return DriftEvent(
        id=event_id, event_type="centroid_shift", severity=severity,
        cell_id=cell_id, metric_value=drift, threshold=CENTROID_DRIFT_THRESHOLD,
        details={"ref_count": len(ref_rows), "cur_count": len(cur_rows)},
        detected_at=None, resolved_at=None,
    )


def _mean_vector(vectors: list) -> list[float]:
    """Compute mean of a list of vectors."""
    n = len(vectors)
    dim = len(vectors[0])
    result = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            result[i] += float(v[i])
    return [x / n for x in result]


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """1 - cosine_similarity. Range: 0 (identical) to 2 (opposite)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 1.0
    return 1.0 - (dot / (norm_a * norm_b))
