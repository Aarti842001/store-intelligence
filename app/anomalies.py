"""Operational anomalies an on-call/store manager actually cares about.

Three detectors, each returning a severity and a concrete suggested_action so
the output is actionable, not just a flag:
  - queue spike     : billing queue deeper than the configured threshold
  - conversion drop : today's conversion well below the trailing 7-day mean
  - dead zone       : a zone with zero visits for the dead-zone window

Conversion drop needs history; with under ~2 days of data we say so (INFO)
rather than cry wolf off a one-day baseline.
"""

from datetime import datetime, timedelta
from app.clock import utcnow

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Event
from app.metrics import (converted_visitors, current_queue_depth,
                         unique_visitors)


def _anom(kind, severity, detail, action, **extra):
    return {"type": kind, "severity": severity, "detail": detail,
            "suggested_action": action, **extra}


def _conversion(db, store_id, start, end):
    v = unique_visitors(db, store_id, start, end)
    return (len(converted_visitors(db, store_id, start, end)) / v) if v else None


def detect(db: Session, store_id: str) -> list[dict]:
    now = utcnow()
    out = []

    # 1) queue spike -----------------------------------------------------
    depth = current_queue_depth(db, store_id, now - timedelta(hours=1), now)
    if depth >= settings.queue_spike_depth:
        sev = "CRITICAL" if depth >= settings.queue_spike_depth + 3 else "WARN"
        out.append(_anom("BILLING_QUEUE_SPIKE", sev,
                         f"billing queue depth is {depth}",
                         "open a second till / call floor staff to billing",
                         queue_depth=depth))

    # 2) conversion drop vs 7-day mean -----------------------------------
    today = _conversion(db, store_id, now - timedelta(hours=24), now)
    base_vals = []
    for d in range(1, 8):
        s = now - timedelta(days=d + 1)
        e = now - timedelta(days=d)
        c = _conversion(db, store_id, s, e)
        if c is not None:
            base_vals.append(c)
    if today is not None and len(base_vals) >= 2:
        baseline = sum(base_vals) / len(base_vals)
        if baseline and today < 0.7 * baseline:
            out.append(_anom("CONVERSION_DROP", "WARN",
                             f"conversion {today:.0%} vs 7-day avg {baseline:.0%}",
                             "check staffing, queue length and stock on hot zones",
                             today=round(today, 4), baseline=round(baseline, 4)))
    elif today is not None:
        out.append(_anom("CONVERSION_DROP", "INFO",
                         "not enough history for a 7-day conversion baseline yet",
                         "no action; baseline will populate as data accrues"))

    # 3) dead zone -------------------------------------------------------
    # only product zones (the ones tracked via ZONE_ENTER); BILLING is a queue
    # zone with its own events, not a browse zone, so it never counts as "dead".
    known = db.execute(
        select(func.distinct(Event.zone_id)).where(
            Event.store_id == store_id, Event.event_type == "ZONE_ENTER",
            Event.zone_id.isnot(None))).all()
    cutoff = now - timedelta(seconds=settings.dead_zone_s)
    for (zone,) in known:
        recent = db.execute(select(func.count()).where(
            Event.store_id == store_id, Event.zone_id == zone,
            Event.ts >= cutoff, Event.event_type == "ZONE_ENTER")).scalar() or 0
        if recent == 0:
            out.append(_anom("DEAD_ZONE", "INFO",
                             f"no visits to {zone} in the last "
                             f"{settings.dead_zone_s // 60} min",
                             "check lighting/merchandising or camera health",
                             zone_id=zone))
    return out
