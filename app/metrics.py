"""Real-time store metrics.

Everything here is computed on read against the events table -- no nightly
rollups, no cache -- so a freshly ingested event shows up immediately. Staff
events are excluded from every customer-facing number.

An honest note on conversion that runs through this whole module: our cameras
aren't time-synced and each emits its own visitor ids, so we can't link a
specific billing-zone visitor back to a specific entry visitor. We therefore
follow the brief's rule literally -- a visitor seen in the billing zone within
the POS window before a transaction is "converted" -- and express conversion as
a store/window ratio (converted billing visitors / unique entry visitors). The
production fix (cross-camera Re-ID) is called out in DESIGN.md.
"""

from datetime import datetime, timedelta
from app.clock import utcnow

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Event
from app.pos import pos_for


def _window(hours=None):
    end = utcnow()
    start = end - timedelta(hours=hours or settings.metrics_window_h)
    return start, end


def _q(db, store_id, start, end, staff=False):
    return (select(Event).where(Event.store_id == store_id)
            .where(Event.ts >= start).where(Event.ts <= end)
            .where(Event.is_staff == staff))


def unique_visitors(db: Session, store_id, start, end) -> int:
    stmt = (select(func.count(func.distinct(Event.visitor_id)))
            .where(Event.store_id == store_id, Event.ts.between(start, end),
                   Event.is_staff == False,                       # noqa: E712
                   Event.event_type.in_(["ENTRY", "REENTRY"])))
    return db.execute(stmt).scalar() or 0


def _billing_visitors(db: Session, store_id, start, end):
    """(visitor_id, ts) for non-staff people seen in the billing zone."""
    stmt = (select(Event.visitor_id, Event.ts)
            .where(Event.store_id == store_id, Event.ts.between(start, end),
                   Event.is_staff == False,                       # noqa: E712
                   Event.event_type.in_(["BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"])))
    return db.execute(stmt).all()


def converted_visitors(db: Session, store_id, start, end) -> set:
    """billing-zone visitors who had a POS txn within the window after them."""
    win = timedelta(seconds=settings.pos_correlation_window_s)
    txns = pos_for(store_id, start - win, end + win)
    if not txns:
        return set()
    converted = set()
    for vid, ts in _billing_visitors(db, store_id, start, end):
        if vid is None:
            continue
        if any(ts <= t and t - ts <= win for _, t, _ in txns):
            converted.add(vid)
    return converted


def avg_dwell_by_zone(db: Session, store_id, start, end) -> dict:
    stmt = (select(Event.zone_id, func.avg(Event.dwell_ms))
            .where(Event.store_id == store_id, Event.ts.between(start, end),
                   Event.is_staff == False,                       # noqa: E712
                   Event.event_type.in_(["ZONE_EXIT", "ZONE_DWELL"]),
                   Event.zone_id.isnot(None), Event.dwell_ms > 0)
            .group_by(Event.zone_id))
    return {z: round(float(d), 1) for z, d in db.execute(stmt).all()}


def current_queue_depth(db: Session, store_id, start, end) -> int:
    stmt = (select(Event.queue_depth)
            .where(Event.store_id == store_id, Event.ts.between(start, end),
                   Event.event_type == "BILLING_QUEUE_JOIN",
                   Event.queue_depth.isnot(None))
            .order_by(Event.ts.desc()).limit(1))
    return db.execute(stmt).scalar() or 0


def abandonment_rate(db: Session, store_id, start, end) -> float:
    def count(t):
        return db.execute(select(func.count()).where(
            Event.store_id == store_id, Event.ts.between(start, end),
            Event.is_staff == False, Event.event_type == t)).scalar() or 0   # noqa: E712
    joined = count("BILLING_QUEUE_JOIN")
    abandoned = count("BILLING_QUEUE_ABANDON")
    denom = joined + abandoned
    return round(abandoned / denom, 4) if denom else 0.0


def store_metrics(db: Session, store_id: str) -> dict:
    start, end = _window()
    visitors = unique_visitors(db, store_id, start, end)
    converted = converted_visitors(db, store_id, start, end)
    conv = round(len(converted) / visitors, 4) if visitors else 0.0
    return {
        "store_id": store_id,
        "window_start": start.isoformat() + "Z",
        "window_end": end.isoformat() + "Z",
        "unique_visitors": visitors,
        "converted_visitors": len(converted),
        "conversion_rate": min(conv, 1.0),
        "avg_dwell_ms_by_zone": avg_dwell_by_zone(db, store_id, start, end),
        "current_queue_depth": current_queue_depth(db, store_id, start, end),
        "abandonment_rate": abandonment_rate(db, store_id, start, end),
    }
