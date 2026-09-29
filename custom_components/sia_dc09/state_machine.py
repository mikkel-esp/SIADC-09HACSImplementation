"""Derives an account's alarm status from the order of received messages.

The default mapping is a strict superset of the ``code_consequences`` tables in
Home Assistant's built-in ``sia`` integration, so a user migrating from it never
loses behaviour. :func:`core_parity_gaps` proves that claim, and the test suite
asserts it is empty.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final

from .dc09 import AlarmEvent, DecodeResult


class SiaStatus(StrEnum):
    """Status of an account.

    Values deliberately match ``AlarmControlPanelState`` so the alarm panel can
    reuse them directly, with ``PANIC`` as the one addition. Home Assistant has
    no ``panic`` panel state, so the panel reports ``triggered`` for it while the
    status sensor keeps the distinction.
    """

    DISARMED = "disarmed"
    ARMED_HOME = "armed_home"
    ARMED_AWAY = "armed_away"
    ARMED_NIGHT = "armed_night"
    ARMED_VACATION = "armed_vacation"
    ARMED_CUSTOM_BYPASS = "armed_custom_bypass"
    ARMING = "arming"
    DISARMING = "disarming"
    PENDING = "pending"
    TRIGGERED = "triggered"
    PANIC = "panic"
    UNKNOWN = "unknown"


#: Sentinel meaning "return to whatever the state was before the alarm".
#: Named after, and behaving exactly like, core's ``PREVIOUS_STATE``.
PREVIOUS_STATE: Final = "previous_state"

#: States that are held until a disarm or an explicit restore clears them.
STICKY_STATES: Final[frozenset[SiaStatus]] = frozenset(
    {SiaStatus.TRIGGERED, SiaStatus.PANIC}
)

#: States that represent an armed condition of some kind.
ARMED_STATES: Final[frozenset[SiaStatus]] = frozenset(
    {
        SiaStatus.ARMED_AWAY,
        SiaStatus.ARMED_HOME,
        SiaStatus.ARMED_NIGHT,
        SiaStatus.ARMED_VACATION,
        SiaStatus.ARMED_CUSTOM_BYPASS,
    }
)

#: SIA data codes that move the status, as ``code -> status or PREVIOUS_STATE``.
#:
#: Entries marked "core" are taken verbatim from
#: ``homeassistant/components/sia/alarm_control_panel.py`` and must not be
#: dropped or re-pointed; the rest extend them.
DEFAULT_SIA_STATUS_MAP: Final[dict[str, str]] = {
    # --- Triggered ---------------------------------------------------------
    "BA": SiaStatus.TRIGGERED,  # core - Burglary Alarm
    "TA": SiaStatus.TRIGGERED,  # core - Tamper Alarm
    "JA": SiaStatus.TRIGGERED,  # core - User Code Tamper
    "FA": SiaStatus.TRIGGERED,  # Fire Alarm
    "GA": SiaStatus.TRIGGERED,  # Gas Alarm
    "KA": SiaStatus.TRIGGERED,  # Heat Alarm
    "MA": SiaStatus.TRIGGERED,  # Medical Alarm
    "SA": SiaStatus.TRIGGERED,  # Sprinkler Alarm
    "WA": SiaStatus.TRIGGERED,  # Water Alarm
    "ZA": SiaStatus.TRIGGERED,  # Freeze Alarm
    "UA": SiaStatus.TRIGGERED,  # Untyped Zone Alarm
    # --- Panic -------------------------------------------------------------
    # Core folds these into TRIGGERED; splitting them out is a superset, and the
    # alarm panel still reports TRIGGERED so core's behaviour is preserved.
    "PA": SiaStatus.PANIC,  # core (as triggered) - Panic Alarm
    "HA": SiaStatus.PANIC,  # core (as triggered) - Holdup Alarm
    "QA": SiaStatus.PANIC,  # Emergency Alarm
    # --- Armed away --------------------------------------------------------
    "CA": SiaStatus.ARMED_AWAY,  # core - Automatic Closing
    "CB": SiaStatus.ARMED_AWAY,  # core
    "CG": SiaStatus.ARMED_AWAY,  # core - Close Area
    "CL": SiaStatus.ARMED_AWAY,  # core - Closing Report
    "CP": SiaStatus.ARMED_AWAY,  # core - Automatic Closing
    "CQ": SiaStatus.ARMED_AWAY,  # core - Remote Closing
    "CS": SiaStatus.ARMED_AWAY,  # core - Closing Keyswitch
    "CF": SiaStatus.ARMED_AWAY,  # core - Forced Closing
    "CJ": SiaStatus.ARMED_AWAY,  # Late Close
    "CR": SiaStatus.ARMED_AWAY,  # Recent Closing
    # --- Armed night -------------------------------------------------------
    "NC": SiaStatus.ARMED_NIGHT,  # core
    "NL": SiaStatus.ARMED_NIGHT,  # core - Perimeter Armed
    "NE": SiaStatus.ARMED_NIGHT,  # core
    "NF": SiaStatus.ARMED_NIGHT,  # core - Forced Perimeter Arm
    # --- Disarmed ----------------------------------------------------------
    "NP": SiaStatus.DISARMED,  # core
    "NO": SiaStatus.DISARMED,  # core
    "OA": SiaStatus.DISARMED,  # core - Automatic Opening
    "OB": SiaStatus.DISARMED,  # core
    "OG": SiaStatus.DISARMED,  # core - Open Area
    "OP": SiaStatus.DISARMED,  # core - Opening Report
    "OQ": SiaStatus.DISARMED,  # core - Remote Opening
    "OR": SiaStatus.DISARMED,  # core - Disarm From Alarm
    "OS": SiaStatus.DISARMED,  # core - Opening Keyswitch
    "OK": SiaStatus.DISARMED,  # Early Open
    "OJ": SiaStatus.DISARMED,  # Late Open
    "OH": SiaStatus.DISARMED,  # Early to Open from Alarm
    # --- Restores and cancels ----------------------------------------------
    "BR": PREVIOUS_STATE,  # core - Burglary Restoral
    "BH": PREVIOUS_STATE,  # Burglary Alarm Restore
    "BC": PREVIOUS_STATE,  # Burglary Cancel
    "OC": PREVIOUS_STATE,  # Cancel Report
    "FR": PREVIOUS_STATE,  # Fire Restoral
    "FH": PREVIOUS_STATE,  # Fire Alarm Restore
    "GR": PREVIOUS_STATE,  # Gas Restoral
    "GH": PREVIOUS_STATE,  # Gas Alarm Restore
    "KR": PREVIOUS_STATE,  # Heat Restoral
    "KH": PREVIOUS_STATE,  # Heat Alarm Restore
    "WR": PREVIOUS_STATE,  # Water Restoral
    "WH": PREVIOUS_STATE,  # Water Alarm Restore
    "ZR": PREVIOUS_STATE,  # Freeze Restoral
    "SR": PREVIOUS_STATE,  # Sprinkler Restoral
    "TR": PREVIOUS_STATE,  # Tamper Restoral
    "TH": PREVIOUS_STATE,  # Tamper Alarm Restore
    "PR": PREVIOUS_STATE,  # Panic Restoral
    "PH": PREVIOUS_STATE,  # Panic Alarm Restore
    "HR": PREVIOUS_STATE,  # Holdup Restoral
    "HH": PREVIOUS_STATE,  # Holdup Alarm Restore
    "MR": PREVIOUS_STATE,  # Medical Restoral
    "MH": PREVIOUS_STATE,  # Medical Alarm Restore
    "QR": PREVIOUS_STATE,  # Emergency Restoral
    "QH": PREVIOUS_STATE,  # Emergency Alarm Restore
    "UR": PREVIOUS_STATE,  # Untyped Zone Restoral
    "UH": PREVIOUS_STATE,  # Untyped Alarm Restore
}

#: Contact ID event codes that move the status. Contact ID carries the meaning
#: in the qualifier as well as the code, so these are keyed ``qualifier + code``
#: where the qualifier is ``1`` (new event) or ``3`` (restore).
DEFAULT_CID_STATUS_MAP: Final[dict[str, str]] = {
    # Panic, duress and holdup.
    **{f"1{code}": SiaStatus.PANIC for code in ("120", "121", "122", "123", "126")},
    # Every other 1xx alarm.
    **{
        f"1{code:03d}": SiaStatus.TRIGGERED
        for code in (*range(100, 120), *range(130, 160))
    },
    # Open / close. Qualifier 1 is the "open" (disarm) direction in Contact ID,
    # qualifier 3 is the "close" (arm) direction.
    **{
        f"1{code}": SiaStatus.DISARMED
        for code in ("401", "402", "403", "407", "408", "409")
    },
    **{
        f"3{code}": SiaStatus.ARMED_AWAY
        for code in ("401", "402", "403", "407", "408", "409")
    },
    "3441": SiaStatus.ARMED_HOME,  # Armed STAY
    "3442": SiaStatus.ARMED_HOME,  # Keyswitch Armed STAY
    "3456": SiaStatus.ARMED_HOME,  # Partial Arm
    "1406": PREVIOUS_STATE,  # Cancel
    "1465": PREVIOUS_STATE,  # Panic Alarm Reset
    # Restores of the alarm codes above.
    **{f"3{code:03d}": PREVIOUS_STATE for code in range(100, 160)},
}

#: SIA codes that are routine supervision rather than real activity.
DEFAULT_TEST_CODES: Final[frozenset[str]] = frozenset(
    {
        "RP",  # Automatic Test
        "RX",  # Manual Test
        "TX",  # Test Report
        "TS",  # Test Start
        "TE",  # Test End
        "TP",  # Walk Test Point
    }
)

#: Contact ID codes that are routine supervision.
DEFAULT_CID_TEST_CODES: Final[frozenset[str]] = frozenset(
    {"602", "603", "608", "1602", "1603", "1608"}
)

#: SIA codes that report the panel is alive, used by the connectivity sensor.
HEARTBEAT_CODES: Final[frozenset[str]] = frozenset({"RP"})


@dataclass(frozen=True, slots=True)
class AccountState:
    """The status of one account, and enough history to undo an alarm."""

    status: SiaStatus = SiaStatus.UNKNOWN
    #: State to return to when a restore or cancel arrives.
    previous: SiaStatus = SiaStatus.UNKNOWN
    last_message_at: datetime | None = None
    last_activity_at: datetime | None = None
    last_code: str | None = None


@dataclass(frozen=True, slots=True)
class StatusMapping:
    """The code tables in force for one account."""

    sia: dict[str, str]
    cid: dict[str, str]
    test_codes: frozenset[str]
    cid_test_codes: frozenset[str]

    @classmethod
    def build(
        cls,
        status_override: dict[str, str] | None = None,
        test_codes_override: list[str] | None = None,
    ) -> StatusMapping:
        """Layer per-account overrides on top of the defaults."""
        sia = dict(DEFAULT_SIA_STATUS_MAP)
        cid = dict(DEFAULT_CID_STATUS_MAP)
        for code, status in (status_override or {}).items():
            key = code.upper()
            if key.isdigit():
                cid[key] = status
            else:
                sia[key] = status

        if test_codes_override is None:
            return cls(sia, cid, DEFAULT_TEST_CODES, DEFAULT_CID_TEST_CODES)

        sia_tests = frozenset(
            code.upper() for code in test_codes_override if not code.isdigit()
        )
        cid_tests = frozenset(code for code in test_codes_override if code.isdigit())
        return cls(sia, cid, sia_tests, cid_tests)

    def consequence(self, event: AlarmEvent, is_cid: bool) -> str | None:
        """Return the mapped status for an event, or ``None`` if unmapped."""
        if is_cid:
            qualified = f"{event.qualifier or ''}{event.code}"
            return self.cid.get(qualified) or self.cid.get(event.code)
        return self.sia.get(event.code.upper())

    def is_test(self, event: AlarmEvent, is_cid: bool) -> bool:
        """Return whether an event is routine supervision."""
        if is_cid:
            return (
                event.code in self.cid_test_codes
                or f"{event.qualifier or ''}{event.code}" in self.cid_test_codes
            )
        return event.code.upper() in self.test_codes


@dataclass(frozen=True, slots=True)
class StatusTransition:
    """What one message did to an account."""

    state: AccountState
    before: SiaStatus
    after: SiaStatus
    changed: bool
    is_test: bool
    #: Code that drove the transition, if any.
    code: str | None = None


def apply_message(
    state: AccountState,
    result: DecodeResult,
    mapping: StatusMapping,
    received_at: datetime,
) -> StatusTransition:
    """Fold one decoded message into an account's state.

    Every message updates ``last_message_at`` - that is the heartbeat. Only
    mapped, non-test codes move the status; an unmapped code leaves the state
    untouched, matching core's ``update_state`` returning ``False``.
    """
    before = state.status
    payload = result.payload

    link_test = payload is not None and payload.link_test
    is_cid = payload is not None and payload.protocol == "ADM-CID"

    events = payload.events if payload is not None else ()
    is_test = link_test or (
        bool(events) and all(mapping.is_test(event, is_cid) for event in events)
    )

    state = replace(state, last_message_at=received_at)

    if is_test or not events:
        return StatusTransition(
            state=state, before=before, after=before, changed=False, is_test=True
        )

    status = state.status
    previous = state.previous
    driving_code: str | None = None

    for event in events:
        consequence = mapping.consequence(event, is_cid)
        if consequence is None:
            continue

        driving_code = event.code.upper()

        if consequence == PREVIOUS_STATE:
            # Only a restore of a sticky state actually rewinds; a restore that
            # arrives while nothing is triggered is informational.
            if status in STICKY_STATES:
                status, previous = previous, status
            continue

        new_status = SiaStatus(consequence)

        # Triggered and panic are sticky: a plain arm/disarm-free event cannot
        # silently clear them, but a disarm always can, and panic outranks
        # triggered.
        if status in STICKY_STATES:
            if new_status == SiaStatus.DISARMED:
                status = previous = SiaStatus.DISARMED
            elif new_status == SiaStatus.PANIC:
                # Panic outranks triggered; an equal-rank alarm changes nothing.
                status = SiaStatus.PANIC
            elif new_status not in STICKY_STATES:
                # Arming while an alarm stands only records where to return to.
                previous = new_status
            continue

        if new_status in STICKY_STATES:
            previous = status
        status = new_status

    state = replace(
        state,
        status=status,
        previous=previous,
        last_activity_at=received_at,
        last_code=driving_code or state.last_code,
    )

    return StatusTransition(
        state=state,
        before=before,
        after=status,
        changed=status != before,
        is_test=False,
        code=driving_code,
    )


def restore_state(
    statuses: Sequence[str],
    last_message_at: datetime | None = None,
    last_activity_at: datetime | None = None,
    last_code: str | None = None,
) -> AccountState:
    """Rebuild an account's state from the statuses its messages produced.

    Home Assistant learns nothing about a panel until it speaks, and a quiet
    alarm may not speak for hours, so after a restart the stored history is the
    only evidence of whether the house is armed.

    Replaying the recorded statuses rather than the raw codes means the
    restored state can only ever be what this integration already decided at
    the time. The one thing that has to be worked out again is which state to
    return to when an alarm is restored, which is the last state that was not
    itself an alarm.
    """
    status = SiaStatus.UNKNOWN
    previous = SiaStatus.UNKNOWN

    for value in statuses:
        try:
            after = SiaStatus(value)
        except ValueError:
            # A status written by a newer version, or a corrupted row. Skipping
            # it is better than abandoning the whole restore.
            continue

        if after in STICKY_STATES:
            if status not in STICKY_STATES:
                previous = status
        else:
            previous = after
        status = after

    return AccountState(
        status=status,
        previous=previous,
        last_message_at=last_message_at,
        last_activity_at=last_activity_at,
        last_code=last_code,
    )


def core_parity_gaps() -> dict[str, str]:
    """Return core SIA codes this integration would handle differently.

    Used by the test suite to guarantee the default mapping never regresses
    against Home Assistant's built-in ``sia`` integration. An empty result means
    full parity.
    """
    # Transcribed from homeassistant/components/sia/alarm_control_panel.py.
    core_map = {
        "PA": "triggered",
        "JA": "triggered",
        "TA": "triggered",
        "BA": "triggered",
        "HA": "triggered",
        "CA": "armed_away",
        "CB": "armed_away",
        "CG": "armed_away",
        "CL": "armed_away",
        "CP": "armed_away",
        "CQ": "armed_away",
        "CS": "armed_away",
        "CF": "armed_away",
        "NP": "disarmed",
        "NO": "disarmed",
        "OA": "disarmed",
        "OB": "disarmed",
        "OG": "disarmed",
        "OP": "disarmed",
        "OQ": "disarmed",
        "OR": "disarmed",
        "OS": "disarmed",
        "NC": "armed_night",
        "NL": "armed_night",
        "NE": "armed_night",
        "NF": "armed_night",
        "BR": PREVIOUS_STATE,
    }

    gaps: dict[str, str] = {}
    for code, expected in core_map.items():
        actual = DEFAULT_SIA_STATUS_MAP.get(code)
        if actual is None:
            gaps[code] = f"missing (core maps it to {expected})"
        elif actual != expected and not (
            # Panic is reported as triggered by the alarm panel, so mapping a
            # core "triggered" code to panic preserves core's behaviour.
            expected == "triggered" and actual == SiaStatus.PANIC
        ):
            gaps[code] = f"maps to {actual}, core maps it to {expected}"
    return gaps


def panel_state(status: SiaStatus) -> str | None:
    """Translate a status into a value ``alarm_control_panel`` accepts."""
    if status is SiaStatus.UNKNOWN:
        return None
    if status is SiaStatus.PANIC:
        return SiaStatus.TRIGGERED.value
    return status.value
