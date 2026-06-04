"""Health: the first thing an on-call engineer looks at.

Reports DB connectivity and, per store, the last event we saw and whether that
feed has gone quiet (STALE_FEED) past the configured lag. Note the staleness is
measured against wall-clock now, so replaying historical clips will read stale
unless you replay in (simulated) real time -- which is exactly what scripts/
replay.py does for the live demo.
"""

from datetime import datetime, timedelta
from app.clock import utcnow

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Event, db_ok


def health(db: Session) -> dict:
    ok = db_ok()
    feeds = []
    if ok:
        rows = db.execute(
            select(Event.store_id, func.max(Event.ts)).group_by(Event.store_id)).all()
        now = utcnow()
        for store_id, last_ts in rows:
            lag = (now - last_ts).total_seconds() if last_ts else None
            feeds.append({
                "store_id": store_id,
                "last_event": last_ts.isoformat() + "Z" if last_ts else None,
                "lag_seconds": int(lag) if lag is not None else None,
                "stale": bool(lag is not None and lag > settings.stale_feed_s),
            })
    any_stale = any(f["stale"] for f in feeds)
    return {
        "status": "ok" if ok else "degraded",
        "database": "up" if ok else "down",
        "warning": "STALE_FEED" if any_stale else None,
        "stores": feeds,
    }
