"""Ingest pipeline: validate each event, dedup by event_id, store.

Partial success is the contract -- one bad event in a batch of 500 must not
sink the other 499. So we validate per item, collect failures with their index
and reason, and upsert the good ones. Re-sending the same batch is a no-op
because event_id is the primary key (idempotent by construction).
"""

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Event
from app.models import EventIn, IngestError, IngestResponse


def _insert_stmt():
    # ON CONFLICT DO NOTHING -- dialect differs between pg and sqlite
    return pg_insert if not settings.database_url.startswith("sqlite") else sqlite_insert


def ingest_events(db: Session, raw_events: list[dict]) -> IngestResponse:
    rows, failed, seen = [], [], set()

    for i, item in enumerate(raw_events):
        try:
            ev = EventIn.model_validate(item)
        except Exception as e:
            failed.append(IngestError(index=i, error=_short(e)))
            continue
        if ev.event_id in seen:           # duplicate within the same batch
            continue
        seen.add(ev.event_id)
        rows.append({
            "event_id": ev.event_id,
            "store_id": ev.store_id,
            "camera_id": ev.camera_id,
            "visitor_id": ev.visitor_id,
            "event_type": ev.event_type,
            "ts": ev.timestamp.replace(tzinfo=None),   # store naive-utc
            "zone_id": ev.zone_id,
            "dwell_ms": ev.dwell_ms,
            "is_staff": ev.is_staff,
            "confidence": ev.confidence,
            "queue_depth": ev.metadata.queue_depth,
            "sku_zone": ev.metadata.sku_zone,
            "session_seq": ev.metadata.session_seq,
            "raw": item,
        })

    duplicates = 0
    if rows:
        ins = _insert_stmt()(Event).values(rows)
        ins = ins.on_conflict_do_nothing(index_elements=["event_id"])
        result = db.execute(ins)
        db.commit()
        # rowcount tells us how many actually landed; the rest were dupes
        landed = result.rowcount if result.rowcount is not None and result.rowcount >= 0 else len(rows)
        duplicates = len(rows) - landed
        ingested = landed
    else:
        ingested = 0

    return IngestResponse(ingested=ingested, duplicates=max(duplicates, 0), failed=failed)


def _short(exc: Exception) -> str:
    msg = str(exc).splitlines()[0]
    return msg[:160]
