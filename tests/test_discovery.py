"""Tests for the unconfigured-account log and for user-name substitution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.sia_dc09.dc09 import (
    build_frame,
    enrich,
    normalise_user_number,
    summarise_with_user_names,
)
from custom_components.sia_dc09.discovery import UnknownAccountLog, UnknownMessage
from custom_components.sia_dc09.models import SiaDc09Event
from custom_components.sia_dc09.utils import format_users, parse_users

from .conftest import decode_text

NOW = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)


def summary_of(code: str, users: dict[str, str]) -> str:
    """Decode one SIA code and render its summary with user names applied."""
    message = f'"SIA-DCS"0001R0L0#1234[#1234|Nri1/{code}]'
    result = decode_text(build_frame(message).decode("latin-1"))
    assert result.ok, result.errors
    enriched = enrich(result)
    assert enriched is not None
    return summarise_with_user_names(enriched, users)


def make_event(account: str = "9999", ip: str = "192.0.2.10", **kwargs) -> SiaDc09Event:
    """Return an event as it would arrive from an unconfigured account."""
    return SiaDc09Event(
        account=account,
        received_at=kwargs.pop("received_at", NOW),
        transport=kwargs.pop("transport", "udp"),
        port=kwargs.pop("port", 10000),
        remote_ip=ip,
        protocol="SIA-DCS",
        code=kwargs.pop("code", "BA"),
        summary=kwargs.pop("summary", "Burglary Alarm"),
        raw_hex=kwargs.pop("raw_hex", "0a0b"),
        **kwargs,
    )


def test_log_keeps_messages_and_sources() -> None:
    """A count alone cannot answer who is transmitting or what they sent."""
    log = UnknownAccountLog()

    log.record("9999", make_event(summary="Burglary Alarm"))
    log.record("9999", make_event(ip="192.0.2.11", summary="Closing Report"))

    record = log.get("9999")
    assert record.count == 2
    assert record.sources == {"192.0.2.10": 1, "192.0.2.11": 1}
    assert record.last_message.summary == "Closing Report"

    detail = record.as_dict()
    assert detail["remote_ips"] == {"192.0.2.10": 1, "192.0.2.11": 1}
    # Newest first, because that is the one being investigated.
    assert detail["recent_messages"][0]["summary"] == "Closing Report"
    assert detail["recent_messages"][0]["raw"] == "0a0b"


def test_log_tracks_first_and_last_seen() -> None:
    """The window a stranger has been transmitting in is part of the picture."""
    log = UnknownAccountLog()
    later = NOW + timedelta(minutes=5)

    log.record("9999", make_event(received_at=NOW))
    log.record("9999", make_event(received_at=later))

    record = log.get("9999")
    assert record.first_seen == NOW
    assert record.last_seen == later


def test_log_caps_accounts() -> None:
    """Account numbers are attacker-chosen, so the log must not grow freely."""
    log = UnknownAccountLog(max_accounts=2)

    for index in range(5):
        log.record(f"900{index}", make_event(account=f"900{index}"))

    assert len(log) == 2
    assert log.dropped == 3


def test_log_caps_messages_per_account() -> None:
    """One chatty stranger cannot fill memory either."""
    log = UnknownAccountLog()

    for index in range(50):
        log.record("9999", make_event(summary=f"Message {index}"))

    record = log.get("9999")
    assert record.count == 50
    assert len(record.messages) == 10
    assert record.last_message.summary == "Message 49"


def test_log_caps_sources_per_account() -> None:
    """A spoofed source address per packet cannot fill memory either."""
    log = UnknownAccountLog()

    for index in range(50):
        log.record("9999", make_event(ip=f"192.0.2.{index}"))

    assert len(log.get("9999").sources) == 10


def test_attribute_view_can_drop_raw_frames() -> None:
    """Entity attributes are written to the recorder, so they stay small."""
    log = UnknownAccountLog()
    log.record("9999", make_event())

    [detail] = log.as_list(messages=1, include_raw=False)
    assert "raw" not in detail["recent_messages"][0]


def test_busiest_first() -> None:
    """The account shouting loudest is the one worth looking at."""
    log = UnknownAccountLog()
    log.record("1111", make_event(account="1111"))
    for _ in range(3):
        log.record("2222", make_event(account="2222"))

    assert [record.account for record in log.busiest()] == ["2222", "1111"]
    assert log.message_counts() == {"2222": 3, "1111": 1}


def test_unknown_message_from_event_carries_the_source() -> None:
    """The originating address is the whole point of the record."""
    message = UnknownMessage.from_event(make_event())
    assert message.remote_ip == "192.0.2.10"
    assert message.as_dict()["remote_ip"] == "192.0.2.10"


# --- user names -------------------------------------------------------------


def test_closing_report_uses_the_user_name() -> None:
    """The example from the panel: user 501 should read as a person."""
    assert summary_of("CL501", {"501": "Mikkel"}) == (
        "Closing Report - User Mikkel (area 1)"
    )


def test_padded_user_number_still_matches() -> None:
    """Panels pad the same user inconsistently, so both sides are normalised."""
    assert "User Mikkel" in summary_of("CL0501", {"501": "Mikkel"})


def test_unknown_user_number_is_left_alone() -> None:
    """A number with no name keeps reading as a number."""
    assert summary_of("CL501", {"7": "Anna"}) == (
        "Closing Report - User number 501 (area 1)"
    )


def test_zone_numbers_are_not_users() -> None:
    """A zone that happens to match a user number must never be renamed."""
    assert "Mikkel" not in summary_of("BA501", {"501": "Mikkel"})


def test_no_names_configured_changes_nothing() -> None:
    """Without a user list the summary is exactly what it always was."""
    assert summary_of("CL501", {}) == "Closing Report - User number 501 (area 1)"


def test_normalise_user_number_strips_padding() -> None:
    """Zero padding is cosmetic; '007' and '7' are the same user."""
    assert normalise_user_number("007") == "7"
    assert normalise_user_number("7") == "7"
    assert normalise_user_number("0") == "0"


def test_user_list_round_trips() -> None:
    """What the user typed comes back when they reopen the form."""
    users, invalid = parse_users("501: Mikkel\n502 = Anna")
    assert invalid == []
    assert users == {"501": "Mikkel", "502": "Anna"}
    assert parse_users(format_users(users))[0] == users


def test_duplicate_user_numbers_are_reported() -> None:
    """The same number twice is a typo, not an instruction to overwrite."""
    users, invalid = parse_users("501: Mikkel\n501: Anna")
    assert invalid == ["501: Anna"]
    assert users == {"501": "Mikkel"}
