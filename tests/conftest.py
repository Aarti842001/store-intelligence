"""Shared test fixtures. We force a throwaway sqlite db and turn autoseed off so
each test starts from a clean, known state -- no postgres needed to run `pytest`.
"""

import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

# must be set before any `app` import so the engine binds to the temp db
os.environ["AUTOSEED"] = "0"
_fd, _path = tempfile.mkstemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{_path}"
os.environ["POS_CSV"] = "data/pos_test.csv"

import pytest
from fastapi.testclient import TestClient


def ts_ago(minutes=0, seconds=0):
    """ISO-8601 UTC timestamp `minutes`/`seconds` before now -- keeps test
    events inside the rolling metrics window regardless of when the suite runs."""
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes, seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_event(**kw):
    """minimal valid event with sensible defaults; override any field."""
    ev = {
        "event_id": kw.get("event_id", uuid.uuid4().hex),
        "store_id": kw.get("store_id", "STORE_T"),
        "camera_id": kw.get("camera_id", "CAM1"),
        "visitor_id": kw.get("visitor_id", "VIS_1"),
        "event_type": kw.get("event_type", "ENTRY"),
        "timestamp": kw.get("timestamp", ts_ago(minutes=10)),
        "zone_id": kw.get("zone_id"),
        "dwell_ms": kw.get("dwell_ms", 0),
        "is_staff": kw.get("is_staff", False),
        "confidence": kw.get("confidence", 0.9),
        "metadata": {"queue_depth": kw.get("queue_depth"),
                     "sku_zone": kw.get("zone_id"),
                     "session_seq": kw.get("seq", 0)},
    }
    return ev


def write_pos(rows):
    """rows: [(store_id, 'DD-MM-YYYY', 'HH:MM:SS', amount)]"""
    import csv
    with open("data/pos_test.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "order_date", "order_time", "store_id",
                    "product_id", "brand_name", "total_amount"])
        for i, (store, d, t, amt) in enumerate(rows):
            w.writerow([i, d, t, store, "P", "Purplle", amt])
    from app.pos import load_pos
    load_pos.cache_clear()


@pytest.fixture
def client():
    from app.db import Event, SessionLocal, init_db
    from app.main import app
    init_db()
    s = SessionLocal()
    s.query(Event).delete()
    s.commit()
    s.close()
    write_pos([])                       # empty pos unless a test sets its own
    with TestClient(app) as c:
        yield c
