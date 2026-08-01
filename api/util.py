"""Small shared helpers."""
from datetime import datetime, timedelta, timezone


def now_iso() -> str:
    """UTC ISO-8601, matching the format the pipeline orchestrators already write."""
    return datetime.now(timezone.utc).isoformat()


def hours_from_now_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
