"""DC-09 timestamps are ``HH:MM:SS,MM-DD-YYYY`` and are always UTC."""

from __future__ import annotations

import re
from datetime import UTC, datetime

TIMESTAMP_PATTERN = re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{2})-(\d{2})-(\d{4})$")


def parse_timestamp(value: str) -> datetime | None:
    """Parse a DC-09 timestamp into an aware UTC datetime, or ``None``."""
    match = TIMESTAMP_PATTERN.match(value)
    if not match:
        return None
    hour, minute, second, month, day, year = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError:
        # Rejects impossible dates such as 02-30, which the pattern allows.
        return None


def format_timestamp(value: datetime | None = None) -> str:
    """Format a datetime as the DC-09 UTC timestamp used in responses."""
    moment = (value or datetime.now(UTC)).astimezone(UTC)
    return (
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d},"
        f"{moment.month:02d}-{moment.day:02d}-{moment.year:04d}"
    )
