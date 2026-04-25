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

### 16. Callback/poll race condition — `src/orchestrator.py` + `src/server.py`
**Status:** FIXED
- Root cause: sync `orchestrate()` sent `callbackUrl` to drone, so both callback and poll path raced to ingest. Callback would claim ingestion first via `_claim_ingestion()`, then fail silently in background task, leaving status stuck at `'running'`.
- Fix: Added `use_callback` parameter to `orchestrate()`. Sync path sends `callbackUrl=None` (polls instead). Async path sends callback URL.
- Also added `_safe_ingest()` wrapper in server.py to catch background ingestion errors and mark status as `'failed'`.
**Files changed:** `src/orchestrator.py`, `src/server.py`

---

## Re-Review Fixes (GPT-5.4 re-review, task 1ed662c7, score 6.5/10)

### 17. F-01: Async path polls AND sends callback — dual completion
**Status:** FIXED
- `orchestrate(use_callback=True)` now returns immediately after dispatch. No polling in callback mode.
- Callback path runs full post-completion pipeline (ingest + score + drift) via `_post_completion()`.
- Sync path polls as before and runs same `_post_completion()` pipeline.
**Files changed:** `src/orchestrator.py`, `src/server.py`

### 18. F-02: Contradiction updates use `pool` outside fact insert transaction
**Status:** FIXED
- `detect_contradictions()` and `_decrement_coverage_count()` now accept `conn` (not `pool`).
- Invalidations happen inside the same transaction as the fact insert.
- Renamed shadowed `result` variable to `update_result`.
**Files changed:** `src/ingest.py`

### 19. F-04: Dispatch/poll failures leave run status stuck
**Status:** FIXED
- `_post_completion()` catches ingestion errors and writes `'failed'` to DB.
- Extraction errors caught separately, also write `'failed'` + scoring_notes.
- `_build_run_status()` reads actual DB state instead of constructing from local vars.
**Files changed:** `src/orchestrator.py`, `src/ingest.py`

### 20. F-05/F-06: Embedding failure aborts ingestion / manifest mismatch
**Status:** FIXED
- Embedding failures are non-fatal: facts stored without vectors.
- Dimension mismatch sets `embedding=None` with warning log.
- Aligns with MANIFEST: "Facts stored without vectors; spatial queries work."
**Files changed:** `src/ingest.py`

### 21. F-07: Drift sweep failure fails orchestration
**Status:** FIXED
- Drift sweep wrapped in try/except inside `_post_completion()`. Failures logged, don't fail the run.
**Files changed:** `src/orchestrator.py`

### 22. F-08: Centroid drift events not deduplicated
**Status:** FIXED
- Added duplicate suppression: skip if unresolved or recent (<1 day) centroid_shift event exists.
**Files changed:** `src/drift.py`

### 23. F-10: f-string SQL in `/knowledge`
**Status:** FIXED
- Replaced f-string `where_clause` with parameterized `$5 OR invalid_at IS NULL`.
**Files changed:** `src/server.py`

### 24. F-12: No Pydantic models on `/callback` and `/ingest`
**Status:** FIXED
- Added `CallbackPayload` and `ManualIngestPayload` models.
- Both endpoints now validate input via Pydantic instead of raw `dict`.
**Files changed:** `src/models.py`, `src/server.py`

### 25. F-18: Return value misreports ingestion status
**Status:** FIXED
- `_build_run_status()` reads actual `ingestion_status` from DB row.
- Both sync and async paths use this shared helper.
- Coverage counts based on `inserted_findings` (actually stored) not `extraction.findings` (attempted).
**Files changed:** `src/orchestrator.py`, `src/ingest.py`

---

## Verification

Test dispatch 1 (2026-04-25 02:49 UTC):
- Task: "Research the current state of renewable energy adoption in New Zealand"
- Drone task: `087e9277`, status: `complete`
- **21 facts extracted**, 0 contradictions, quality 1.0

Test dispatch 2 (2026-04-25 03:21 UTC — post re-review fixes):
- Task: "Research recent volcanic activity in the Taupo Volcanic Zone"
- Drone task: `6bdb4b0e`, status: `complete`
- **13 facts extracted**, 0 contradictions, quality 0.65
- Ingestion status: `complete`, ingested_at populated
- Status reads from DB correctly (F-18 fix verified)

---

## Third-Pass Fixes (GPT-5.4 review 3, task e4ae45b5, score 7.4/10)

### 26. F-01: Sync dispatch failures leave run with drone_status='pending'
**Status:** FIXED
- Dispatch wrapped in try/except. On failure: `drone_status='dispatch_failed'`, `ingestion_status='skipped'`, error in `scoring_notes`.
**Files changed:** `src/orchestrator.py`

