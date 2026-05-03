#!/usr/bin/env python3
"""
intent_crs_export.py — Export Intent CRS data to JSON format for demo3d.html.

Queries:
  - intent_crs (active CRS only)
  - intent_crs_anchors (positions + labels)
  - action_projections (projected points + intent labels)

Output: {crs_id: {points: [...], anchors: [...]}}
"""

import gzip
import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
from vm102_brain_corpora import get_db_conn

# Intent label → colour (from governance_server.py)
LABEL_COLOURS = {
    "SEARCH":       "#c89632",
    "READ":         "#4a90d9",
    "ANALYZE":      "#5ba85a",
    "EXECUTE":      "#d95b5b",
    "WRITE":        "#9b59b6",
    "DELEGATE":     "#e67e22",
    "REQUEST":      "#1abc9c",
    "PLAN":         "#e74c3c",
    "RECALL":       "#34495e",
    "AUTHENTICATE": "#8e44ad",
    "VERIFY":       "#16a085",
    "TRANSFORM":    "#2980b9",
    "GENERATE":     "#f39c12",
    "NOTIFY":       "#7f8c8d",
    "UPDATE":       "#27ae60",
    "DELETE":       "#c0392b",
    "REFLECT":      "#bdc3c7",
    "RETRY":        "#e8b84b",
    "ABANDON":      "#95a5a6",
    "LIST":         "#3498db",
    "AUTHORIZE":    "#6c3483",
    "IDENTIFY":     "#117a65",
    "RESPOND":      "#a04000",
}
DEFAULT_COLOUR = "#aaaaaa"


def export_crs(crs_id: int) -> dict:
    """Export CRS data: points + anchors."""
    conn = get_db_conn()

    # Fetch CRS metadata
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT crs_id, name FROM intent_crs WHERE crs_id = %s AND is_active = true",
            (crs_id,),
        )
        crs_row = cur.fetchone()
    if not crs_row:
        raise ValueError(f"CRS {crs_id} not found or inactive")

    crs_name = crs_row["name"]

    # Fetch anchors
    anchors = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT label, ST_X(position) AS x, ST_Y(position) AS y, ST_Z(position) AS z
            FROM intent_crs_anchors
            WHERE crs_id = %s
            ORDER BY label
            """,
            (crs_id,),
        )
        for row in cur.fetchall():
            anchors.append({
                "label": row["label"],
                "color": LABEL_COLOURS.get(row["label"], DEFAULT_COLOUR),
                "x": float(row["x"]),
                "y": float(row["y"]),
                "z": float(row["z"]),
            })

    # Fetch projected points (all action_projections for this CRS)
    points = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT ST_X(position) AS x, ST_Y(position) AS y, ST_Z(position) AS z,
                   intent_label
            FROM action_projections
            WHERE crs_id = %s
            """,
            (crs_id,),
        )
        for row in cur.fetchall():
            points.append({
                "label": row["intent_label"] or "OTHER",
                "color": LABEL_COLOURS.get(row["intent_label"] or "OTHER", DEFAULT_COLOUR),
                "x": float(row["x"]),
                "y": float(row["y"]),
                "z": float(row["z"]),
            })

    conn.close()

    return {
        "crs_id": crs_id,
        "crs_name": crs_name,
        "points": points,
        "anchors": anchors,
    }


def export_all_crs() -> dict:
    """Export all active CRS as {crs_id: data}."""
    conn = get_db_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT crs_id FROM intent_crs WHERE is_active = true ORDER BY crs_id")
        crs_ids = [r[0] for r in cur.fetchall()]
    conn.close()

    result = {}
    for crs_id in crs_ids:
        try:
            result[crs_id] = export_crs(crs_id)
            print(f"✓ Exported CRS {crs_id} ({result[crs_id]['crs_name']})")
        except Exception as e:
            print(f"✗ CRS {crs_id}: {e}")

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--crs",
        type=int,
        help="Export specific CRS by ID (default: all active)",
    )
    parser.add_argument(
        "--out",
        default="/tmp/intent_crs_data.json.gz",
        help="Output gzip JSON file",
    )
    args = parser.parse_args()

    if args.crs:
        data = {"crs_data": {args.crs: export_crs(args.crs)}}
    else:
        data = {"crs_data": export_all_crs()}

    # Write gzipped JSON
    with gzip.open(args.out, "wt", encoding="utf-8") as f:
        json.dump(data, f)

    # Copy to static dir for serving
    static_dir = Path.home() / "projects/agentic-ui/static"
    if static_dir.exists():
        import shutil

        dest = static_dir / "intent_crs_data.json.gz"
        shutil.copy(args.out, dest)
        print(f"✓ Copied to {dest}")

    print(f"✓ Saved: {args.out}")
