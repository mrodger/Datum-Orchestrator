# Datum-Orchestrator — Review Fixes Log

**Date:** 2026-04-25
**Source:** GPT-5.4 code review (drone task f3cef9e3)
**Reviewer score:** 4.7/10
**Goal:** Address all CRITICAL, HIGH, and actionable MEDIUM findings

---

## CRITICAL Fixes

### 1. SQL injection via f-string geometry — `src/ingest.py`
**Status:** NOT PRESENT (false positive)
The review flagged `geom_sql = f"ST_SetSRID(ST_MakePoint({finding.lon}, {finding.lat}), 4326)"` but the actual code already uses parameterized SQL: `ST_SetSRID(ST_MakePoint($7, $8), 4326)` with `finding.lon, finding.lat` passed as query parameters.
**Action:** No code change needed. The f-string SQL was from an earlier version that was already fixed before the review ran.

### 2. Duplicate ingestion race — `src/orchestrator.py` + `src/server.py`
**Status:** FIXED
- Added `ingestion_status` transition guard: only ingest if status is `'pending'`, atomically set to `'running'` via `UPDATE ... WHERE ingestion_status = 'pending' RETURNING id`
- Callback path and poll path both use the same guard — first one wins
- Added unique constraint `(run_id, extraction_index)` on `knowledge_facts`
**Files changed:** `src/ingest.py`, `src/server.py`, `db/init.sql`

### 3. Schema: `gen_random_uuid()` without `pgcrypto` — `db/init.sql`
**Status:** FIXED
- PostgreSQL 13+ includes `gen_random_uuid()` as a core function, but we add `pgcrypto` as belt-and-suspenders for older images.
**Files changed:** `db/init.sql`

---

## HIGH Fixes

### 4. `DriftEvent.detected_at=None` — `src/drift.py` + `src/models.py`
**Status:** FIXED
- Changed `DriftEvent.detected_at` to `datetime | None = None` in models.py
- In drift.py, `_check_contradiction_rate` and `_check_centroid_drift` now use `datetime.now(timezone.utc)` instead of `None`
**Files changed:** `src/models.py`, `src/drift.py`

### 5. Silent exception swallowing — `src/ingest.py`
**Status:** FIXED
- Replaced `except Exception: pass` with `except Exception as e: logger.warning(...)` and proper error counting
**Files changed:** `src/ingest.py`

### 6. `active_fact_count` not decremented — `src/ingest.py`
**Status:** FIXED
- After invalidation, decrement `active_fact_count` on affected coverage cells
**Files changed:** `src/ingest.py`

### 7. No transaction around ingestion — `src/ingest.py`
**Status:** FIXED
- Wrapped per-finding work (insert + contradiction detection) in `pool.acquire()` + `conn.transaction()`
- Run-level update remains outside transaction (status should update even on partial failure)
**Files changed:** `src/ingest.py`

### 8. SSRF via callback_url — `src/orchestrator.py`
**Status:** FIXED
- Removed `callback_url` from `OrchestrateRequest` model
- Hardcoded internal callback URL in orchestrator
**Files changed:** `src/models.py`, `src/orchestrator.py`

### 9. Dead comments / geocode references
**Status:** FIXED
- Removed "geocode" from docstrings (no geocoding service exists)
- Removed no-op `enriched_instructions = req.instructions`
- Renamed variable to `base_instructions` for clarity
**Files changed:** `src/ingest.py`, `src/orchestrator.py`

---

## MEDIUM Fixes

### 10. HTTP client lifecycle — `src/server.py`
**Status:** FIXED
- Close `dispatch._client`, `ingest._llm_client`, `ingest._embed_client` in lifespan shutdown
**Files changed:** `src/server.py`, `src/dispatch.py`, `src/ingest.py`

### 11. Input validation — `src/models.py`
**Status:** FIXED
- Added `Field(ge=-90, le=90)` for lat, `Field(ge=-180, le=180)` for lon on `OrchestrateRequest` and `ExtractedFinding`
- Added `Field(le=100)` for `max_turns`, `Field(le=100)` for limit, `Field(le=50000)` for radius_m
**Files changed:** `src/models.py`, `src/server.py`

### 12. Unused imports — `src/server.py`
**Status:** FIXED
- Removed unused `DriftStatus` and `KnowledgeQuery` imports from server.py
**Files changed:** `src/server.py`

### 13. Embedding dimension assertion — `src/ingest.py`
**Status:** FIXED
- Added `EMBEDDING_DIM = 1536` constant and assertion before DB insert
**Files changed:** `src/ingest.py`

### 14. `datetime.utcnow()` → timezone-aware — `src/server.py`
**Status:** FIXED
- Switched to `datetime.now(timezone.utc)`
**Files changed:** `src/server.py`

### 15. Orchestrator return: replace 4x fetchval with single fetchrow
**Status:** FIXED
- Combined four individual `fetchval` calls into one `fetchrow` query
**Files changed:** `src/orchestrator.py`

---

## NOT FIXED (Architectural / Out of Scope)

- **No auth on endpoints** — internal-only service on docker network. Auth belongs at API gateway level, not here.
- **orchestrator.py god function** — refactoring the 10-step loop is architectural work for a future sprint.
- **No migration strategy** — valid concern, defer until schema stabilizes.
- **Missing observability** — added `logging` module with structured messages. Full metrics/tracing is out of scope.
- **Synchronous orchestration at scale** — async mode exists and works. Sync mode is convenience for testing.
