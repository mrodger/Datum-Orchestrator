-- Intent CRS Schema — Datum Agent Governance
-- PostGIS-native. Multi-CRS per action, single embedding per action.
--
-- Design principle:
--   One action → one embedding → N projections (one per registered CRS)
--   Each CRS is a separate UMAP model trained for a different detection purpose:
--     governance  — cluster boundary = allow/deny line
--     novelty     — cluster boundary = seen-before vs new
--     loop        — cluster boundary = same-intent vs different
--   Checking all CRS modes = one multi-layer PostGIS spatial query
--
-- Analogy: one GPS coordinate checked against multiple thematic map layers
--   (zoning layer, flood zone layer, heritage layer) simultaneously.

-- ── CRS Registry ─────────────────────────────────────────────────────────────

CREATE TABLE intent_crs (
    crs_id          SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,          -- e.g. "governance-coding-v1"
    mode            TEXT NOT NULL,                 -- governance | novelty | loop | semantic
    datum           TEXT NOT NULL,                 -- embedding model, e.g. "text-embedding-3-large"
    version         TEXT NOT NULL DEFAULT '1',
    description     TEXT,
    ontology        JSONB,                         -- intent labels this CRS was trained on
    accuracy        JSONB,                         -- per-intent accuracy from validation
    region_of_validity TEXT,                       -- natural language: "coding agent actions"
    model_path      TEXT,                          -- path to .joblib UMAP model
    --
    -- direction: which side of the threshold triggers a flag.
    --   'inward'  — flag when distance < threshold (point is INSIDE buffer)
    --               use for: loop detection, governance deny-zone, capability envelope breach
    --   'outward' — flag when distance > threshold (point is OUTSIDE buffer)
    --               use for: novelty detection, research exploration, out-of-envelope alert
    --
    -- Same spatial op (ST_Distance), opposite trigger logic.
    -- A single CRS can be queried in either direction by overriding at rule level.
    default_direction TEXT NOT NULL DEFAULT 'inward'
                      CHECK (default_direction IN ('inward', 'outward')),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Known modes and their natural directions:
--   governance  inward  — flag when inside DENY zone (distance to DENY < distance to ALLOW)
--   novelty     outward — flag when outside recent-window centroid buffer (distance > threshold)
--   loop        inward  — flag when window variance < threshold (points collapsing together)
--   semantic    n/a     — retrieval only, no threshold
--   envelope    inward  — flag when OUTSIDE hull (ST_3DWithin = false → treated as inward breach)
--
-- For research tasks: query novelty CRS with outward direction → surface genuinely new actions.
-- For loop detection: query loop CRS with inward direction → surface repeated semantic regions.


-- ── Reference Points ──────────────────────────────────────────────────────────
-- Named anchor points in each CRS — intent centroids, allow/deny boundaries, etc.
-- Equivalent to control points or fiducials in a surveyed CRS.

CREATE TABLE intent_crs_anchors (
    anchor_id       SERIAL PRIMARY KEY,
    crs_id          INT NOT NULL REFERENCES intent_crs(crs_id),
    label           TEXT NOT NULL,    -- e.g. "ALLOW", "DENY", "RETRY", "SEARCH"
    anchor_type     TEXT NOT NULL,    -- centroid | boundary | exemplar
    position        geometry(PointZ, 0) NOT NULL,
    sample_count    INT,              -- number of training examples this centroid was computed from
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON intent_crs_anchors USING GIST (position);
CREATE INDEX ON intent_crs_anchors (crs_id, label);


-- ── Agent Actions ─────────────────────────────────────────────────────────────
-- The live event stream. Each action is a single agent step.

CREATE TABLE agent_actions (
    action_id       BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL,               -- which agent instance
    session_id      TEXT,                        -- conversation / run ID
    step_number     INT,                         -- position in current session
    action_text     TEXT NOT NULL,               -- raw action: tool call, reasoning step, etc.
    source          TEXT NOT NULL,               -- agent | user_message | tool_result | system
    tool_name       TEXT,                        -- if a tool call, which tool
    tool_input      JSONB,                       -- tool input if applicable
    outcome         TEXT,                        -- success | failure | partial | unknown
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON agent_actions (agent_id, session_id, step_number);
CREATE INDEX ON agent_actions (session_id, created_at);


-- ── Action Embeddings + Projections ──────────────────────────────────────────
-- One row per (action, CRS). Position = action's coordinates in that CRS's 3D space.
-- Single embedding stored once; position differs per CRS.

CREATE TABLE action_projections (
    action_id       BIGINT NOT NULL REFERENCES agent_actions(action_id),
    crs_id          INT NOT NULL REFERENCES intent_crs(crs_id),
    embedding       vector(1536),               -- 1536D from datum (shared across CRS)
    position        geometry(PointZ, 0),        -- 3D coordinates in this CRS's space
    intent_label    TEXT,                       -- nearest anchor label after classification
    intent_distance FLOAT,                      -- distance to nearest anchor centroid
    projected_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (action_id, crs_id)
);

CREATE INDEX ON action_projections USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX ON action_projections USING GIST (position);
CREATE INDEX ON action_projections (crs_id, intent_label);


-- ── Governance Decisions ──────────────────────────────────────────────────────
-- Results of multi-CRS evaluation for a single action.
-- Written after all CRS checks complete; consumed by the policy engine.

CREATE TABLE governance_decisions (
    decision_id     BIGSERIAL PRIMARY KEY,
    action_id       BIGINT NOT NULL REFERENCES agent_actions(action_id),
    agent_id        TEXT NOT NULL,

    -- governance CRS result
    gov_intent      TEXT,                   -- classified governance_tag
    gov_distance    FLOAT,                  -- distance to nearest allow/deny boundary
    gov_verdict     TEXT,                   -- allow | deny | alert

    -- novelty CRS result
    novelty_score   FLOAT,                  -- distance from recent-window centroid
    novelty_flag    BOOLEAN DEFAULT FALSE,  -- true if above novelty threshold

    -- loop CRS result
    loop_score      FLOAT,                  -- variance of recent window in loop CRS
    loop_count      INT,                    -- how many recent actions share this intent region
    loop_flag       BOOLEAN DEFAULT FALSE,

    -- composite
    final_verdict   TEXT NOT NULL,          -- pass | alert | block
    triggered_patterns TEXT[],             -- e.g. ['retry_loop', 'instruction_override']
    notes           TEXT,

    decided_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON governance_decisions (agent_id, decided_at);
CREATE INDEX ON governance_decisions (final_verdict);


-- ── Multi-CRS Check Query ─────────────────────────────────────────────────────
-- Check one action against all active CRS modes in a single query.
-- Call after inserting action_projections rows for the new action.

-- Example: classify intent and measure novelty + loop score for action $1
-- Returns one row per CRS with distance to nearest anchor.

CREATE OR REPLACE VIEW action_crs_summary AS
SELECT
    ap.action_id,
    ic.mode,
    ic.name              AS crs_name,
    ap.intent_label,
    ap.intent_distance,
    -- distance to ALLOW anchor (governance mode only)
    (SELECT ST_Distance(ap.position, a.position)
     FROM intent_crs_anchors a
     WHERE a.crs_id = ap.crs_id AND a.label = 'ALLOW'
     LIMIT 1)            AS dist_to_allow,
    -- distance to DENY anchor (governance mode only)
    (SELECT ST_Distance(ap.position, a.position)
     FROM intent_crs_anchors a
     WHERE a.crs_id = ap.crs_id AND a.label = 'DENY'
     LIMIT 1)            AS dist_to_deny,
    ap.position,
    ap.projected_at
FROM action_projections ap
JOIN intent_crs ic ON ic.crs_id = ap.crs_id
WHERE ic.is_active = TRUE;


-- ── Window Variance Function ──────────────────────────────────────────────────
-- Compute spatial variance of last N actions in a given CRS.
-- Low variance = stuck loop. Used by loop CRS check.

CREATE OR REPLACE FUNCTION window_spatial_variance(
    p_session_id  TEXT,
    p_crs_id      INT,
    p_window      INT DEFAULT 10
) RETURNS FLOAT AS $$
DECLARE
    variance FLOAT;
BEGIN
    SELECT
        AVG(
            ST_Distance(ap.position,
                ST_Centroid(ST_Collect(ap.position) OVER ()))
        ) INTO variance
    FROM (
        SELECT ap.position
        FROM action_projections ap
        JOIN agent_actions aa ON aa.action_id = ap.action_id
        WHERE aa.session_id = p_session_id
          AND ap.crs_id = p_crs_id
        ORDER BY aa.step_number DESC
        LIMIT p_window
    ) ap;
    RETURN COALESCE(variance, 1.0);
END;
$$ LANGUAGE plpgsql;


-- ── Agent Capability Envelope ─────────────────────────────────────────────────
-- Which intent regions are "normal" for a given agent, derived from its corpus.
-- Populated at agent startup by projecting the agent's knowledge corpus
-- into governance CRS space and computing the convex hull of resulting points.

CREATE TABLE agent_capability_envelope (
    envelope_id     SERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    crs_id          INT NOT NULL REFERENCES intent_crs(crs_id),
    -- convex hull of all action projections from this agent's corpus
    hull            geometry(PolygonZ, 0),
    -- or simpler: list of permitted intent labels derived from corpus
    permitted_intents TEXT[],
    derived_from    TEXT,   -- corpus name or trace source
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (agent_id, crs_id)
);

CREATE INDEX ON agent_capability_envelope USING GIST (hull);

-- Usage: check if new action falls within agent's normal envelope
-- ST_3DWithin(new_action.position, envelope.hull, tolerance)
-- Outside = anomaly regardless of governance tag
