# PROMPT: "Write pytest tests for a /stores/{id}/metrics endpoint. Cover: staff
#   events excluded from unique_visitors; conversion_rate computed from POS within
#   a 5-min window of a billing-zone visit; a zero-traffic store returns zeros not
#   nulls; an all-staff clip yields zero customers."
# CHANGES MADE: had to seed a POS row via the write_pos helper for the conversion
#   case (the AI version assumed POS was already loaded), and asserted
#   conversion_rate is a float 0.0 rather than None for the empty store.

from tests.conftest import make_event, write_pos, ts_ago


def test_staff_excluded_from_visitors(client):
    client.post("/events/ingest", json={"events": [
        make_event(visitor_id="C1", event_type="ENTRY"),
        make_event(visitor_id="S1", event_type="ENTRY", is_staff=True),
    ]})
    m = client.get("/stores/STORE_T/metrics").json()
    assert m["unique_visitors"] == 1            # staff not counted


def test_zero_traffic_returns_zeros(client):
    m = client.get("/stores/NOPE/metrics").json()
    assert m["unique_visitors"] == 0
    assert m["conversion_rate"] == 0.0          # a float, never null
    assert m["avg_dwell_ms_by_zone"] == {}


def test_all_staff_clip_has_no_customers(client):
    client.post("/events/ingest", json={"events": [
        make_event(visitor_id="S1", is_staff=True),
        make_event(visitor_id="S2", is_staff=True, event_type="REENTRY"),
    ]})
    m = client.get("/stores/STORE_T/metrics").json()
    assert m["unique_visitors"] == 0
    assert m["conversion_rate"] == 0.0


def test_conversion_with_pos(client):
    # billing 9 min ago, POS txn 8 min ago -> within the 5-min correlation window
    bill = ts_ago(minutes=9)
    from datetime import datetime, timezone
    pos_dt = datetime.strptime(ts_ago(minutes=8), "%Y-%m-%dT%H:%M:%SZ")
    write_pos([("STORE_T", pos_dt.strftime("%d-%m-%Y"), pos_dt.strftime("%H:%M:%S"), 500.0)])
    client.post("/events/ingest", json={"events": [
        make_event(visitor_id="C1", event_type="ENTRY", timestamp=ts_ago(minutes=10)),
        make_event(visitor_id="C1", event_type="BILLING_QUEUE_JOIN",
                   zone_id="BILLING", queue_depth=1, timestamp=bill),
    ]})
    m = client.get("/stores/STORE_T/metrics").json()
    assert m["unique_visitors"] == 1
    assert m["converted_visitors"] == 1          # POS within 5 min of billing
    assert m["conversion_rate"] == 1.0


def test_demo_survives_restart_conversion_stays_nonzero():
    # PROMPT: "Write a regression test for this bug: the auto-seeded demo writes a
    #   POS file and points settings.pos_csv at it only during seeding. On a plain
    #   container restart the events persist, seeding is skipped, the pointer falls
    #   back to the static sample (old dates), and conversion silently drops to 0.
    #   Assert conversion stays non-zero across a simulated restart."
    # CHANGES MADE: wrote it as a direct seed_demo()/metrics call (not via the
    #   client fixture, which runs with AUTOSEED=0), and restore settings.pos_csv
    #   in a finally block so it can't leak into other tests.
    import app.config as cfg
    import app.pos as pos
    from app import metrics
    from app.db import Event, SessionLocal, init_db
    from app.metrics import _window
    from app.seed import STORE, seed_demo

    original = cfg.settings.pos_csv
    init_db()
    try:
        seed_demo(reset=True)                          # first boot
        db = SessionLocal()
        conv1 = len(metrics.converted_visitors(db, STORE, *_window()))
        db.close()
        assert conv1 > 0

        # simulate a fresh process after `docker compose restart`: the in-memory
        # pointer is gone, so it falls back to the static (old-dated) sample
        cfg.settings.pos_csv = "data/pos_transactions.csv"
        pos.load_pos.cache_clear()

        seed_demo(reset=True)                          # boot again, as _maybe_seed does
        db = SessionLocal()
        conv2 = len(metrics.converted_visitors(db, STORE, *_window()))
        db.close()
        assert conv2 > 0, "conversion collapsed to zero after a restart"
        assert cfg.settings.pos_csv.endswith("pos_demo.csv")
    finally:
        cfg.settings.pos_csv = original
        pos.load_pos.cache_clear()
        db = SessionLocal()
        db.query(Event).filter(Event.store_id == STORE).delete()
        db.commit()
        db.close()
