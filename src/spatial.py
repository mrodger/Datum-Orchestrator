"""Spatial context retrieval from PostGIS — nearby facts, contradictions, gaps."""

from __future__ import annotations

from uuid import UUID

from .db import get_pool
from .models import Contradiction, NearbyFact, SpatialContext


async def build_spatial_context(
    lat: float,
    lon: float,
    radius_m: float = 5000.0,
    limit: int = 15,
) -> SpatialContext:
    """Query PostGIS for spatial context around a point.

    Returns nearby facts, recent contradictions, and coverage gaps —
    plus a pre-rendered text block ready for prompt injection.
    """
    pool = await get_pool()

    nearby = await _nearby_facts(pool, lat, lon, radius_m, limit)
    contradictions = await _recent_contradictions(pool, lat, lon, radius_m * 2)
    gaps = await _coverage_gaps(pool, lat, lon, radius_m * 3)
    context_text = _render_context(nearby, contradictions, gaps)

    return SpatialContext(
        nearby_facts=nearby,
        recent_contradictions=contradictions,
        coverage_gaps=gaps,
        context_text=context_text,
    )


async def _nearby_facts(
    pool, lat: float, lon: float, radius_m: float, limit: int
) -> list[NearbyFact]:
    """Active facts within radius, ordered by distance."""
    rows = await pool.fetch(
        """
        SELECT id, content, category, confidence, valid_at,
               ST_Distance(
                   geom::geography,
                   ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
               ) AS distance_m
        FROM knowledge_facts
        WHERE invalid_at IS NULL
          AND geom IS NOT NULL
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
    return [
        NearbyFact(
            id=r["id"],
            content=r["content"],
            category=r["category"],
            confidence=r["confidence"],
            distance_m=r["distance_m"],
            valid_at=r["valid_at"],
        )
        for r in rows
    ]


async def _recent_contradictions(
    pool, lat: float, lon: float, radius_m: float, days: int = 30
) -> list[Contradiction]:
    """Facts invalidated in the last N days near this point."""
    rows = await pool.fetch(
        """
        SELECT
            old.id AS old_id, old.content AS old_content,
            new.id AS new_id, new.content AS new_content,
            old.invalid_at,
            ST_Distance(
                old.geom::geography,
                ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
            ) AS distance_m
        FROM knowledge_facts old
        JOIN knowledge_facts new ON new.id = old.invalidated_by
        WHERE old.invalid_at > NOW() - INTERVAL '1 day' * $3
          AND old.geom IS NOT NULL
          AND ST_DWithin(
              old.geom::geography,
              ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
              $4
          )
        ORDER BY old.invalid_at DESC
        LIMIT 10
        """,
        lon, lat, days, radius_m,
    )
    return [
        Contradiction(
            old_fact_id=r["old_id"],
            old_content=r["old_content"],
            new_fact_id=r["new_id"],
            new_content=r["new_content"],
            invalidated_at=r["invalid_at"],
            distance_m=r["distance_m"],
        )
        for r in rows
    ]


async def _coverage_gaps(
    pool, lat: float, lon: float, radius_m: float, stale_days: int = 30
) -> list[str]:
    """Coverage cells near this point that are stale."""
    rows = await pool.fetch(
        """
        SELECT cell_id
        FROM coverage_cells
        WHERE fact_count > 0
          AND last_ingested_at < NOW() - INTERVAL '1 day' * $3
          AND ST_DWithin(
              geom::geography,
              ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
              $4
          )
        ORDER BY staleness_score DESC
        LIMIT 5
        """,
        lon, lat, stale_days, radius_m,
    )
    return [r["cell_id"] for r in rows]


async def get_fact_ids_for_context(
    lat: float, lon: float, radius_m: float, limit: int = 15
) -> list[UUID]:
    """Return IDs of facts that would be injected as context (for provenance)."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id FROM knowledge_facts
        WHERE invalid_at IS NULL AND geom IS NOT NULL
          AND ST_DWithin(
              geom::geography,
              ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
              $3
          )
        ORDER BY ST_Distance(
            geom::geography,
            ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
        ) ASC
        LIMIT $4
        """,
        lon, lat, radius_m, limit,
    )
    return [r["id"] for r in rows]


def _render_context(
    nearby: list[NearbyFact],
    contradictions: list[Contradiction],
    gaps: list[str],
) -> str:
    """Render spatial context as a text block for prompt injection."""
    lines: list[str] = []

    if nearby:
        lines.append("## Known context for this area\n")
        for f in nearby:
            conf = "high" if f.confidence > 0.8 else "medium" if f.confidence > 0.5 else "low"
            dist = f"{f.distance_m:.0f}m" if f.distance_m < 1000 else f"{f.distance_m / 1000:.1f}km"
            cat = f" [{f.category}]" if f.category else ""
            lines.append(f"- ({conf} confidence, {dist} away{cat}) {f.content}")

    if contradictions:
        lines.append("\n## Recent contradictions in this area\n")
        for c in contradictions:
            lines.append(f"- SUPERSEDED ({c.invalidated_at.date()}): {c.old_content}")
            lines.append(f"  REPLACED BY: {c.new_content}")

    if gaps:
        lines.append(f"\n## Coverage gaps: {len(gaps)} stale cells nearby")
        lines.append("Consider investigating these under-covered areas.")

    if not lines:
        return "No prior knowledge for this area."

    return "\n".join(lines)
