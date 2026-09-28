"""Helpers shared across the SIA DC-09 integration."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .const import DOMAIN
from .dc09 import normalise_user_number, parse_key
from .models import SiaDc09Event
from .store import ActivityRecord

#: DC-09 account numbers are hex digits, three to sixteen of them.
ACCOUNT_PATTERN = re.compile(r"^[0-9A-Fa-f]{3,16}$")

#: One ``number = name`` line of an account's user list. Either separator is
#: accepted because both read naturally to someone typing a list.
USER_LINE_PATTERN = re.compile(r"^\s*(\d{1,10})\s*[:=]\s*(\S.*?)\s*$")

#: Longest user name kept, so one pasted essay cannot bloat every summary.
MAX_USER_NAME = 64

#: Lengths, in characters, of the AES keys DC-09 allows.
VALID_KEY_LENGTHS = (32, 48, 64)


def normalise_account(account: str) -> str:
    """Return an account number in the canonical form used for lookups.

    Panels are inconsistent about case, so accounts are compared upper case
    with surrounding whitespace removed.
    """
    return account.strip().upper()


def is_valid_account(account: str) -> bool:
    """Return whether a string is a usable DC-09 account number."""
    return bool(ACCOUNT_PATTERN.match(account.strip()))


def is_valid_key(key: str) -> bool:
    """Return whether a string is a valid 128, 192 or 256 bit AES key.

    DC-09 keys are written as hex, so the accepted lengths are 32, 48 and 64
    characters.
    """
    candidate = key.strip()
    if len(candidate) not in VALID_KEY_LENGTHS:
        return False
    try:
        bytes.fromhex(candidate)
    except ValueError:
        return False
    return parse_key(candidate) is not None


def parse_users(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse an account's user list into user numbers mapped to names.

    Accepts one ``501 = Mikkel`` or ``501: Mikkel`` pair per line. Returns the
    mapping alongside any lines that could not be understood, so the config
    flow can say what was wrong instead of silently dropping people.
    """
    users: dict[str, str] = {}
    invalid: list[str] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        match = USER_LINE_PATTERN.match(line)
        if match is None:
            invalid.append(line.strip())
            continue
        number, name = match.groups()
        number = normalise_user_number(number)
        if number in users:
            # Silently keeping the last one would hide a typo in a long list.
            invalid.append(line.strip())
            continue
        users[number] = name[:MAX_USER_NAME]
    return users, invalid


def format_users(users: Mapping[str, str]) -> str:
    """Render a stored user mapping back into the text the form shows."""
    return "\n".join(
        f"{number}: {name}" for number, name in sorted(users.items(), key=_by_number)
    )


def _by_number(item: tuple[str, str]) -> tuple[int, str]:
    """Sort user numbers numerically, so 9 comes before 10."""
    number = item[0]
    return (int(number) if number.isdigit() else 0, number)


def clean_users(value: Any) -> dict[str, str]:
    """Return a stored user mapping with its numbers in lookup form."""
    if not isinstance(value, Mapping):
        return {}
    return {
        normalise_user_number(str(number)): str(name)[:MAX_USER_NAME]
        for number, name in value.items()
        if str(name).strip()
    }


def device_identifier(entry_id: str, account: str) -> tuple[str, str]:
    """Return the device registry identifier for an account."""
    return (DOMAIN, f"{entry_id}_{normalise_account(account)}")


def unique_id(entry_id: str, account: str, key: str) -> str:
    """Return a stable unique id for one entity of one account."""
    return f"{entry_id}_{normalise_account(account)}_{key}"


def event_to_record(event: SiaDc09Event) -> ActivityRecord:
    """Convert a dispatched event into a row for the activity store."""
    return ActivityRecord(
        account=event.account,
        received_at=event.received_at,
        transport=event.transport,
        local_port=event.port,
        remote_ip=event.remote_ip,
        timestamp_utc=event.timestamp,
        protocol=event.protocol,
        code=event.code,
        zone=event.zone,
        area=event.area,
        severity=event.severity,
        category=event.category,
        summary=event.summary or event.code_title or event.code,
        status_before=event.status_before,
        status_after=event.status_after,
        is_test=event.is_test,
        encrypted=event.encrypted,
        crc_valid=event.crc_valid,
        response=event.response,
        raw=event.raw_hex,
        extra=_record_extra(event),
    )


def _record_extra(event: SiaDc09Event) -> dict[str, Any] | None:
    """Return the fields worth keeping but not worth a column of their own."""
    extra: dict[str, Any] = {}
    if event.user:
        extra["user"] = event.user
    if event.user_number:
        extra["user_number"] = event.user_number
    if event.user_name:
        extra["user_name"] = event.user_name
    if event.partition:
        extra["partition"] = event.partition
    if event.sequence:
        extra["sequence"] = event.sequence
    if event.code_title:
        extra["code_title"] = event.code_title
    if event.message:
        extra["message"] = event.message
    if event.extended_data:
        extra["extended_data"] = event.extended_data
    if event.errors:
        extra["errors"] = list(event.errors)
    return extra or None
