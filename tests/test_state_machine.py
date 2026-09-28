"""Tests for the per-account status state machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.sia_dc09.dc09 import build_frame, decode
from custom_components.sia_dc09.state_machine import (
    ARMED_STATES,
    DEFAULT_SIA_STATUS_MAP,
    PREVIOUS_STATE,
    STICKY_STATES,
    AccountState,
    SiaStatus,
    StatusMapping,
    apply_message,
    core_parity_gaps,
    panel_state,
)

START = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


def decode_body(body: str, received_at: datetime | None = None):
    """Frame and decode a DC-09 message body."""
    result = decode(build_frame(body), received_at=received_at or START)
    assert result.ok, result.errors
    return result


def sia_message(code: str, sequence: str = "0001", account: str = "1234") -> str:
    """Build a plaintext SIA-DCS message body carrying a single event."""
    return f'"SIA-DCS"{sequence}R0L0#{account}[|Nri1/{code}001]'


def feed(
    codes: list[str],
    mapping: StatusMapping | None = None,
    state: AccountState | None = None,
) -> AccountState:
    """Apply a sequence of SIA codes and return the resulting state."""
    mapping = mapping or StatusMapping.build()
    state = state or AccountState()
    for index, code in enumerate(codes):
        result = decode_body(sia_message(code, sequence=f"{index:04d}"))
        state = apply_message(
            state, result, mapping, START + timedelta(minutes=index)
        ).state
    return state


def test_core_parity() -> None:
    """The default mapping must be a superset of Home Assistant core's."""
    assert core_parity_gaps() == {}


def test_arm_then_disarm() -> None:
    assert feed(["CL"]).status is SiaStatus.ARMED_AWAY
    assert feed(["CL", "OP"]).status is SiaStatus.DISARMED


def test_partial_arm_is_armed_night() -> None:
    assert feed(["NL"]).status is SiaStatus.ARMED_NIGHT


def test_alarm_while_armed_then_restore_returns_to_armed() -> None:
    """A burglary restore rewinds to the state held before the alarm."""
    state = feed(["CL", "BA"])
    assert state.status is SiaStatus.TRIGGERED
    assert state.previous is SiaStatus.ARMED_AWAY

    state = feed(["BR"], state=state)
    assert state.status is SiaStatus.ARMED_AWAY


def test_triggered_is_sticky() -> None:
    """Arming events must not silently clear an active alarm."""
    state = feed(["CL", "BA", "CL", "CG"])
    assert state.status is SiaStatus.TRIGGERED


def test_disarm_always_clears_an_alarm() -> None:
    assert feed(["CL", "BA", "OP"]).status is SiaStatus.DISARMED
    assert feed(["CL", "PA", "OR"]).status is SiaStatus.DISARMED


def test_panic_outranks_triggered() -> None:
    state = feed(["CL", "BA", "PA"])
    assert state.status is SiaStatus.PANIC

    # ...and a burglary alarm does not downgrade an active panic.
    assert feed(["BA"], state=state).status is SiaStatus.PANIC


def test_restore_while_idle_does_not_rewind() -> None:
    """A stray restore with no alarm active leaves the status alone."""
    state = feed(["CL", "BR"])
    assert state.status is SiaStatus.ARMED_AWAY


def test_unmapped_code_leaves_status_untouched() -> None:
    """Mirrors core's ``update_state`` returning False for unknown codes."""
    state = feed(["CL", "YG"])
    assert state.status is SiaStatus.ARMED_AWAY
    assert state.last_code == "CL"


def test_test_messages_update_heartbeat_but_not_status() -> None:
    mapping = StatusMapping.build()
    state = feed(["CL"], mapping=mapping)
    armed_at = state.last_message_at

    result = decode_body(sia_message("RP", sequence="0099"))
    transition = apply_message(state, result, mapping, START + timedelta(hours=1))

    assert transition.is_test
    assert not transition.changed
    assert transition.state.status is SiaStatus.ARMED_AWAY
    assert transition.state.last_message_at > armed_at
    assert transition.state.last_activity_at == state.last_activity_at


def test_null_link_test_is_a_test() -> None:
    result = decode_body('"NULL"0001R0L0#1234[]')
    transition = apply_message(AccountState(), result, StatusMapping.build(), START)

    assert transition.is_test
    assert not transition.changed


def test_transition_reports_before_and_after() -> None:
    mapping = StatusMapping.build()
    state = feed(["CL"], mapping=mapping)

    result = decode_body(sia_message("BA", sequence="0050"))
    transition = apply_message(state, result, mapping, START + timedelta(minutes=5))

    assert transition.before is SiaStatus.ARMED_AWAY
    assert transition.after is SiaStatus.TRIGGERED
    assert transition.changed
    assert transition.code == "BA"


def test_status_override_replaces_default() -> None:
    mapping = StatusMapping.build(status_override={"YG": SiaStatus.PANIC.value})
    assert feed(["YG"], mapping=mapping).status is SiaStatus.PANIC


def test_test_code_override_replaces_defaults() -> None:
    """Overriding the test list makes RP real activity and BA-only tests."""
    mapping = StatusMapping.build(test_codes_override=["BA"])

    result = decode_body(sia_message("RP"))
    assert not apply_message(AccountState(), result, mapping, START).is_test

    result = decode_body(sia_message("BA"))
    assert apply_message(AccountState(), result, mapping, START).is_test


def test_numeric_override_targets_the_contact_id_table() -> None:
    mapping = StatusMapping.build(status_override={"1130": SiaStatus.PANIC.value})
    assert mapping.cid["1130"] == SiaStatus.PANIC.value
    assert "1130" not in mapping.sia


def test_contact_id_open_close_directions() -> None:
    mapping = StatusMapping.build()
    assert mapping.cid["3401"] == SiaStatus.ARMED_AWAY
    assert mapping.cid["1401"] == SiaStatus.DISARMED
    assert mapping.cid["1130"] == SiaStatus.TRIGGERED
    assert mapping.cid["1120"] == SiaStatus.PANIC
    assert mapping.cid["3130"] == PREVIOUS_STATE


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (SiaStatus.PANIC, "triggered"),
        (SiaStatus.TRIGGERED, "triggered"),
        (SiaStatus.ARMED_AWAY, "armed_away"),
        (SiaStatus.UNKNOWN, None),
    ],
)
def test_panel_state_translation(status: SiaStatus, expected: str | None) -> None:
    """``panic`` has no alarm panel equivalent and degrades to ``triggered``."""
    assert panel_state(status) == expected


def test_every_mapped_status_is_valid() -> None:
    for code, status in DEFAULT_SIA_STATUS_MAP.items():
        if status == PREVIOUS_STATE:
            continue
        assert SiaStatus(status), code


def test_sticky_and_armed_sets_are_disjoint() -> None:
    assert not STICKY_STATES & ARMED_STATES
