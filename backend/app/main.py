from __future__ import annotations

import json
import os
from uuid import uuid4

import httpx
import psycopg
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row

from .contracts import RunRequest
from .orchestrator import persist, run_pipeline

API_DATABASE_URL = os.getenv(
    "API_DATABASE_URL",
    "postgresql://policyproof_api:api-dev-only@localhost:5432/policyproof",
)
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b-instruct")
TARGET_URL = os.getenv("TARGET_URL", "http://localhost:8001").rstrip("/")

app = FastAPI(title="Policy-to-Proof API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.on_event("startup")
def initialize_platform_store() -> None:
    with psycopg.connect(API_DATABASE_URL) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS platform AUTHORIZATION policyproof_api")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS platform.runs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                result JSONB NOT NULL DEFAULT '{}'::jsonb,
                error TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )"""
        )


@app.get("/api/health")
async def health() -> dict:
    checks = {"database": "unavailable", "target": "unavailable", "ollama": "unavailable"}
    try:
        with psycopg.connect(API_DATABASE_URL) as conn:
            conn.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception:
        pass
    async with httpx.AsyncClient(timeout=2.0) as client:
        for name, url in (
            ("target", f"{TARGET_URL}/health"),
            ("ollama", f"{OLLAMA_BASE_URL}/api/tags"),
        ):
            try:
                response = await client.get(url)
                response.raise_for_status()
                if name == "ollama":
                    installed = {
                        item.get("name")
                        for item in response.json().get("models", [])
                    }
                    if OLLAMA_MODEL not in installed:
                        continue
                checks[name] = "ok"
            except Exception:
                pass
    status = "ok" if all(value == "ok" for value in checks.values()) else "degraded"
    return {"status": status, "checks": checks, "model": OLLAMA_MODEL}


@app.post("/api/runs")
def create_run(request: RunRequest, background_tasks: BackgroundTasks) -> dict:
    run_id = str(uuid4())
    persist(run_id, "queued", "queued", {"mode": request.mode})
    background_tasks.add_task(run_pipeline, run_id, request.mode)
    return {"id": run_id, "status": "queued", "mode": request.mode}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    with psycopg.connect(API_DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT id, status, stage, result, error, updated_at "
            "FROM platform.runs WHERE id=%s",
            (run_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
    return {**dict(row), "result": result}