### 27. F-02: Non-complete drone status leaves ingestion_status='pending' forever
**Status:** FIXED
- Both sync (poll) and callback paths now set `ingestion_status='skipped'` when drone returns failed/cancelled.
**Files changed:** `src/orchestrator.py`, `src/server.py`

### 28. F-08: Contradiction invalidation accepts any UUID from LLM
**Status:** FIXED
- LLM-returned IDs filtered against the pre-fetched candidate set. Hallucinated/out-of-scope IDs discarded.
**Files changed:** `src/ingest.py`

### 29. F-09: Duplicate cell_id derivation logic
**Status:** FIXED
- Added `_cell_id(lat, lon)` helper. `_update_coverage()` uses it instead of inline f-string.
- `_decrement_coverage_count()` still derives from DB (correct — uses stored geom, not request params).
**Files changed:** `src/ingest.py`

### 30. F-10: Manual /ingest endpoint returns 500 with no failure state
**Status:** FIXED
- Wrapped in try/except. On failure: `ingestion_status='failed'` persisted, structured 500 response.
**Files changed:** `src/server.py`

### 31. F-16: Misleading comment in callback handler
**Status:** FIXED
- Changed "source_request" to "task_description" in comment and variable name.
**Files changed:** `src/server.py`

---

## Third-Pass Verification

Test dispatch (2026-04-25 03:33 UTC):
- Task: "List invasive plant species affecting native bush in the Tararua Range"
- Drone task: `53535d5e`, status: `complete`
- **14 facts extracted**, 0 contradictions, quality 0.7
- All terminal states correct

---

## Fifth-Pass Fixes (GPT-5.4 review 5, task 4acb40c9, score 6.5/10)

### DO-REV5-01: `_run_orchestration_bg` doesn't terminal `ingestion_status` on failure
**Status:** FIXED
- On exception, now sets `drone_status='failed'` AND `ingestion_status='skipped'` (if still `'pending'`).
- Prevents run staying permanently in a non-terminal state if the background task crashes.
**Files changed:** `src/server.py`

### DO-REV5-04/05: Duplicate callbacks create duplicate skill scores / overwrite terminal runs
**Status:** FIXED
- Callback handler now checks `drone_status` and rejects any callback when run is already terminal (not `'pending'` or `'dispatched'`).
- Prevents second callback from writing a second skill_score row or overwriting completed output.
**Files changed:** `src/server.py`

### DO-REV5-07: `geocode_confidence` populated with extraction confidence
**Status:** FIXED
- Column now receives `NULL` instead of `finding.confidence`. Extraction confidence != geocoding confidence; no geocoder is present to supply a real value.
- Removed the duplicate 12th positional arg from the geotagged fact insert.
**Files changed:** `src/ingest.py`

### DO-REV5-09: `poll_until_done` timeout leaves run stuck at `dispatched`
**Status:** FIXED
- Poll wrapped in try/except. On timeout or error: `drone_status='timed_out'`, `ingestion_status='skipped'`, error recorded in `scoring_notes`.
**Files changed:** `src/orchestrator.py`

### DO-REV5-11: Dead import `_get_embed_client` in `server.py`
**Status:** FIXED
- Removed unused import. `server.py` uses `_get_llm_client` only (for health check).
**Files changed:** `src/server.py`

### DO-REV5-02: `datetime` import wrong (FALSE POSITIVE)
**Not fixed** — `from datetime import datetime, timezone` on line 10 is correct and working. Reviewer was mistaken.

### DO-REV5-03: No auth on `/callback` (ARCHITECTURAL — DEFERRED)
**Not fixed** — same as previous passes. Internal docker network. Auth at gateway when boundary expands.

---

## NOT FIXED (Architectural / Out of Scope)

- **No auth on callback endpoint** — internal-only service on docker network. Auth belongs at API gateway level. If boundary expands, add HMAC callback signing.
- **BackgroundTasks not durable (review 3 F-03)** — needs a queue (Celery/ARQ) or reconciliation job. Deferred until scale requires it.
- **No watchdog for stuck dispatched runs (review 3 F-05)** — callback-missed runs can stay `dispatched` forever. Needs a cron/scheduled reconciliation sweep.
- **Ingestion claim not recoverable (review 3 F-06)** — `running` after crash can't be retried. Needs `ingestion_started_at` + stale claim recovery.
- **orchestrator.py god function** — partially mitigated by `_post_completion()` and `_build_run_status()`.
- **No migration strategy** — defer until schema stabilizes.
- **Lazy client init without async lock** — race is harmless for an internal service.
