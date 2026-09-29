"""Restoring an account's status after a restart.

A panel only reports changes. Nothing arrives to say "still armed", so without
restoring from stored activity an alarm left armed overnight reads as unknown
until morning.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.sia_dc09.state_machine import (
    AccountState,
    SiaStatus,
    restore_state,
)
from custom_components.sia_dc09.store import ActivityRecord, ActivityStore

from .test_integration import (  # noqa: F401
    ACCOUNT,
    _auto_enable,
    body,
    hub_of,
    make_entry,
    send_udp,
)


@pytest.fixture(autouse=True)
def _clean_database(hass: HomeAssistant):
    """Remove the shared database so one test cannot seed the next."""

    def purge() -> None:
        path = hass.config.path("sia_dc09.db")
        for name in (path, f"{path}-wal", f"{path}-shm"):
            try:
                import pathlib

                pathlib.Path(name).unlink(missing_ok=True)
            except OSError:  # pragma: no cover - Windows file locking
                pass

    purge()
    yield
    purge()


def test_restore_reads_the_last_status() -> None:
    """The most recent stored status is where the account stands."""
    state = restore_state(["disarmed", "armed_away"])

    assert state.status is SiaStatus.ARMED_AWAY


def test_restore_of_nothing_is_unknown() -> None:
    """An account that has never sent anything cannot be guessed at."""
    assert restore_state([]).status is SiaStatus.UNKNOWN


def test_restore_remembers_what_to_return_to() -> None:
    """A restore after a reboot has to rewind to the armed state."""
    state = restore_state(["armed_away", "triggered"])

    assert state.status is SiaStatus.TRIGGERED
    assert state.previous is SiaStatus.ARMED_AWAY


def test_restore_keeps_the_state_before_the_alarm() -> None:
    """A second alarm while triggered must not overwrite the return state."""
    state = restore_state(["armed_night", "triggered", "panic"])

    assert state.status is SiaStatus.PANIC
    assert state.previous is SiaStatus.ARMED_NIGHT


def test_restore_ignores_an_unreadable_status() -> None:
    """A row from a newer version must not abandon the whole restore."""
    state = restore_state(["armed_away", "nonsense", "disarmed"])

    assert state.status is SiaStatus.DISARMED


def test_restore_carries_the_timestamps() -> None:
    """The heartbeat is as stale as the last message, not as the restart."""
    seen = dt_util.utcnow() - timedelta(hours=3)
    state = restore_state(
        ["armed_away"], last_message_at=seen, last_activity_at=seen, last_code="CL"
    )

    assert state.last_message_at == seen
    assert state.last_activity_at == seen
    assert state.last_code == "CL"


async def test_status_survives_a_restart(hass: HomeAssistant) -> None:
    """The end to end case: arm, restart Home Assistant, still armed."""
    entry = await make_entry(hass)
    await send_udp(hass, entry, body("CL"))
    await hass.async_block_till_done()
    assert hass.states.get("alarm_control_panel.front_door").state == "armed_away"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("alarm_control_panel.front_door").state == "armed_away"
    assert hass.states.get("sensor.front_door_status").state == "armed_away"


async def test_restart_restores_the_heartbeat(hass: HomeAssistant) -> None:
    """A restored account still knows when it last heard from the panel."""
    entry = await make_entry(hass)
    await send_udp(hass, entry, body("CL"))
    await hass.async_block_till_done()
    before = hub_of(hass, entry).states[ACCOUNT].last_message_at

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    restored = hub_of(hass, entry).states[ACCOUNT]
    assert restored.last_message_at is not None
    assert abs((restored.last_message_at - before).total_seconds()) < 1


async def test_restart_leaves_a_silent_account_unknown(hass: HomeAssistant) -> None:
    """Inventing a status for a panel that has never reported would be worse."""
    entry = await make_entry(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hub_of(hass, entry).states[ACCOUNT].status is SiaStatus.UNKNOWN


async def test_reload_does_not_overwrite_a_known_status(hass: HomeAssistant) -> None:
    """Restoring is for a cold start, not for every options change."""
    entry = await make_entry(hass)
    hub = hub_of(hass, entry)
    hub.states[ACCOUNT] = AccountState(status=SiaStatus.ARMED_HOME)

    await hub.async_restore_states()

    assert hub.states[ACCOUNT].status is SiaStatus.ARMED_HOME


async def test_restore_reads_only_its_own_entry(hass: HomeAssistant) -> None:
    """The database is shared, so another receiver's rows must not leak in."""
    entry = await make_entry(hass)
    other = ActivityStore(hass, "some-other-entry")
    await other.async_setup()
    await other.async_add(
        ActivityRecord(
            account=ACCOUNT,
            received_at=dt_util.utcnow(),
            transport="udp",
            local_port=1,
            status_after="armed_away",
        )
    )
    await other.async_flush()
    await other.async_close()

    hub = hub_of(hass, entry)
    hub.states[ACCOUNT] = AccountState()
    await hub.async_restore_states()

    assert hub.states[ACCOUNT].status is SiaStatus.UNKNOWN
