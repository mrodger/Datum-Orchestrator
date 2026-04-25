-- Datum-Orchestrator PostGIS Schema
-- Full provenance: every point reconstructable to source drone output

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- ORCHESTRATION LOG
-- Every task the orchestrator handles, start to finish
-- ============================================================

CREATE TABLE orchestration_runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- What came in
    source              TEXT NOT NULL DEFAULT 'datum',  -- 'datum', 'api', 'autonomous'
    source_request      JSONB NOT NULL,                 -- full original request body
    task_description    TEXT NOT NULL,
    task_instructions   TEXT NOT NULL,
    task_geom           GEOMETRY(POINT, 4326),          -- geocoded task location

    -- What we injected
    context_snapshot    TEXT,                            -- full spatial context injected into drone prompt
    skills_used         TEXT[],                          -- which SKILL.md files were selected
    facts_injected      UUID[],                         -- FK refs to knowledge_facts used as context

    -- Drone dispatch
    drone_task_id       TEXT,                            -- task ID returned by drone API
    drone_model         TEXT DEFAULT 'gpt-5.4-mini',
    drone_status        TEXT DEFAULT 'pending',          -- pending, dispatched, complete, failed
    drone_output_raw    TEXT,                            -- verbatim drone output (full log)

    -- Ingestion results
    facts_extracted     INT DEFAULT 0,
    contradictions_found INT DEFAULT 0,
    ingestion_status    TEXT DEFAULT 'pending',          -- pending, complete, failed, skipped

    -- Scoring
    outcome_quality     FLOAT,                          -- 0-1, computed post-ingestion
    scoring_notes       TEXT,

    -- Timestamps
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_at       TIMESTAMPTZ,
    drone_completed_at  TIMESTAMPTZ,
    ingested_at         TIMESTAMPTZ,
    scored_at           TIMESTAMPTZ
);

CREATE INDEX idx_runs_created ON orchestration_runs (created_at DESC);
CREATE INDEX idx_runs_drone ON orchestration_runs (drone_task_id);
CREATE INDEX idx_runs_status ON orchestration_runs (drone_status);
CREATE INDEX idx_runs_geom ON orchestration_runs USING GIST (task_geom);

-- ============================================================
-- KNOWLEDGE FACTS
-- Every finding extracted from drone reports, geotagged + embedded.
-- Append-only: facts are invalidated (invalid_at), never deleted.
-- ============================================================

CREATE TABLE knowledge_facts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Provenance chain (reconstructable)
    run_id              UUID NOT NULL REFERENCES orchestration_runs(id),
    drone_task_id       TEXT NOT NULL,
    extraction_index    INT NOT NULL,                   -- position in extraction batch (0-based)

    -- Content
    content             TEXT NOT NULL,                   -- the extracted finding
    category            TEXT,                            -- topic/domain tag
    raw_excerpt         TEXT,                            -- verbatim excerpt from drone output this was extracted from

    -- Spatial
    geom                GEOMETRY(POINT, 4326),
    location_text       TEXT,                            -- original location string before geocoding
    geocode_confidence  FLOAT,                          -- geocoder confidence score

    -- Embedding
    embedding           VECTOR(1536),

    -- Confidence + validity
    confidence          FLOAT NOT NULL DEFAULT 0.7,
    source_type         TEXT NOT NULL DEFAULT 'drone_report',
    valid_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    invalid_at          TIMESTAMPTZ,                    -- set when superseded
    invalidated_by      UUID REFERENCES knowledge_facts(id),
    invalidation_reason TEXT,                           -- why this fact was superseded

    -- Timestamps
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_facts_embedding ON knowledge_facts
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_facts_geom ON knowledge_facts USING GIST (geom);
CREATE INDEX idx_facts_valid ON knowledge_facts (valid_at)
    WHERE invalid_at IS NULL;
CREATE INDEX idx_facts_run ON knowledge_facts (run_id);
CREATE INDEX idx_facts_drone ON knowledge_facts (drone_task_id);
CREATE INDEX idx_facts_category ON knowledge_facts (category)
    WHERE invalid_at IS NULL;
CREATE UNIQUE INDEX idx_facts_run_extraction ON knowledge_facts (run_id, extraction_index);

-- ============================================================
-- COVERAGE CELLS
-- Spatial grid tracking what areas have been covered and how fresh
-- ============================================================

