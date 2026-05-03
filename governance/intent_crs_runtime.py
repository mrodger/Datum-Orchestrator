#!/usr/bin/env python3
"""
intent_crs_runtime.py — Runtime projection and governance check for the Intent CRS.

Given an action text:
  1. Embed via OpenAI text-embedding-3-small
  2. Project into CRS space using the stored UMAP model
  3. Query PostGIS for nearest anchor labels + distances
  4. Apply governance rules → verdict

Can be used as a module or run from CLI for testing.

Usage:
  source ~/.secrets.env
  python3 intent_crs_runtime.py "search the web for recent NZ earthquake data"
  python3 intent_crs_runtime.py --crs governance-coding-v1 --threshold 3.0 "read file /etc/passwd"
  python3 intent_crs_runtime.py --loop-check --window 5  # check last 5 agent_actions for loops
"""

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import openai
import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
from vm102_brain_corpora import get_db_conn

MODEL_DIR = Path.home() / "vault/vm102_brain"
EMBED_MODEL = "text-embedding-3-small"


@dataclass
class GovernanceResult:
    action_text: str
    crs_name: str
    position: tuple[float, float, float]
    nearest_label: str
    nearest_dist: float
    all_distances: dict[str, float]       # label → distance
    direction: str                         # inward | outward
    threshold: float
    flagged: bool
    verdict: str                           # ALLOW | FLAG | DENY


def load_crs(conn, crs_name: str) -> dict:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT crs_id, name, mode, datum, model_path, default_direction
            FROM intent_crs WHERE name = %s AND is_active = true
        """, (crs_name,))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"CRS '{crs_name}' not found or inactive")
    return dict(row)


def load_anchors(conn, crs_id: int) -> dict[str, tuple[float, float, float]]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT label,
                   ST_X(position) AS x,
                   ST_Y(position) AS y,
                   ST_Z(position) AS z
            FROM intent_crs_anchors
            WHERE crs_id = %s
        """, (crs_id,))
        return {row[0]: (row[1], row[2], row[3]) for row in cur.fetchall()}


def embed(client: openai.OpenAI, text: str) -> np.ndarray:
    resp = client.embeddings.create(model=EMBED_MODEL, input=[text[:8000]])
    return np.array(resp.data[0].embedding, dtype=np.float32)


def project(reducer, embedding: np.ndarray) -> tuple[float, float, float]:
    pos = reducer.transform(embedding.reshape(1, -1))[0]
    return float(pos[0]), float(pos[1]), float(pos[2])


def distances_to_anchors(
    position: tuple[float, float, float],
    anchors: dict[str, tuple[float, float, float]]
) -> dict[str, float]:
    px, py, pz = position
    result = {}
    for label, (ax, ay, az) in anchors.items():
        dist = ((px - ax)**2 + (py - ay)**2 + (pz - az)**2) ** 0.5
        result[label] = round(dist, 4)
    return result


def apply_governance(
    distances: dict[str, float],
    direction: str,
    threshold: float,
) -> tuple[bool, str, str, float]:
    """
    Returns (flagged, verdict, nearest_label, nearest_dist).

    inward:  flag if distance to nearest anchor < threshold  (too close = loop / repetition)
    outward: flag if distance to nearest anchor > threshold  (too far = novelty / out-of-envelope)
    """
    nearest_label = min(distances, key=distances.get)
    nearest_dist = distances[nearest_label]

    if direction == "inward":
        flagged = nearest_dist < threshold
        verdict = "FLAG" if flagged else "ALLOW"
    else:  # outward
        flagged = nearest_dist > threshold
        verdict = "FLAG" if flagged else "ALLOW"

    return flagged, verdict, nearest_label, nearest_dist


