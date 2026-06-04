# PROMPT: "Write pytest tests for a /stores/{id}/funnel endpoint returning stages
#   entry -> zone_visit -> billing_queue -> purchase with counts and drop_off_pct.
#   The unit is a visitor session; a customer who exits and re-enters (REENTRY,
#   same visitor_id) must not be counted twice at the entry stage."
# CHANGES MADE: asserted the first stage's drop_off_pct is null (no prior stage)
#   which the generated version had as 0.0, and added the explicit re-entry
#   double-count guard the prompt described but didn't actually test.

from tests.conftest import make_event


def test_funnel_stage_counts_and_dropoff(client):
    client.post("/events/ingest", json={"events": [
        make_event(visitor_id="A", event_type="ENTRY"),
        make_event(visitor_id="B", event_type="ENTRY"),
        make_event(visitor_id="A", event_type="ZONE_ENTER", zone_id="SKINCARE"),
        make_event(visitor_id="A", event_type="BILLING_QUEUE_JOIN",
                   zone_id="BILLING", queue_depth=1),
    ]})
    f = client.get("/stores/STORE_T/funnel").json()["stages"]
    by = {s["stage"]: s for s in f}
    assert by["entry"]["count"] == 2
    assert by["entry"]["drop_off_pct"] is None        # no prior stage
    assert by["zone_visit"]["count"] == 1
    assert by["billing_queue"]["count"] == 1


def test_reentry_not_double_counted(client):
    # same visitor_id enters, exits, re-enters -> still ONE entry-stage visitor
    client.post("/events/ingest", json={"events": [
        make_event(visitor_id="A", event_type="ENTRY"),
        make_event(visitor_id="A", event_type="EXIT"),
        make_event(visitor_id="A", event_type="REENTRY"),
    ]})
    f = client.get("/stores/STORE_T/funnel").json()["stages"]
    entry = next(s for s in f if s["stage"] == "entry")
    assert entry["count"] == 1
