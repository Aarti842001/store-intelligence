"""Wire schemas + the normalisation layer.

The brief ships two slightly different event shapes (the "Required Output
Schema" box vs the sample_events.jsonl), and real CCTV vendors are never
consistent either. So ingest is deliberately tolerant: we accept the canonical
schema and quietly map the known aliases (store_code, id_token, event_time,
lowercase types...) onto it. Anything we genuinely can't parse is rejected per
event with a reason, not a 500.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from dateutil import parser as dtparser
from pydantic import BaseModel, Field, field_validator, model_validator

# canonical event types (uppercase)
EVENT_TYPES = {
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
    "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
}

# aliases we've seen in the wild -> canonical
_TYPE_ALIASES = {
    "zone_entered": "ZONE_ENTER", "zone_exited": "ZONE_EXIT",
    "queue_join": "BILLING_QUEUE_JOIN", "queue_completed": "BILLING_QUEUE_JOIN",
    "queue_abandoned": "BILLING_QUEUE_ABANDON",
}
_FIELD_ALIASES = {
    "store_code": "store_id", "id_token": "visitor_id",
    "event_timestamp": "timestamp", "event_time": "timestamp",
}


class Metadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0

    model_config = {"extra": "allow"}   # don't lose vendor extras


class EventIn(BaseModel):
    event_id: str
    store_id: str
    event_type: str
    timestamp: datetime
    camera_id: Optional[str] = None
    visitor_id: Optional[str] = None
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool = False
    confidence: float = 1.0
    metadata: Metadata = Field(default_factory=Metadata)

    model_config = {"extra": "ignore"}

    @model_validator(mode="before")
    @classmethod
    def _apply_aliases(cls, data: Any):
        if not isinstance(data, dict):
            return data
        d = dict(data)
        for alias, canon in _FIELD_ALIASES.items():
            if alias in d and canon not in d:
                d[canon] = d.pop(alias)
        # a couple of vendors put queue fields at the top level
        meta = dict(d.get("metadata") or {})
        for k in ("queue_depth", "sku_zone", "session_seq"):
            if k in d and k not in meta:
                meta[k] = d[k]
        if meta:
            d["metadata"] = meta
        return d

    @field_validator("event_type", mode="before")
    @classmethod
    def _norm_type(cls, v):
        if not isinstance(v, str):
            raise ValueError("event_type must be a string")
        t = _TYPE_ALIASES.get(v.strip().lower(), v.strip().upper())
        if t not in EVENT_TYPES:
            raise ValueError(f"unknown event_type: {v}")
        return t

    @field_validator("timestamp", mode="before")
    @classmethod
    def _parse_ts(cls, v):
        if isinstance(v, datetime):
            dt = v
        else:
            dt = dtparser.parse(str(v))
        if dt.tzinfo is None:                # assume UTC if naive
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)


class IngestRequest(BaseModel):
    events: list[dict]      # validate per-item ourselves for partial success

    @field_validator("events")
    @classmethod
    def _cap_batch(cls, v):
        if len(v) > 500:
            raise ValueError("batch exceeds 500 events")
        return v


class IngestError(BaseModel):
    index: int
    error: str


class IngestResponse(BaseModel):
    ingested: int
    duplicates: int
    failed: list[IngestError]
