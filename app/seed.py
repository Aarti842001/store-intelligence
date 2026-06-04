"""Generates a small, coherent demo dataset for STORE_BLR_002 and seeds it on
first boot, so the API answers with real numbers immediately (and the
acceptance-gate endpoint isn't just an empty store).

It is deliberately hand-shaped to exercise the things the brief grades:
  - 12 customers, 2 staff (staff must be excluded from metrics)
  - one customer who exits and comes back -> REENTRY (no double count)
  - 6 reach billing; 4 convert (POS follows), 2 abandon (no POS)
  - a quiet gap with no events (zero-traffic handling)
Everything is anchored to 'now' so /health reads live and the 24h window catches
it. We write a matching POS file rather than touch the provided sample.
"""

import csv
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import Event, SessionLocal
from app.ingestion import ingest_events

STORE = "STORE_BLR_002"
ZONES = ["SKINCARE", "HAIRCARE", "MAKEUP", "FRAGRANCE"]


def _ev(store, cam, vid, etype, ts, **kw):
    md = {"queue_depth": kw.get("queue_depth"),
          "sku_zone": kw.get("zone_id"),
          "session_seq": kw.get("seq", 0)}
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store, "camera_id": cam, "visitor_id": vid,
        "event_type": etype, "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "zone_id": kw.get("zone_id"), "dwell_ms": kw.get("dwell_ms", 0),
        "is_staff": kw.get("is_staff", False), "confidence": kw.get("conf", 0.9),
        "metadata": md,
    }


def build_demo(now=None):
    """returns (events, pos_rows) anchored so the newest event is ~2 min ago."""
    rng = random.Random(7)
    now = now or datetime.now(timezone.utc)
    base = now - timedelta(minutes=30)
    events, pos = [], []

    # 2 staff drifting through zones + billing (must be excluded everywhere)
    for s in ("STAFF_01", "STAFF_02"):
        t = base + timedelta(seconds=rng.randint(0, 200))
        events.append(_ev(STORE, "CAM2", s, "ZONE_ENTER", t, zone_id=rng.choice(ZONES),
                          is_staff=True, seq=1))
        events.append(_ev(STORE, "CAM6", s, "BILLING_QUEUE_JOIN",
                          t + timedelta(minutes=2), zone_id="BILLING",
                          queue_depth=1, is_staff=True, seq=2))

    billing_plan = {  # vid -> "convert" | "abandon" | None
        "VIS_0001": "convert", "VIS_0002": "convert", "VIS_0004": "convert",
        "VIS_0005": "convert", "VIS_0007": "abandon", "VIS_0009": "abandon",
    }
    queue_depth = 0
    for n in range(1, 13):
        vid = f"VIS_{n:04d}"
        enter = base + timedelta(seconds=90 * n + rng.randint(0, 40))
        # leave a quiet ~5 min gap in the middle (customers 6-7 spaced out)
        if n >= 7:
            enter += timedelta(minutes=5)
        seq = 1
        events.append(_ev(STORE, "CAM1", vid, "ENTRY", enter, seq=seq, conf=rng.uniform(.7, .97)))

        # browse 1-2 zones, sometimes long enough to dwell
        for _ in range(rng.randint(1, 2)):
            z = rng.choice(ZONES)
            zin = enter + timedelta(seconds=rng.randint(10, 40))
            dwell = rng.randint(8000, 55000)
            seq += 1
            events.append(_ev(STORE, "CAM2", vid, "ZONE_ENTER", zin, zone_id=z, seq=seq))
            if dwell >= 30000:
                seq += 1
                events.append(_ev(STORE, "CAM2", vid, "ZONE_DWELL",
                                  zin + timedelta(seconds=30), zone_id=z,
                                  dwell_ms=30000, seq=seq))
            seq += 1
            events.append(_ev(STORE, "CAM2", vid, "ZONE_EXIT",
                              zin + timedelta(milliseconds=dwell), zone_id=z,
                              dwell_ms=dwell, seq=seq))

        plan = billing_plan.get(vid)
        if plan:
            queue_depth += 1
            bt = enter + timedelta(seconds=rng.randint(120, 240))
            seq += 1
            events.append(_ev(STORE, "CAM6", vid, "BILLING_QUEUE_JOIN", bt,
                              zone_id="BILLING", queue_depth=queue_depth, seq=seq))
            if plan == "convert":
                pos.append((STORE, bt + timedelta(seconds=70), round(rng.uniform(300, 1800), 2)))
                queue_depth = max(0, queue_depth - 1)
            else:  # abandoned -> leaves with no POS following
                seq += 1
                events.append(_ev(STORE, "CAM6", vid, "BILLING_QUEUE_ABANDON",
                                  bt + timedelta(seconds=rng.randint(60, 120)),
                                  zone_id="BILLING", dwell_ms=90000,
                                  queue_depth=queue_depth, seq=seq))
                queue_depth = max(0, queue_depth - 1)

        # one customer steps out and returns -> REENTRY, same visitor_id
        if vid == "VIS_0003":
            ex = enter + timedelta(seconds=60)
            events.append(_ev(STORE, "CAM1", vid, "EXIT", ex, seq=seq + 1))
            events.append(_ev(STORE, "CAM1", vid, "REENTRY",
                              ex + timedelta(seconds=40), seq=seq + 2, conf=.82))

    events.sort(key=lambda e: e["timestamp"])
    return events, pos


def _write_pos(pos_rows):
    """write a demo POS csv (original sample rows + our STORE_BLR_002 rows) and
    point settings at it, without mutating the provided sample."""
    demo = "data/pos_demo.csv"
    header = ["order_id", "order_date", "order_time", "store_id",
              "product_id", "brand_name", "total_amount"]
    rows = []
    if os.path.exists(settings.pos_csv):
        with open(settings.pos_csv, newline="") as f:
            rows = list(csv.reader(f))[1:]
    oid = 100000
    extra = []
    for store, ts, amt in pos_rows:
        oid += 1
        extra.append([oid, ts.strftime("%d-%m-%Y"), ts.strftime("%H:%M:%S"),
                      store, "DEMO", "Purplle", amt])
    with open(demo, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows + extra)
    settings.pos_csv = demo
    from app.pos import load_pos
    load_pos.cache_clear()


def _use_demo_pos_if_present():
    """Point POS correlation at the demo file if it's on disk. settings.pos_csv
    is only mutated in-process by _write_pos, so after a plain restart (where we
    don't re-seed) the pointer would otherwise fall back to the static sample and
    conversion would read zero. This keeps it correct."""
    demo = "data/pos_demo.csv"
    if os.path.exists(demo):
        settings.pos_csv = demo
        from app.pos import load_pos
        load_pos.cache_clear()


def seed_demo(db=None, reset=False) -> int:
    """Seed the demo store. With reset=True we wipe this store's events first and
    regenerate everything anchored to 'now' -- so a restart gives a fresh, live
    feed and the POS file always matches the billing events. Without reset, we
    leave existing data alone (e.g. replayed real events) and only make sure the
    POS pointer is right."""
    db = db or SessionLocal()
    if reset:
        db.query(Event).filter(Event.store_id == STORE).delete()
        db.commit()
    elif db.query(Event).count() > 0:
        _use_demo_pos_if_present()
        return 0
    events, pos = build_demo()
    _write_pos(pos)
    res = ingest_events(db, events)
    return res.ingested


if __name__ == "__main__":   # allow: python -m app.seed
    from app.db import init_db
    init_db()
    print("seeded", seed_demo(), "events")