def check_action(
    action_text: str,
    crs_name: str = "governance-coding-v1",
    threshold: float = 4.0,
    direction: str | None = None,   # override CRS default
) -> GovernanceResult:
    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    conn = get_db_conn()

    crs = load_crs(conn, crs_name)
    anchors = load_anchors(conn, crs["crs_id"])

    model_path = crs["model_path"]
    reducer = joblib.load(model_path)

    effective_direction = direction or crs["default_direction"]

    vec = embed(client, action_text)
    pos = project(reducer, vec)
    dists = distances_to_anchors(pos, anchors)
    flagged, verdict, nearest_label, nearest_dist = apply_governance(
        dists, effective_direction, threshold
    )

    conn.close()

    return GovernanceResult(
        action_text=action_text,
        crs_name=crs_name,
        position=pos,
        nearest_label=nearest_label,
        nearest_dist=nearest_dist,
        all_distances=dists,
        direction=effective_direction,
        threshold=threshold,
        flagged=flagged,
        verdict=verdict,
    )


def check_loop(
    conn,
    crs_name: str = "governance-coding-v1",
    window: int = 5,
    variance_threshold: float = 1.5,
) -> dict:
    """
    Check the last N agent_actions for spatial clustering (loop detection).
    Returns variance of positions — low variance = possible loop.
    """
    crs = load_crs(conn, crs_name)
    anchors = load_anchors(conn, crs["crs_id"])

    with conn.cursor() as cur:
        cur.execute("""
            SELECT ap.position
            FROM action_projections ap
            JOIN agent_actions aa ON aa.action_id = ap.action_id
            WHERE ap.crs_id = %s
            ORDER BY aa.step_number DESC
            LIMIT %s
        """, (crs["crs_id"], window))
        rows = cur.fetchall()

    if len(rows) < 3:
        return {"loop_detected": False, "reason": "insufficient data", "n": len(rows)}

    positions = np.array([
        [psycopg2.extras.register_default_json(None)] for r in rows
    ])  # placeholder — position parsing handled below

    # Parse geometry positions from PostGIS
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ST_X(ap.position), ST_Y(ap.position), ST_Z(ap.position)
            FROM action_projections ap
            JOIN agent_actions aa ON aa.action_id = ap.action_id
            WHERE ap.crs_id = %s
            ORDER BY aa.step_number DESC
            LIMIT %s
        """, (crs["crs_id"], window))
        pts = cur.fetchall()

    if len(pts) < 3:
        return {"loop_detected": False, "reason": "insufficient projected actions", "n": len(pts)}

    positions = np.array([[x, y, z] for x, y, z in pts], dtype=np.float32)
    variance = float(np.mean(np.var(positions, axis=0)))
    loop_detected = variance < variance_threshold

    return {
        "loop_detected": loop_detected,
        "variance": round(variance, 4),
        "threshold": variance_threshold,
        "n": len(pts),
        "verdict": "LOOP_FLAG" if loop_detected else "NORMAL",
    }


def print_result(r: GovernanceResult):
    print(f"\n{'='*55}")
    print(f"  Action : {r.action_text[:70]}")
    print(f"  CRS    : {r.crs_name}")
    print(f"  Position: ({r.position[0]:.3f}, {r.position[1]:.3f}, {r.position[2]:.3f})")
    print(f"  Nearest: {r.nearest_label} (dist={r.nearest_dist:.3f})")
    print(f"  Mode   : {r.direction}, threshold={r.threshold}")
    print(f"  Verdict: {'*** ' + r.verdict + ' ***' if r.flagged else r.verdict}")
    print()
    print("  All distances:")
    for label, dist in sorted(r.all_distances.items(), key=lambda x: x[1]):
        marker = " ◄" if label == r.nearest_label else ""
        print(f"    {label:15s}  {dist:.3f}{marker}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="?", help="Action text to check")
    parser.add_argument("--crs", default="governance-coding-v1")
    parser.add_argument("--threshold", type=float, default=4.0)
    parser.add_argument("--direction", choices=["inward", "outward"], default=None)
    parser.add_argument("--loop-check", action="store_true", help="Check recent actions for loops")
    parser.add_argument("--window", type=int, default=5, help="Window size for loop check")
    args = parser.parse_args()

    if args.loop_check:
        conn = get_db_conn()
        result = check_loop(conn, args.crs, args.window)
        conn.close()
        print(result)
    elif args.action:
        result = check_action(args.action, args.crs, args.threshold, args.direction)
        print_result(result)
    else:
        parser.print_help()
