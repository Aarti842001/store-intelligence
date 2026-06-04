"""Zone heatmap: visit frequency + avg dwell, normalised 0-100 for a grid.

We normalise each metric independently against its own max in the window so the
front end can colour cells directly. data_confidence goes false when the window
has too few sessions to trust the shape (small-sample noise).
"""

from datetime import datetime, timedelta
from app.clock import utcnow

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Event


def _norm(value, peak):
    return round(100 * value / peak, 1) if peak else 0.0


def heatmap(db: Session, store_id: str) -> dict:
    end = utcnow()
    start = end - timedelta(hours=settings.metrics_window_h)

    stmt = (select(Event.zone_id,
                   func.count().label("visits"),
                   func.avg(Event.dwell_ms).label("dwell"))
            .where(Event.store_id == store_id, Event.ts.between(start, end),
                   Event.is_staff == False,                       # noqa: E712
                   Event.event_type == "ZONE_ENTER",
                   Event.zone_id.isnot(None))
            .group_by(Event.zone_id))
    rows = db.execute(stmt).all()

    sessions = db.execute(
        select(func.count(func.distinct(Event.visitor_id)))
        .where(Event.store_id == store_id, Event.ts.between(start, end),
               Event.is_staff == False,                           # noqa: E712
               Event.event_type == "ZONE_ENTER")).scalar() or 0

    peak_visits = max([r.visits for r in rows], default=0)
    # avg dwell per zone needs a separate pass (dwell lives on ZONE_EXIT/DWELL)
    dwell_rows = db.execute(
        select(Event.zone_id, func.avg(Event.dwell_ms))
        .where(Event.store_id == store_id, Event.ts.between(start, end),
               Event.is_staff == False,                           # noqa: E712
               Event.event_type.in_(["ZONE_EXIT", "ZONE_DWELL"]),
               Event.dwell_ms > 0, Event.zone_id.isnot(None))
        .group_by(Event.zone_id)).all()
    dwell_by_zone = {z: float(d) for z, d in dwell_rows}
    peak_dwell = max(dwell_by_zone.values(), default=0)

    zones = []
    for r in rows:
        zones.append({
            "zone_id": r.zone_id,
            "visits": r.visits,
            "visit_score": _norm(r.visits, peak_visits),
            "avg_dwell_ms": round(dwell_by_zone.get(r.zone_id, 0), 1),
            "dwell_score": _norm(dwell_by_zone.get(r.zone_id, 0), peak_dwell),
        })
    zones.sort(key=lambda z: z["visit_score"], reverse=True)

    return {
        "store_id": store_id,
        "sessions_in_window": sessions,
        "data_confidence": sessions >= settings.heatmap_min_sessions,
        "zones": zones,
    }
