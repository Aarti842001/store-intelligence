# PROMPT: "Write pytest tests for /stores/{id}/heatmap (normalised 0-100 visit
#   and dwell scores per zone, plus a data_confidence flag that is false when
#   there are fewer than 20 sessions) and a smoke test for the demo seed builder."
# CHANGES MADE: asserted the top zone normalises to exactly 100.0 (the AI version
#   only checked it was <=100), and pointed the seed smoke test at build_demo()
#   directly so it doesn't need a live DB.

from tests.conftest import make_event


def test_heatmap_scores_and_confidence(client):
    client.post("/events/ingest", json={"events": [
        make_event(visitor_id="A", event_type="ZONE_ENTER", zone_id="SKINCARE"),
        make_event(visitor_id="B", event_type="ZONE_ENTER", zone_id="SKINCARE"),
        make_event(visitor_id="C", event_type="ZONE_ENTER", zone_id="MAKEUP"),
        make_event(visitor_id="A", event_type="ZONE_EXIT", zone_id="SKINCARE", dwell_ms=40000),
    ]})
    h = client.get("/stores/STORE_T/heatmap").json()
    assert h["data_confidence"] is False            # 3 sessions < 20
    top = h["zones"][0]
    assert top["zone_id"] == "SKINCARE"             # most visited
    assert top["visit_score"] == 100.0              # normalised peak


def test_seed_builder_shape():
    from app.seed import build_demo
    events, pos = build_demo()
    types = {e["event_type"] for e in events}
    assert {"ENTRY", "REENTRY", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"} <= types
    assert any(e["is_staff"] for e in events)        # staff present (to be excluded)
    assert len(pos) == 4                             # four converting customers
