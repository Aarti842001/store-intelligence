"""One place for 'now'. We store timestamps as naive UTC in the DB (portable
across sqlite/pg), so we compare against a naive-UTC now rather than a
tz-aware one -- mixing the two raises in Python."""

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
