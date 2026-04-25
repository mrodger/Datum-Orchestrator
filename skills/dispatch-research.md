# Skill: dispatch-research

Dispatch a research task to a drone with location-aware context injection.

## When to Use
- Task involves researching a topic tied to a geographic location
- Prior knowledge exists in the spatial store for the target area
- Coverage gaps have been detected nearby

## Procedure
1. Extract location from task description (city name, coordinates, region)
2. Query PostGIS for nearby active facts within 5km
3. Check for recent contradictions (last 30 days) within 10km
4. Identify coverage gaps in adjacent cells
5. Build context block with: known facts, contradictions, gap warnings
6. Inject context into drone instructions as "Known context for this area" section
7. Dispatch to drone with model gpt-5.4-mini, maxTurns 30

## Pitfalls
- Do not inject more than 15 facts (token budget)
- Contradictions should be flagged, not resolved in the prompt
- If no location can be determined, dispatch without spatial context (still useful)
- Coverage gaps are advisory — the drone may or may not investigate them

## Verification
- Check that the dispatched task includes the context_snapshot in orchestration_runs
- Verify facts_injected array is populated for provenance
