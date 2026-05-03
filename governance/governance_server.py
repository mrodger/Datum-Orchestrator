#!/usr/bin/env python3
"""
governance_server.py — Lightweight governance microservice for the Intent CRS.

Runs in the brain-embed venv (has joblib, umap-learn, psycopg2).
Pre-loads UMAP model and anchor positions on startup.
datum-ui calls POST /check for each tool_use event — fire and forget.

Usage:
  source ~/.secrets.env
  ~/venvs/brain-embed/bin/python governance_server.py [--port 3011]

Endpoints:
  POST /check        — check one action, write to agent_actions + action_projections
  GET  /health       — liveness
  GET  /anchors      — current anchor positions (for inspection)
"""

import argparse
import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import joblib
import numpy as np
import openai
import psycopg2
import psycopg2.extras
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
from vm102_brain_corpora import get_db_conn

EMBED_MODEL = "text-embedding-3-small"
DEFAULT_CRS = "governance-coding-v1"
MODEL_DIR = Path.home() / "vault/vm102_brain"


# ── State (loaded at startup) ─────────────────────────────────────────────────

class State:
    crs: dict = {}
    reducer = None
    anchors: dict[str, tuple[float, float, float]] = {}
    oai: openai.OpenAI = None


state = State()


def load_crs_state(crs_name: str = DEFAULT_CRS):
    conn = get_db_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT crs_id, name, mode, datum, model_path, default_direction
            FROM intent_crs WHERE name = %s AND is_active = true
        """, (crs_name,))
        row = cur.fetchone()

    if not row:
        raise RuntimeError(f"CRS '{crs_name}' not found")

    state.crs = dict(row)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT label, ST_X(position), ST_Y(position), ST_Z(position)
            FROM intent_crs_anchors WHERE crs_id = %s
        """, (state.crs["crs_id"],))
        state.anchors = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}

    conn.close()

    model_path = state.crs["model_path"]
    state.reducer = joblib.load(model_path)
    print(f"[governance] Loaded CRS '{crs_name}' with {len(state.anchors)} anchors")
    print(f"[governance] Model: {model_path}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.oai = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    load_crs_state()
    yield


app = FastAPI(lifespan=lifespan)


# ── Models ────────────────────────────────────────────────────────────────────

class CheckRequest(BaseModel):
    action_text: str
    agent_id: str = "datum"
    session_id: str = ""
    step_number: int = 0
    tool_name: str = ""
    source: str = "tool_use"
    direction: str | None = None    # override CRS default
    threshold: float = 4.0
    write_db: bool = True           # set False to skip DB writes (dry-run)


class CheckResponse(BaseModel):
    nearest_label: str
    nearest_dist: float
    flagged: bool
    verdict: str
    position: list[float]
    all_distances: dict[str, float]
    action_id: int | None = None


# ── Core logic ────────────────────────────────────────────────────────────────

def _embed(text: str) -> np.ndarray:
    resp = state.oai.embeddings.create(model=EMBED_MODEL, input=[text[:8000]])
    return np.array(resp.data[0].embedding, dtype=np.float32)


def _project(vec: np.ndarray) -> tuple[float, float, float]:
    pos = state.reducer.transform(vec.reshape(1, -1))[0]
    return float(pos[0]), float(pos[1]), float(pos[2])


def _distances(pos: tuple[float, float, float]) -> dict[str, float]:
    px, py, pz = pos
    return {
        label: round(((px-ax)**2 + (py-ay)**2 + (pz-az)**2)**0.5, 4)
        for label, (ax, ay, az) in state.anchors.items()
    }


def _govern(dists: dict[str, float], direction: str, threshold: float):
    nearest = min(dists, key=dists.get)
    dist = dists[nearest]
    if direction == "inward":
        flagged = dist < threshold
    else:
        flagged = dist > threshold
    return flagged, "FLAG" if flagged else "ALLOW", nearest, dist


def _write_action(req: CheckRequest, pos: tuple, nearest_label: str,
                   nearest_dist: float, verdict: str, dists: dict) -> int | None:
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO agent_actions
                    (agent_id, session_id, step_number, action_text, source, tool_name, outcome)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING action_id
            """, (
                req.agent_id, req.session_id, req.step_number,
                req.action_text[:2000], req.source, req.tool_name, verdict,
            ))
            action_id = cur.fetchone()[0]

            x, y, z = pos
            cur.execute("""
                INSERT INTO action_projections
                    (action_id, crs_id, position, intent_label, intent_distance)
                VALUES (%s, %s, ST_MakePoint(%s, %s, %s)::geometry(PointZ, 0), %s, %s)
            """, (
                action_id, state.crs["crs_id"],
                x, y, z,
                nearest_label, nearest_dist,
            ))

        conn.commit()
        conn.close()
        return action_id
    except Exception as e:
        print(f"[governance] DB write failed: {e}")
        return None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/check", response_model=CheckResponse)
async def check(req: CheckRequest):
    loop = asyncio.get_event_loop()

    # Embed + project in thread pool (CPU/IO bound)
    vec = await loop.run_in_executor(None, _embed, req.action_text)
    pos = await loop.run_in_executor(None, _project, vec)

    dists = _distances(pos)
    direction = req.direction or state.crs["default_direction"]
    flagged, verdict, nearest_label, nearest_dist = _govern(dists, direction, req.threshold)

    action_id = None
    if req.write_db:
        action_id = await loop.run_in_executor(
            None, _write_action, req, pos, nearest_label, nearest_dist, verdict, dists
        )

    return CheckResponse(
        nearest_label=nearest_label,
        nearest_dist=nearest_dist,
        flagged=flagged,
        verdict=verdict,
        position=list(pos),
        all_distances=dists,
        action_id=action_id,
    )


class LoopRequest(BaseModel):
    session_id: str
    window: int = 5
    variance_threshold: float = 1.5
    repetition_threshold: float = 0.6   # fraction of window with same intent_label


class LoopResponse(BaseModel):
    loop_detected: bool
    verdict: str
    variance: float | None
    dominant_label: str | None
    repetition_rate: float | None
    n: int
    reason: str


def _check_loop(session_id: str, window: int, var_thresh: float, rep_thresh: float) -> LoopResponse:
    conn = get_db_conn()
    crs_id = state.crs["crs_id"]
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ST_X(ap.position), ST_Y(ap.position), ST_Z(ap.position),
                       ap.intent_label
                FROM action_projections ap
                JOIN agent_actions aa ON aa.action_id = ap.action_id
                WHERE aa.session_id = %s AND ap.crs_id = %s
                ORDER BY aa.step_number DESC
                LIMIT %s
            """, (session_id, crs_id, window))
            rows = cur.fetchall()
    finally:
        conn.close()

    n = len(rows)
    if n < 3:
        return LoopResponse(loop_detected=False, verdict="NORMAL", variance=None,
                            dominant_label=None, repetition_rate=None, n=n,
                            reason="insufficient data")

    positions = np.array([[x, y, z] for x, y, z, _ in rows], dtype=np.float32)
    labels = [r[3] for r in rows]

    variance = float(np.mean(np.var(positions, axis=0)))

    # Repetition: fraction of window dominated by the most common label
    from collections import Counter
    counts = Counter(labels)
    dominant_label, dominant_count = counts.most_common(1)[0]
    repetition_rate = dominant_count / n

    spatial_loop = variance < var_thresh
    repetition_loop = repetition_rate >= rep_thresh
    loop_detected = spatial_loop or repetition_loop

    reasons = []
    if spatial_loop:
        reasons.append(f"low spatial variance ({variance:.3f} < {var_thresh})")
    if repetition_loop:
        reasons.append(f"repetitive intent {dominant_label!r} ({repetition_rate:.0%})")

    return LoopResponse(
        loop_detected=loop_detected,
        verdict="LOOP_FLAG" if loop_detected else "NORMAL",
        variance=round(variance, 4),
        dominant_label=dominant_label,
        repetition_rate=round(repetition_rate, 3),
        n=n,
        reason="; ".join(reasons) if reasons else "ok",
    )


@app.post("/loop", response_model=LoopResponse)
async def loop_check(req: LoopRequest):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _check_loop, req.session_id, req.window,
        req.variance_threshold, req.repetition_threshold
    )


@app.get("/health")
def health():
    return {
        "ok": True,
        "crs": state.crs.get("name"),
        "anchors": len(state.anchors),
    }


@app.get("/anchors")
def anchors():
    return {
        label: {"x": x, "y": y, "z": z}
        for label, (x, y, z) in sorted(state.anchors.items())
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3011)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
