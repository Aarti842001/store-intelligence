"""Runtime settings, read from the environment.

Defaults are tuned for `docker compose up` (postgres) but fall back to a local
sqlite file so the test suite and a quick `uvicorn app.main:app` work with no
database running. Everything here is overridable via env vars.
"""

import os


class Settings:
    # postgres in compose; sqlite locally so tests need zero infra
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./store_intelligence.db")

    # business rules
    pos_correlation_window_s: int = int(os.getenv("POS_WINDOW_S", "300"))   # 5 min
    metrics_window_h: int = int(os.getenv("METRICS_WINDOW_H", "24"))        # "today"
    stale_feed_s: int = int(os.getenv("STALE_FEED_S", "600"))               # 10 min
    dead_zone_s: int = int(os.getenv("DEAD_ZONE_S", "1800"))                # 30 min
    queue_spike_depth: int = int(os.getenv("QUEUE_SPIKE_DEPTH", "5"))
    heatmap_min_sessions: int = int(os.getenv("HEATMAP_MIN_SESSIONS", "20"))

    pos_csv: str = os.getenv("POS_CSV", "data/pos_transactions.csv")
    seed_file: str = os.getenv("SEED_FILE", "data/events.sample.jsonl")
    autoseed: bool = os.getenv("AUTOSEED", "1") == "1"

    # the POS sample is a different store than our footage, so we let ops map it
    # onto the store(s) we actually have events for. format: "ST1008:STORE_BLR_002"
    pos_store_map: str = os.getenv("POS_STORE_MAP", "ST1008:STORE_BLR_002")


settings = Settings()
