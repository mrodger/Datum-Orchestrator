# Skill: geocode-findings

Extract geographic coordinates from text findings during report ingestion.

## When to Use
- Processing drone output that contains location references
- Findings mention place names, addresses, coordinates, or regional identifiers

## Procedure
1. For each extracted finding, check if the LLM provided lat/lon
2. If lat/lon present: use directly (LLM geocoding from context)
3. If location_text present but no coordinates: flag for future geocoding service
4. If no location at all: store fact without geometry (still valuable for semantic search)

## Known Patterns (NZ focus)
- City names: Wellington (-41.29, 174.78), Christchurch (-43.53, 172.64), Auckland (-36.85, 174.76), Greymouth (-42.45, 171.21)
- Regional council names → centroid of region
- NZTM coordinates (e.g. E1571234 N5178234) → convert via proj
- Decimal degrees with S/E suffixes → negate S latitude

## Pitfalls
- LLM-provided coordinates can be approximate (city-level, not address-level)
- Set geocode_confidence accordingly: 0.9 for exact coords, 0.5 for city-level, 0.3 for region
- Never fabricate coordinates — null geometry is better than wrong geometry
