"""Long term activity storage for the SIA DC-09 integration.

Home Assistant's recorder is tuned for weeks of data, not the two years of
alarm history this integration promises to keep, and purging it would take the
activity log with it. So activity lives in a small private SQLite database
alongside the Home Assistant configuration.

``sqlite3`` is blocking, so every call here runs on the executor. The public API
is asynchronous and must only be awaited from the event loop.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

#: Bumped whenever the schema changes in a way that needs a migration.
SCHEMA_VERSION = 1

#: Rows are written in batches this large at most, to keep the executor job short.
MAX_BATCH = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id      TEXT    NOT NULL,
    account       TEXT    NOT NULL,
    received_at   TEXT    NOT NULL,
    timestamp_utc TEXT,
    transport     TEXT    NOT NULL,
    local_port    INTEGER NOT NULL,
    remote_ip     TEXT,
    protocol      TEXT,
    code          TEXT,
    zone          TEXT,
    area          TEXT,
    severity      TEXT,
    category      TEXT,
    summary       TEXT,
    status_before TEXT,
    status_after  TEXT,
    is_test       INTEGER NOT NULL DEFAULT 0,
    encrypted     INTEGER NOT NULL DEFAULT 0,
    crc_valid     INTEGER NOT NULL DEFAULT 1,
    response      TEXT,
    raw           TEXT,
    extra         TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_account_time
    ON events (entry_id, account, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_events_time
    ON events (received_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_COLUMNS = (
    "entry_id",
    "account",
    "received_at",
    "timestamp_utc",
    "transport",
    "local_port",
    "remote_ip",
    "protocol",
    "code",
    "zone",
    "area",
    "severity",
    "category",
    "summary",
    "status_before",
    "status_after",
    "is_test",
    "encrypted",
    "crc_valid",
    "response",
    "raw",
    "extra",
)

_INSERT = (
    f"INSERT INTO events ({', '.join(_COLUMNS)}) "  # noqa: S608 - fixed column list
    f"VALUES ({', '.join('?' * len(_COLUMNS))})"
)


@dataclass(slots=True)
class ActivityRecord:
    """One stored activity row."""

    account: str
    received_at: datetime
    transport: str
    local_port: int
    remote_ip: str | None = None
    timestamp_utc: datetime | None = None
    protocol: str | None = None
    code: str | None = None
    zone: str | None = None
    area: str | None = None
    severity: str | None = None
    category: str | None = None
    summary: str | None = None
    status_before: str | None = None
    status_after: str | None = None
    is_test: bool = False
    encrypted: bool = False
    crc_valid: bool = True
    response: str | None = None
    raw: str | None = None
    extra: dict[str, Any] | None = None

    def as_row(self, entry_id: str) -> tuple[Any, ...]:
        """Return this record as a parameter tuple for the insert statement."""
        return (
            entry_id,
            self.account,
            self.received_at.isoformat(),
            self.timestamp_utc.isoformat() if self.timestamp_utc else None,
            self.transport,
            self.local_port,
            self.remote_ip,
            self.protocol,
            self.code,
            self.zone,
            self.area,
            self.severity,
            self.category,
            self.summary,
            self.status_before,
            self.status_after,
            int(self.is_test),
            int(self.encrypted),
            int(self.crc_valid),
            self.response,
            self.raw,
            json.dumps(self.extra) if self.extra else None,
        )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a database row into a JSON-serialisable dictionary."""
    data = dict(row)
    data.pop("entry_id", None)
    for flag in ("is_test", "encrypted", "crc_valid"):
        if flag in data:
            data[flag] = bool(data[flag])
    if data.get("extra"):
        with suppress(json.JSONDecodeError):
            data["extra"] = json.loads(data["extra"])
    return data


def _parse(value: Any) -> datetime | None:
    """Return a stored timestamp as an aware datetime."""
    if not value:
        return None
    parsed = dt_util.parse_datetime(str(value))
    return dt_util.as_utc(parsed) if parsed is not None else None


@dataclass(slots=True)
class AccountHistory:
    """What an account's stored messages say about it.

    ``statuses`` holds the status each message left the account in, oldest
    first, so replaying it reproduces the state machine's own conclusions
    rather than guessing at them again from the raw codes.
    """

    last_message_at: datetime | None = None
    last_activity_at: datetime | None = None
    statuses: list[str] = field(default_factory=list)
    last_code: str | None = None


