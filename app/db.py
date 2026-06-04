"""Database wiring.

One table: events. We keep the raw event payload in a JSON column and also
lift the few fields we filter/aggregate on into real columns (with indexes) so
the metric queries stay fast and readable. event_id is the primary key, which
is what makes ingest idempotent for free -- a re-sent event just collides.
"""

from datetime import datetime

from sqlalchemy import (Boolean, Column, DateTime, Float, Integer, String,
                        create_engine, func)
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.types import JSON

from app.config import settings

Base = declarative_base()

# sqlite needs check_same_thread off for the threaded test client; harmless on pg
_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Event(Base):
    __tablename__ = "events"

    event_id = Column(String, primary_key=True)          # idempotency key
    store_id = Column(String, index=True, nullable=False)
    camera_id = Column(String)
    visitor_id = Column(String, index=True)
    event_type = Column(String, index=True, nullable=False)
    ts = Column(DateTime, index=True, nullable=False)     # parsed timestamp
    zone_id = Column(String, index=True)
    dwell_ms = Column(Integer, default=0)
    is_staff = Column(Boolean, default=False, index=True)
    confidence = Column(Float, default=1.0)
    queue_depth = Column(Integer, nullable=True)
    sku_zone = Column(String, nullable=True)
    session_seq = Column(Integer, default=0)
    raw = Column(JSON)                                    # full original payload


def init_db():
    Base.metadata.create_all(engine)


def db_ok() -> bool:
    """cheap liveness probe used by /health and the 503 guard."""
    try:
        with engine.connect() as c:
            c.execute(func.now() if not settings.database_url.startswith("sqlite")
                      else __import__("sqlalchemy").text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False


def get_db():
    """FastAPI dependency. If the DB is down the session blows up on first use
    and the global handler turns it into a clean 503 (no stack trace leaks)."""
    db = SessionLocal()
    try:
        yield db
    except OperationalError:
        db.rollback()
        raise
    finally:
        db.close()