CREATE TABLE coverage_cells (
    cell_id             TEXT PRIMARY KEY,                -- "lat_lon_zoom" or H3 index
    geom                GEOMETRY(POLYGON, 4326) NOT NULL,
    fact_count          INT NOT NULL DEFAULT 0,
    active_fact_count   INT NOT NULL DEFAULT 0,         -- facts where invalid_at IS NULL
    last_ingested_at    TIMESTAMPTZ,
    first_ingested_at   TIMESTAMPTZ,
    staleness_score     FLOAT NOT NULL DEFAULT 0.0,     -- 0=fresh, 1=very stale
    contradiction_rate  FLOAT NOT NULL DEFAULT 0.0,     -- invalidations/total in window
    centroid_drift      FLOAT NOT NULL DEFAULT 0.0      -- last computed drift score
);

CREATE INDEX idx_coverage_geom ON coverage_cells USING GIST (geom);
CREATE INDEX idx_coverage_stale ON coverage_cells (staleness_score DESC)
    WHERE fact_count > 0;

-- ============================================================
-- DRIFT EVENTS
-- Log of detected drift — auditable, timestamped
-- ============================================================

CREATE TABLE drift_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type          TEXT NOT NULL,                   -- 'centroid_shift', 'contradiction_spike', 'coverage_gap'
    severity            TEXT NOT NULL DEFAULT 'info',    -- 'info', 'warning', 'critical'
    cell_id             TEXT REFERENCES coverage_cells(cell_id),
    geom                GEOMETRY(POINT, 4326),          -- event location (for non-cell events)

    -- Detail
    metric_value        FLOAT,                          -- the measured drift value
    threshold           FLOAT,                          -- the threshold that was exceeded
    details             JSONB,                          -- full context (affected fact IDs, etc.)

    -- Resolution
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ,
    resolution_run_id   UUID REFERENCES orchestration_runs(id)  -- which run resolved it
);

CREATE INDEX idx_drift_type ON drift_events (event_type, detected_at DESC);
CREATE INDEX idx_drift_unresolved ON drift_events (detected_at DESC)
    WHERE resolved_at IS NULL;
CREATE INDEX idx_drift_geom ON drift_events USING GIST (geom);

-- ============================================================
-- SKILL EFFECTIVENESS
-- Track which skills + context patterns produce good outcomes
-- ============================================================

CREATE TABLE skill_scores (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    skill_name          TEXT NOT NULL,
    run_id              UUID NOT NULL REFERENCES orchestration_runs(id),
    outcome_quality     FLOAT,
    fact_yield          INT,                            -- facts extracted
    contradiction_count INT,
    scored_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_skill_name ON skill_scores (skill_name, scored_at DESC);

-- ============================================================
-- VIEWS — convenience queries
-- ============================================================

-- Active knowledge (current, non-invalidated facts)
CREATE VIEW active_knowledge AS
SELECT id, content, category, geom, location_text, confidence, embedding,
       valid_at, ingested_at, drone_task_id, run_id
FROM knowledge_facts
WHERE invalid_at IS NULL;

-- Invalidation chains (what superseded what)
CREATE VIEW invalidation_chains AS
SELECT
    new.id AS current_id,
    new.content AS current_content,
    new.valid_at AS current_valid_at,
    old.id AS superseded_id,
    old.content AS superseded_content,
    old.valid_at AS superseded_valid_at,
    old.invalid_at AS superseded_invalid_at,
    old.invalidation_reason,
    ST_Distance(new.geom::geography, old.geom::geography) AS distance_m
FROM knowledge_facts new
JOIN knowledge_facts old ON old.invalidated_by = new.id;

-- Run summary (for log reconstruction)
CREATE VIEW run_log AS
SELECT
    r.id,
    r.source,
    r.task_description,
    r.drone_task_id,
    r.drone_status,
    r.facts_extracted,
    r.contradictions_found,
    r.outcome_quality,
    r.created_at,
    r.dispatched_at,
    r.drone_completed_at,
    r.ingested_at,
    array_agg(kf.id) FILTER (WHERE kf.id IS NOT NULL) AS fact_ids,
    array_agg(kf.content) FILTER (WHERE kf.id IS NOT NULL) AS fact_contents
FROM orchestration_runs r
LEFT JOIN knowledge_facts kf ON kf.run_id = r.id
GROUP BY r.id
ORDER BY r.created_at DESC;