class ActivityStore:
    """Append-only activity log for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str, path: str | None = None):
        """Initialise the store without touching the filesystem."""
        self.hass = hass
        self.entry_id = entry_id
        self.path = Path(
            path or hass.config.path(f"{__package__.rsplit('.', 1)[-1]}.db")
        )
        self._pending: list[tuple[Any, ...]] = []

    # --- lifecycle -----------------------------------------------------------

    async def async_setup(self) -> None:
        """Create the database file and schema if they do not exist."""
        await self.hass.async_add_executor_job(self._setup)

    def _setup(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        # WAL keeps readers from blocking the writer, which matters because the
        # listener writes while services read.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    async def async_close(self) -> None:
        """Flush anything still buffered."""
        await self.async_flush()

    async def async_remove(self) -> None:
        """Delete this entry's stored activity, and the file if it is the last.

        The database is shared by every config entry, so removing one receiver
        must not take another's history with it. When nothing is left the file
        goes too, including the write-ahead sidecars, so removing the
        integration really does leave nothing behind.
        """
        self._pending.clear()
        await self.hass.async_add_executor_job(self._remove)

    def _remove(self) -> None:
        if not self.path.exists():
            return
        try:
            with closing(self._connect()) as conn:
                conn.execute("DELETE FROM events WHERE entry_id = ?", (self.entry_id,))
                conn.commit()
                remaining = int(
                    conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                )
                if remaining:
                    # Another receiver is still using the file. Reclaim the
                    # space this entry was holding rather than leaving it.
                    conn.execute("VACUUM")
                    return
        except sqlite3.Error:
            _LOGGER.exception("Failed to delete stored activity for this entry")
            return

        for path in (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        ):
            with suppress(OSError):
                path.unlink(missing_ok=True)

    # --- writing -------------------------------------------------------------

    def queue(self, record: ActivityRecord) -> None:
        """Buffer a record. Safe to call from the event loop."""
        self._pending.append(record.as_row(self.entry_id))

    async def async_add(self, record: ActivityRecord) -> None:
        """Buffer a record and flush if the batch is full."""
        self.queue(record)
        if len(self._pending) >= MAX_BATCH:
            await self.async_flush()

    async def async_flush(self) -> None:
        """Write buffered records to disk."""
        if not self._pending:
            return
        rows, self._pending = self._pending, []
        try:
            await self.hass.async_add_executor_job(self._write, rows)
        except sqlite3.Error:
            _LOGGER.exception("Failed to write %d activity rows", len(rows))

    def _write(self, rows: list[tuple[Any, ...]]) -> None:
        with closing(self._connect()) as conn:
            conn.executemany(_INSERT, rows)
            conn.commit()

    # --- reading -------------------------------------------------------------

    async def async_get_activity(
        self,
        account: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        include_tests: bool = False,
        severity: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return stored activity, newest first."""
        await self.async_flush()
        return await self.hass.async_add_executor_job(
            self._query, account, start, end, limit, include_tests, severity
        )

    def _query(
        self,
        account: str | None,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        include_tests: bool,
        severity: str | None,
    ) -> list[dict[str, Any]]:
        where = ["entry_id = ?"]
        params: list[Any] = [self.entry_id]

        if account is not None:
            where.append("account = ?")
            params.append(account)
        if start is not None:
            where.append("received_at >= ?")
            params.append(start.isoformat())
        if end is not None:
            where.append("received_at <= ?")
            params.append(end.isoformat())
        if not include_tests:
            where.append("is_test = 0")
        if severity is not None:
            where.append("severity = ?")
            params.append(severity)

        sql = (
            # Only fixed fragments are interpolated; all values are bound.
            f"SELECT * FROM events WHERE {' AND '.join(where)} "  # noqa: S608
            "ORDER BY received_at DESC, id DESC LIMIT ?"
        )
        params.append(max(1, min(limit, 10000)))

        with closing(self._connect()) as conn:
            return [_row_to_dict(row) for row in conn.execute(sql, params)]

    async def async_get_history(self, account: str, limit: int = 50) -> AccountHistory:
        """Return what is known about an account from what it has already sent.

        Used to restore an account after a restart. Home Assistant has no idea
        whether a panel is armed until it sends its next message, which for a
        quiet alarm can be hours away, so the last messages it did send are the
        only evidence available.
        """
        await self.async_flush()
        return await self.hass.async_add_executor_job(self._history, account, limit)

    def _history(self, account: str, limit: int) -> AccountHistory:
        with closing(self._connect()) as conn:
            seen = conn.execute(
                "SELECT MAX(received_at) AS last_message, "
                "MAX(CASE WHEN is_test = 0 THEN received_at END) AS last_activity "
                "FROM events WHERE entry_id = ? AND account = ?",
                (self.entry_id, account),
            ).fetchone()

            rows = conn.execute(
                "SELECT status_after, code FROM events "
                "WHERE entry_id = ? AND account = ? AND status_after IS NOT NULL "
                "ORDER BY received_at DESC, id DESC LIMIT ?",
                (self.entry_id, account, max(1, min(limit, 1000))),
            ).fetchall()

        return AccountHistory(
            last_message_at=_parse(seen["last_message"] if seen else None),
            last_activity_at=_parse(seen["last_activity"] if seen else None),
            # Oldest first, so it can be replayed in the order it happened.
            statuses=[str(row["status_after"]) for row in reversed(rows)],
            last_code=str(rows[0]["code"]) if rows and rows[0]["code"] else None,
        )

    async def async_count(self, account: str | None = None) -> int:
        """Return how many rows are stored, optionally for one account."""
        await self.async_flush()
        return await self.hass.async_add_executor_job(self._count, account)

    def _count(self, account: str | None) -> int:
        sql = "SELECT COUNT(*) FROM events WHERE entry_id = ?"
        params: list[Any] = [self.entry_id]
        if account is not None:
            sql += " AND account = ?"
            params.append(account)
        with closing(self._connect()) as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    # --- maintenance ---------------------------------------------------------

    async def async_purge(self, retention_months: int) -> int:
        """Delete rows older than the retention window and return the count."""
        if retention_months <= 0:
            return 0
        cutoff = dt_util.utcnow() - timedelta(days=retention_months * 31)
        return await self.hass.async_add_executor_job(self._purge, cutoff)

    def _purge(self, cutoff: datetime) -> int:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "DELETE FROM events WHERE entry_id = ? AND received_at < ?",
                (self.entry_id, cutoff.isoformat()),
            )
            conn.commit()
            deleted = cursor.rowcount or 0
        if deleted:
            _LOGGER.debug("Purged %d activity rows older than %s", deleted, cutoff)
        return deleted

    async def async_clear(self, account: str | None = None) -> int:
        """Delete all stored activity, optionally for one account only."""
        self._pending.clear()
        return await self.hass.async_add_executor_job(self._clear, account)

    def _clear(self, account: str | None) -> int:
        sql = "DELETE FROM events WHERE entry_id = ?"
        params: list[Any] = [self.entry_id]
        if account is not None:
            sql += " AND account = ?"
            params.append(account)
        with closing(self._connect()) as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.rowcount or 0
