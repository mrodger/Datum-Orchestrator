"""Pydantic models for the Datum Orchestrator."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# ── Inbound request ──────────────────────────────────────────────

class OrchestrateRequest(BaseModel):
    description: str
    instructions: str
    context: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    location_text: str | None = None
    model: str = "gpt-5.4-mini"
    max_turns: int = Field(default=30, ge=1, le=100)
    source: str = "datum"


# ── Orchestration run (DB row) ───────────────────────────────────

class RunStatus(BaseModel):
    id: UUID
    task_description: str
    drone_task_id: str | None
    drone_status: str
    ingestion_status: str
    facts_extracted: int
    contradictions_found: int
    outcome_quality: float | None
    created_at: datetime
    dispatched_at: datetime | None
    drone_completed_at: datetime | None
    ingested_at: datetime | None


# ── Drone dispatch / response ────────────────────────────────────

class DroneDispatchPayload(BaseModel):
    description: str
    instructions: str
    context: str | None = None
    model: str = "gpt-5.4-mini"
    maxTurns: int = 30
    callbackUrl: str | None = None


class DroneTaskResponse(BaseModel):
    taskId: str
    status: str


class DroneTaskResult(BaseModel):
    taskId: str
    status: str
    description: str | None = None
    output: str | None = None
    tokenUsage: dict | None = None
    costUsd: float | None = None
    duration: float | None = None
    error: str | None = None


# ── Extracted finding (LLM extraction output) ───────────────────

class ExtractedFinding(BaseModel):
    content: str
    location_text: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    category: str | None = None
    raw_excerpt: str | None = None


class ExtractionResult(BaseModel):
    findings: list[ExtractedFinding]
    extraction_notes: str | None = None


# ── Spatial context ──────────────────────────────────────────────

class NearbyFact(BaseModel):
    id: UUID
    content: str
    category: str | None
    confidence: float
    distance_m: float
    valid_at: datetime


class Contradiction(BaseModel):
    old_fact_id: UUID
    old_content: str
    new_fact_id: UUID
    new_content: str
    invalidated_at: datetime
    distance_m: float


class SpatialContext(BaseModel):
    nearby_facts: list[NearbyFact]
    recent_contradictions: list[Contradiction]
    coverage_gaps: list[str]  # cell IDs
    context_text: str  # pre-rendered text block for prompt injection


# ── Drift ────────────────────────────────────────────────────────

class DriftStatus(BaseModel):
    cell_id: str
    staleness_score: float
    contradiction_rate: float
    centroid_drift: float
    fact_count: int
    active_fact_count: int
    last_ingested_at: datetime | None


class DriftEvent(BaseModel):
    id: UUID
    event_type: str
    severity: str
    cell_id: str | None
    metric_value: float | None
    threshold: float | None
    details: dict | None
    detected_at: datetime | None = None
    resolved_at: datetime | None = None


class KnowledgeFact(BaseModel):
    id: UUID
    content: str
    category: str | None
    location_text: str | None
    lat: float | None
    lon: float | None
    confidence: float
    valid_at: datetime
    invalid_at: datetime | None
    invalidation_reason: str | None
    drone_task_id: str
    run_id: UUID
    distance_m: float | None = None


# ── Health ───────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    db: str
    drone: str
    timestamp: datetime
