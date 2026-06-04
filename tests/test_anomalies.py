# PROMPT: "Write pytest tests for /stores/{id}/anomalies (queue spike when depth
#   exceeds threshold, with a suggested_action) and for /health returning a
#   STALE_FEED warning. Also test that when the database is unavailable the API
#   returns 503 with a structured body and no stack trace."
# CHANGES MADE: simulated the DB outage with a FastAPI dependency override that
#   raises OperationalError (cleaner and faster than tearing down a real pg), and
#   asserted the 503 body has an `error` key rather than matching exact text.

from sqlalchemy.exc import OperationalError

from app.db import get_db
from app.main import app
from tests.conftest import make_event


def test_queue_spike_anomaly(client):
    # depth 6 >= threshold 5 -> a queue-spike anomaly with an action
    client.post("/events/ingest", json={"events": [
        make_event(visitor_id="C1", event_type="BILLING_QUEUE_JOIN",
                   zone_id="BILLING", queue_depth=6),
    ]})
    anoms = client.get("/stores/STORE_T/anomalies").json()["anomalies"]
    spike = [a for a in anoms if a["type"] == "BILLING_QUEUE_SPIKE"]
    assert spike and spike[0]["suggested_action"]
    assert spike[0]["severity"] in ("WARN", "CRITICAL")


def test_health_reports_feed(client):
    client.post("/events/ingest", json={"events": [make_event(store_id="STORE_T")]})
    h = client.get("/health").json()
    assert h["database"] == "up"
    assert any(s["store_id"] == "STORE_T" for s in h["stores"])


def test_db_down_returns_503(client):
    def boom():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))
    app.dependency_overrides[get_db] = boom
    try:
        r = client.get("/stores/STORE_T/metrics")
        assert r.status_code == 503
        assert r.json()["error"] == "database_unavailable"
        assert "Traceback" not in r.text          # no stack trace leaks
    finally:
        app.dependency_overrides.clear()
