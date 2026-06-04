"""POS transaction loader.

The provided sample uses split date/time columns (DD-MM-YYYY + HH:MM:SS) and a
store id (ST1008) that doesn't match our footage's store, so we remap it via
POS_STORE_MAP. There's no customer id in POS by design -- correlation to a
visitor is purely store + time window, exactly as the brief specifies.
"""

import csv
import os
from datetime import datetime
from functools import lru_cache

from app.config import settings


def _store_map() -> dict:
    out = {}
    for pair in settings.pos_store_map.split(","):
        pair = pair.strip()
        if ":" in pair:
            src, dst = pair.split(":", 1)
            out[src.strip()] = dst.strip()
    return out


@lru_cache(maxsize=1)
def load_pos() -> list[tuple]:
    """returns [(store_id, ts, amount), ...] sorted by ts. cached; the sample
    file is tiny and static."""
    path = settings.pos_csv
    if not os.path.exists(path):
        return []
    smap = _store_map()
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                ts = datetime.strptime(f"{r['order_date']} {r['order_time']}",
                                       "%d-%m-%Y %H:%M:%S")
                store = smap.get(r["store_id"], r["store_id"])
                amt = float(r.get("total_amount") or 0)
                rows.append((store, ts, amt))
            except (KeyError, ValueError):
                continue          # skip malformed rows rather than crash
    rows.sort(key=lambda x: x[1])
    return rows


def pos_for(store_id: str, start: datetime, end: datetime) -> list[tuple]:
    return [r for r in load_pos() if r[0] == store_id and start <= r[1] <= end]
