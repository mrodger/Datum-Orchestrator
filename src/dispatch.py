"""Drone dispatch — POST tasks to the drone API and poll for results."""

from __future__ import annotations

import asyncio
import os

import httpx

from .models import DroneDispatchPayload, DroneTaskResponse, DroneTaskResult

DRONE_URL = os.environ.get("DRONE_API_URL", "http://host.docker.internal:3010")
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=DRONE_URL, timeout=30.0)
    return _client


async def dispatch(payload: DroneDispatchPayload) -> DroneTaskResponse:
    """Submit a task to the drone. Returns taskId + status."""
    client = _get_client()
    resp = await client.post("/task", json=payload.model_dump())
    resp.raise_for_status()
    return DroneTaskResponse(**resp.json())


async def get_task(task_id: str) -> DroneTaskResult:
    """Get current status + output for a drone task."""
    client = _get_client()
    resp = await client.get(f"/task/{task_id}")
    resp.raise_for_status()
    return DroneTaskResult(**resp.json())


async def poll_until_done(
    task_id: str,
    interval: float = 5.0,
    timeout: float = 600.0,
) -> DroneTaskResult:
    """Poll drone task until terminal state. Returns final result."""
    elapsed = 0.0
    while elapsed < timeout:
        result = await get_task(task_id)
        if result.status in ("complete", "failed", "cancelled"):
            return result
        await asyncio.sleep(interval)
        elapsed += interval
    raise TimeoutError(f"Drone task {task_id} did not complete within {timeout}s")


async def check_drone_health() -> str:
    """Returns 'ok' or error message."""
    try:
        client = _get_client()
        resp = await client.get("/health", timeout=5.0)
        if resp.status_code == 200:
            return "ok"
        return f"status {resp.status_code}"
    except Exception as e:
        return str(e)
