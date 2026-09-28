"""Records accounts seen on the wire that are not configured.

Knowing only that an unconfigured account transmitted is not actionable: it
says nothing about whether it is a panel that was never added, a neighbour's
installation pointed at the wrong receiver, or someone probing the port. What
makes it answerable is where the traffic came from and what it said, so the
messages themselves are kept alongside the count.

Everything here is bounded. An unconfigured account number is chosen by
whoever sends the message, so an attacker could otherwise invent an unlimited
number of them and grow memory without limit.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import SiaDc09Event

#: Distinct unconfigured accounts remembered at once.
MAX_UNKNOWN_ACCOUNTS = 25

#: Originating addresses remembered per account.
MAX_SOURCES = 10

#: Messages kept per account, newest last.
MAX_MESSAGES = 10

#: Messages included when the detail is rendered for entity attributes, where
#: the whole payload has to stay small enough for the recorder.
ATTRIBUTE_MESSAGES = 3

#: Accounts included in entity attributes, busiest first.
ATTRIBUTE_ACCOUNTS = 10


@dataclass(frozen=True, slots=True)
class UnknownMessage:
    """One message from an account that is not configured."""

    received_at: datetime
    transport: str
    local_port: int
    remote_ip: str
    protocol: str
    code: str | None = None
    code_title: str | None = None
    summary: str = ""
    severity: str = "info"
    response: str | None = None
    encrypted: bool = False
    decoded: bool = True
    errors: tuple[str, ...] = ()
    raw_hex: str = ""

    @classmethod
    def from_event(cls, event: SiaDc09Event) -> UnknownMessage:
        """Build a record from the event the message was decoded into."""
        return cls(
            received_at=event.received_at,
            transport=event.transport,
            local_port=event.port,
            remote_ip=event.remote_ip,
            protocol=event.protocol,
            code=event.code,
            code_title=event.code_title,
            summary=event.summary,
            severity=event.severity,
            response=event.response,
            encrypted=event.encrypted,
            decoded=event.decoded,
            errors=event.errors,
            raw_hex=event.raw_hex,
        )

    def as_dict(self, include_raw: bool = True) -> dict[str, Any]:
        """Return a JSON-serialisable view of this message."""
        data: dict[str, Any] = {
            "received_at": self.received_at.isoformat(),
            "transport": self.transport,
            "port": self.local_port,
            "remote_ip": self.remote_ip,
            "protocol": self.protocol,
            "code": self.code,
            "title": self.code_title,
            "summary": self.summary,
            "severity": self.severity,
            "response": self.response,
            "encrypted": self.encrypted,
            "decoded": self.decoded,
        }
        if self.errors:
            data["errors"] = list(self.errors)
        if include_raw and self.raw_hex:
            data["raw"] = self.raw_hex
        return data


@dataclass
class UnknownAccount:
    """What has been seen from one unconfigured account."""

    account: str
    count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    #: Originating addresses mapped to how many messages came from each. More
    #: than one is worth noticing: an account should have a single panel.
    sources: dict[str, int] = field(default_factory=dict)
    messages: deque[UnknownMessage] = field(
        default_factory=lambda: deque(maxlen=MAX_MESSAGES)
    )

    def record(self, message: UnknownMessage) -> None:
        """Fold one message into this account's record."""
        self.count += 1
        if self.first_seen is None:
            self.first_seen = message.received_at
        self.last_seen = message.received_at

        if message.remote_ip in self.sources:
            self.sources[message.remote_ip] += 1
        elif len(self.sources) < MAX_SOURCES:
            self.sources[message.remote_ip] = 1

        self.messages.append(message)

    @property
    def last_message(self) -> UnknownMessage | None:
        """Return the most recent message, if there is one."""
        return self.messages[-1] if self.messages else None

    def as_dict(
        self, limit: int = MAX_MESSAGES, include_raw: bool = True
    ) -> dict[str, Any]:
        """Return a JSON-serialisable view, newest messages first."""
        recent = list(self.messages)[-limit:] if limit else []
        last = self.last_message
        return {
            "account": self.account,
            "message_count": self.count,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "remote_ips": dict(self.sources),
            "last_summary": last.summary if last else None,
            "last_code": last.code if last else None,
            "recent_messages": [
                message.as_dict(include_raw) for message in reversed(recent)
            ],
        }


class UnknownAccountLog:
    """The unconfigured accounts heard by one receiver."""

    def __init__(self, max_accounts: int = MAX_UNKNOWN_ACCOUNTS):
        """Start with nothing seen."""
        self._accounts: dict[str, UnknownAccount] = {}
        self._max_accounts = max_accounts
        #: Messages dropped because the account cap was already reached. A
        #: non-zero value here is itself a signal: something is spraying
        #: invented account numbers at the receiver.
        self.dropped = 0

    def __contains__(self, account: str) -> bool:
        """Return whether an account has been heard."""
        return account in self._accounts

    def __len__(self) -> int:
        """Return how many distinct accounts have been heard."""
        return len(self._accounts)

    def __iter__(self):
        """Iterate over the account numbers heard."""
        return iter(self._accounts)

    def get(self, account: str) -> UnknownAccount | None:
        """Return the record for an account, if it has been heard."""
        return self._accounts.get(account)

    def record(self, account: str, event: SiaDc09Event) -> UnknownAccount | None:
        """Record a message, unless the account cap has been reached."""
        record = self._accounts.get(account)
        if record is None:
            if len(self._accounts) >= self._max_accounts:
                self.dropped += 1
                return None
            record = UnknownAccount(account=account)
            self._accounts[account] = record
        record.record(UnknownMessage.from_event(event))
        return record

    def clear(self) -> None:
        """Forget everything heard so far."""
        self._accounts.clear()
        self.dropped = 0

    def busiest(self) -> list[UnknownAccount]:
        """Return the accounts heard, busiest first."""
        return sorted(
            self._accounts.values(), key=lambda item: item.count, reverse=True
        )

    def as_list(
        self,
        accounts: int | None = None,
        messages: int = MAX_MESSAGES,
        include_raw: bool = True,
    ) -> list[dict[str, Any]]:
        """Return a JSON-serialisable view of the busiest accounts."""
        records = self.busiest()
        if accounts is not None:
            records = records[:accounts]
        return [record.as_dict(messages, include_raw) for record in records]

    def message_counts(self) -> dict[str, int]:
        """Return how many messages each account has sent."""
        return {record.account: record.count for record in self.busiest()}
