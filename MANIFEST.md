# MANIFEST — Datum Orchestrator

## Purpose

Spatial self-learning orchestrator for the Datum drone fleet. Accepts research
tasks, enriches them with prior spatial knowledge from PostGIS, dispatches to
drones, ingests results back into the knowledge store, and monitors for
knowledge drift. Does NOT execute tasks itself — it orchestrates.

The self-learning loop is inspired by the Hermes agent pattern: procedure is
stored as editable SKILL.md files, not as opaque model weights. Every data
point traces back to its source drone output via full provenance chains.

## Dependencies

| Dependency | Type | Failure Behaviour |
|---|---|---|
| **PostGIS 16 + pgvector** | In-compose container | Orchestrator cannot start (503 on all endpoints) |
| **Drone API** (drone-agent:3000) | External service (drone-internal network) | Tasks queue but don't dispatch; ingestion continues for manual submissions |
| **LiteLLM** (drone-litellm:4000) | External service (drone-internal network) | Extraction fails; tasks still dispatch with stale/no context |
| **OpenAI Embeddings API** | Direct HTTPS | Facts stored without vectors; spatial queries work, semantic queries degraded |

## Dependents

| Dependent | Contract |
|---|---|
| **Datum** (agentic-ui :8090) | POST /orchestrate → {run_id, status}. POST /orchestrate/async for fire-and-forget. |
| **Drone** (drone-agent:3000) | Receives enriched tasks. Calls back to POST /callback on completion. No direct dependency on orchestrator. |

## Failure Modes

| Failure | Status | Recovery |
|---|---|---|
| PostGIS down | 503 on all endpoints | Restart postgis container |
| Drone unreachable | Task logged as dispatch_failed | Retries on next manual trigger |
| LLM extraction fails | Raw output stored, no structured facts | Re-run via POST /ingest |
| Embedding API fails | Facts stored without vectors (embedding=NULL) | Backfill via scheduled re-embed job |
| Contradiction detection fails | New facts stored, old facts not invalidated | Manual sweep via POST /drift/sweep |

## Behavioral Contracts

- **Read-only drone consumer**: never modifies drone state, only reads via GET endpoints
- **Idempotent ingestion**: ingestion is claimed atomically (pending → running). Poll path and callback path cannot both ingest. Unique constraint on (run_id, extraction_index) prevents duplicate facts.
- **Append-only knowledge store**: facts are invalidated (invalid_at set), never deleted. Full provenance chain via invalidated_by FK and run_id FK.
- **Full log reconstruction**: every orchestration run stores the original request, injected context snapshot, drone output verbatim, extracted facts with extraction_index, and invalidation chains. The run_log view joins all of this.
- **Drift events are advisory**: no autonomous action in Phase 1. Drift sweep logs events but does not auto-dispatch.
- **Skills are files**: editable, auditable, deletable by operator. No hidden state.

## State Management

All state is in PostGIS. The orchestrator process is stateless — any instance
can serve any request. No in-memory caches that affect correctness.

Tables: orchestration_runs, knowledge_facts, coverage_cells, drift_events, skill_scores.
Views: active_knowledge, invalidation_chains, run_log.

## Why These Dependencies

- **PostGIS** (not plain Postgres): spatial indexing (GIST) for ST_DWithin queries is core to the spatial context injection model. pgvector for semantic similarity search.
- **LiteLLM** (not direct OpenAI): already deployed in drone stack, provides model routing + rate limiting + cost tracking across GPT-5.4 variants.
- **GPT-5.4** for orchestrator (not mini): extraction accuracy and contradiction detection require stronger reasoning than mini provides. Drones stay on mini for cost.
- **Drone API** (not embedded execution): separation of concerns. Orchestrator enriches; drone executes. Keeps blast radius contained.
