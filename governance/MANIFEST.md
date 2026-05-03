# governance — Intent CRS Module

## Purpose

Semantic governance layer for Datum agents. Classifies each agent action into an intent (SEARCH, READ, EXECUTE, etc.) by projecting a 1536-dimensional embedding into a pre-trained 3D UMAP space and finding the nearest labelled anchor centroid. No LLM call at runtime — only a distance lookup. Supports loop detection (low spatial variance in a sliding window), capability envelope checking (action outside trained hull), and multi-CRS spatial governance queries via PostGIS.

This module does NOT make policy decisions. It classifies and logs. Policy (allow/deny/alert) lives in the orchestrator.

## Dependencies

| Dependency | Used by | Failure behaviour |
|---|---|---|
| OpenAI API (`text-embedding-3-small`) | `governance_server.py`, `intent_crs_runtime.py` | Returns 500; action blocked (fail closed) |
| PostgreSQL + PostGIS | All files | Server refuses to start; runtime raises on first check |
| pgvector extension | `intent_crs_schema.sql` | Schema migration fails at CREATE INDEX |
| UMAP model (`.joblib`) | `governance_server.py`, `intent_crs_bootstrap.py`, `intent_crs_runtime.py` | Server refuses to start if model path invalid |
| ConceptLinker SQLite DB | `intent_crs_bootstrap.py` | Bootstrap fails; run-time unaffected |
| `vm102_brain_corpora.get_db_conn()` | All Python files | Must be importable from same directory |

## Dependents

| Caller | Endpoint / function | Contract |
|---|---|---|
| Datum orchestrator / `server.py` | `POST /check` | Must handle 500 gracefully; governance check is advisory unless policy says block |
| `demo3d_intent.html` (agentic-ui) | `/api/load-crs/{crs_id}` (served by agentic-ui, calls `intent_crs_export.py`) | Read-only; stale data acceptable |
| CLI testing | `intent_crs_runtime.py --action "..."` | Standalone; requires env vars |

## Failure Modes

| Failure | Status | Recovery |
|---|---|---|
| OpenAI API unavailable | `POST /check` returns 503 | Caller retries with backoff; action allowed on timeout (fail open) or blocked (fail closed) — set `write_db=False` for dry run |
| DB write fails | Action classified but not persisted; `action_id=null` in response | Non-fatal; classification still returned |
| Model file missing at startup | `RuntimeError` on load; server exits | Re-run `intent_crs_bootstrap.py` to regenerate model |
| No anchors for CRS | Empty distances dict; nearest anchor lookup returns first key | Re-run bootstrap |
| Loop check < 3 actions | Returns `loop_detected=false`, `reason="insufficient data"` | Normal at session start |

## Behavioral Contracts

- **Idempotent bootstrap**: `intent_crs_bootstrap.py` upserts — safe to re-run. Updates anchor positions; does not delete old CRS records.
- **State at startup**: `governance_server.py` loads UMAP model + anchors into memory once at startup. Hot-reload requires process restart.
- **DB writes**: Every `/check` call with `write_db=true` inserts one `agent_actions` row and one `action_projections` row inside a single transaction. If the transaction fails, the HTTP response still returns the classification result.
- **Loop detection**: Uses `session_id` to scope the sliding window. Actions without `session_id` (empty string) share a single window — avoid in production.
- **No retry semantics**: The server does not retry failed embedding calls. The caller decides whether to retry.
- **CRS is immutable at runtime**: Anchor positions are read once at startup. To update anchors, restart the server after re-running bootstrap.

## Files

| File | Role |
|---|---|
| `governance_server.py` | FastAPI microservice, runs at `:3011` |
| `intent_crs_bootstrap.py` | One-time training: embed corpus → UMAP → PostGIS anchors |
| `intent_crs_runtime.py` | Standalone runtime check + loop detection |
| `intent_crs_export.py` | Export CRS data to JSON.GZ for Three.js visualisation |
| `intent_crs_schema.sql` | PostGIS schema migration (run once) |
| `intent_taxonomy.yaml` | Intent ontology — labels, signals, loop_risk, adversarial patterns |

## Comprehension Gate

**Why this dependency (not an alternative)?**
UMAP is used because it preserves local topology — nearby embeddings stay nearby in 3D. This means semantically similar actions cluster together, making nearest-anchor classification reliable. A classifier (e.g. logistic regression) would work but loses the spatial interpretability needed for loop detection and envelope checking.

**Why this structure?**
Training is separated from runtime (`bootstrap.py` vs `runtime.py`) because the UMAP transform must be trained once on a labelled corpus, then frozen. Runtime never retrains — it only applies the saved transform. This makes runtime latency predictable (< 5 ms for projection).

**Where is state managed?**
The `State` singleton in `governance_server.py` holds the loaded UMAP reducer and anchor positions. It is populated once at FastAPI lifespan startup and never mutated again. Callers cannot observe or modify it. The DB (`action_projections`) holds persistent state; the in-memory state is a read-only cache of the trained model.

**Is business logic leaking across layers?**
No. This module classifies and logs. The verdict is `ALLOW` or `FLAG` — not `block` or `deny`. The orchestrator decides what to do with a `FLAG`. Policy rules (allow-list, deny-list, threshold escalation) live outside this module in `intent_taxonomy.yaml` and the orchestrator policy engine.
