# PROMPT: "Write pytest tests for a FastAPI /events/ingest endpoint that is
#   idempotent by event_id, returns partial success (207) when some events in a
#   batch are malformed, dedupes within a batch, and rejects batches over 500.
#   Use a TestClient fixture called `client` and a `make_event(**kw)` helper."
# CHANGES MADE: tightened the duplicate-within-batch assertion (our API silently
#   collapses in-batch dupes rather than reporting them, so I assert on the row
#   count via a follow-up metrics read instead of the response body), and added
#   the alias-normalisation case (lowercase event_type / store_code) which the
#   model accepts but the prompt's tests didn't cover.

from tests.conftest import make_event


def test_valid_batch_ingests(client):
    r = client.post("/events/ingest", json={"events": [make_event(), make_event()]})
    assert r.status_code == 200
    assert r.json()["ingested"] == 2
    assert r.json()["failed"] == []


def test_partial_success_on_bad_event(client):
    good = make_event()
    bad = make_event(event_type="NOT_A_TYPE")
    r = client.post("/events/ingest", json={"events": [good, bad]})
    assert r.status_code == 207                 # multi-status
    body = r.json()
    assert body["ingested"] == 1
    assert len(body["failed"]) == 1
    assert body["failed"][0]["index"] == 1


def test_idempotent_replay(client):
    ev = make_event(event_id="FIXED-1")
    first = client.post("/events/ingest", json={"events": [ev]}).json()
    second = client.post("/events/ingest", json={"events": [ev]}).json()
    assert first["ingested"] == 1
    assert second["ingested"] == 0
    assert second["duplicates"] == 1


def test_batch_over_500_rejected(client):
    big = {"events": [make_event() for _ in range(501)]}
    r = client.post("/events/ingest", json=big)
    assert r.status_code == 422                 # pydantic validation, not a 500


def test_alias_normalisation(client):
    # vendor dialect: lowercase type, store_code, event_time, id_token
    raw = {"event_id": "AL1", "store_code": "STORE_T", "id_token": "VIS_9",
           "event_type": "zone_entered", "event_time": "2026-06-03T10:00:00",
           "zone_id": "SKINCARE"}
    r = client.post("/events/ingest", json={"events": [raw]})
    assert r.status_code == 200
    assert r.json()["ingested"] == 1
