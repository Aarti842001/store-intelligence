"""Replay a detection output file (events.jsonl) into the API.

Two modes:
  --batch     fire everything in chunks of 500 as fast as possible (back-fill)
  --realtime  pace events by their own timestamps, scaled by --speed, so the
              dashboard updates live -- this is the Part E "genuinely connected"
              proof, not a batch dump.

Usage:
  python scripts/replay.py events.jsonl --realtime --speed 60
  python scripts/replay.py events.jsonl --batch
"""

import argparse
import json
import time
from datetime import datetime

import urllib.request

CHUNK = 500


def _post(api, batch):
    req = urllib.request.Request(
        f"{api}/events/ingest", method="POST",
        data=json.dumps({"events": batch}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def _ts(e):
    return datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))


def replay(path, api, realtime, speed):
    events = [json.loads(l) for l in open(path) if l.strip()]
    events.sort(key=_ts)
    sent = 0

    if not realtime:
        for i in range(0, len(events), CHUNK):
            r = _post(api, events[i:i + CHUNK])
            sent += r["ingested"]
            print(f"  +{r['ingested']} ingested ({r['duplicates']} dup, {len(r['failed'])} bad)")
        print(f"done: {sent} events")
        return

    # real-time: sleep the inter-event gap, divided by speed
    t0 = _ts(events[0])
    wall0 = time.time()
    for e in events:
        target = (_ts(e) - t0).total_seconds() / max(speed, 1e-6)
        slack = target - (time.time() - wall0)
        if slack > 0:
            time.sleep(min(slack, 5))       # cap so huge gaps don't stall forever
        _post(api, [e])
        sent += 1
        print(f"  [{sent}/{len(events)}] {e['event_type']:<20} {e.get('store_id')}")
    print(f"done: streamed {sent} events")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--realtime", action="store_true")
    ap.add_argument("--batch", dest="realtime", action="store_false")
    ap.add_argument("--speed", type=float, default=60.0, help="realtime time-compression")
    ap.set_defaults(realtime=True)
    a = ap.parse_args()
    replay(a.file, a.api, a.realtime, a.speed)
