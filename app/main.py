"""FastAPI entrypoint -- wires the endpoints, structured request logging, and
graceful degradation, and seeds a demo dataset on first boot so the API is
immediately useful after `docker compose up`.
"""

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app import anomalies, funnel, health, heatmap, metrics
from app.config import settings
from app.db import Event, get_db, init_db
from app.ingestion import ingest_events
from app.models import IngestRequest

log = logging.getLogger("store_intel")
logging.basicConfig(level=logging.INFO, format="%(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _maybe_seed()
    yield


app = FastAPI(title="Store Intelligence API", version="1.0", lifespan=lifespan)


# --- structured request logging -------------------------------------------
@app.middleware("http")
async def access_log(request: Request, call_next):
    trace_id = request.headers.get("x-trace-id", uuid.uuid4().hex[:12])
    request.state.trace_id = trace_id
    t0 = time.perf_counter()
    response = await call_next(request)
    line = {
        "trace_id": trace_id,
        "store_id": request.path_params.get("store_id"),
        "endpoint": request.url.path,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "event_count": getattr(request.state, "event_count", None),
        "status_code": response.status_code,
    }
    log.info(json.dumps(line))
    response.headers["x-trace-id"] = trace_id
    return response


# --- graceful degradation: db errors become a clean 503, never a stack trace
@app.exception_handler(OperationalError)
@app.exception_handler(SQLAlchemyError)
async def db_down(request: Request, exc):
    return JSONResponse(status_code=503, content={
        "error": "database_unavailable",
        "detail": "the storage backend is not reachable; retry shortly",
        "trace_id": getattr(request.state, "trace_id", None),
    })


@app.exception_handler(Exception)
async def unhandled(request: Request, exc):
    log.error(json.dumps({"trace_id": getattr(request.state, "trace_id", None),
                          "error": type(exc).__name__, "detail": str(exc)[:200]}))
    return JSONResponse(status_code=500, content={
        "error": "internal_error", "trace_id": getattr(request.state, "trace_id", None)})


# --- endpoints -------------------------------------------------------------
@app.post("/events/ingest")
async def ingest(req: IngestRequest, request: Request, db: Session = Depends(get_db)):
    request.state.event_count = len(req.events)
    result = ingest_events(db, req.events)
    code = 207 if result.failed else 200      # multi-status when partially bad
    return JSONResponse(status_code=code, content=result.model_dump())


@app.get("/stores/{store_id}/metrics")
async def get_metrics(store_id: str, db: Session = Depends(get_db)):
    return metrics.store_metrics(db, store_id)


@app.get("/stores/{store_id}/funnel")
async def get_funnel(store_id: str, db: Session = Depends(get_db)):
    return funnel.funnel(db, store_id)


@app.get("/stores/{store_id}/heatmap")
async def get_heatmap(store_id: str, db: Session = Depends(get_db)):
    return heatmap.heatmap(db, store_id)


@app.get("/stores/{store_id}/anomalies")
async def get_anomalies(store_id: str, db: Session = Depends(get_db)):
    return {"store_id": store_id, "anomalies": anomalies.detect(db, store_id)}


@app.get("/health")
async def get_health(db: Session = Depends(get_db)):
    return health.health(db)


# --- demo dashboard (Part E) ----------------------------------------------
if os.path.isdir("dashboard"):
    app.mount("/dashboard", StaticFiles(directory="dashboard", html=True), name="dashboard")


def _maybe_seed():
    """on boot, load a coherent demo dataset for STORE_BLR_002 so every endpoint
    returns real numbers (the acceptance-gate store isn't empty). When autoseed
    is on we reseed this demo store fresh each boot, so the feed stays live and
    the demo POS always matches the seeded billing events. Set AUTOSEED=0 before
    replaying your own detection events so they aren't wiped."""
    if not settings.autoseed:
        return
    from app.seed import seed_demo
    try:
        n = seed_demo(reset=True)
        if n:
            log.info(json.dumps({"seed": "loaded", "events": n}))
    except Exception as e:           # seeding is best-effort, never block boot
        log.error(json.dumps({"seed": "failed", "detail": str(e)[:160]}))
