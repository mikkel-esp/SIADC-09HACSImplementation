"""Zone and point names, and who or what last changed an account's status."""

from __future__ import annotations

import pathlib
from contextlib import suppress

import pytest
from homeassistant.core import HomeAssistant

from custom_components.sia_dc09.const import (
    CONF_ACCOUNT,
    CONF_USERS,
    CONF_ZONES,
    DOMAIN,
)
from custom_components.sia_dc09.dc09 import build_frame, decode, enrich
from custom_components.sia_dc09.dc09.enrich import (
    summarise_with_user_names,
    zone_number_of,
)
from custom_components.sia_dc09.utils import parse_zones

from .test_integration import (  # noqa: F401
    ACCOUNT,
    _auto_enable,
    body,
    hub_of,
    make_entry,
    send_udp,
)

PANEL = "alarm_control_panel.front_door"
STATUS = "sensor.front_door_status"


@pytest.fixture(autouse=True)
def _clean_database(hass: HomeAssistant):
    """Remove the shared database so one test cannot seed the next."""

    def purge() -> None:
        path = hass.config.path("sia_dc09.db")
        for name in (path, f"{path}-wal", f"{path}-shm"):
            with suppress(OSError):
                pathlib.Path(name).unlink(missing_ok=True)

    purge()
    yield
    purge()


def named_account(**extra) -> list[dict]:
    """Return an account with one named user and one named zone."""
    return [
        {
            CONF_ACCOUNT: ACCOUNT,
            "name": "Front door",
            CONF_USERS: {"501": "Mikkel"},
            CONF_ZONES: {"10": "Office Window Sensor"},
            **extra,
        }
    ]


def enriched(code: str):
    """Decode and enrich one body."""
    message = enrich(decode(build_frame(body(code))))
    assert message is not None
    return message


# --- pure functions ----------------------------------------------------------


def test_zone_number_comes_from_a_zone_address() -> None:
    """A burglary reports a zone; a closing reports a user, never a zone."""
    assert zone_number_of(enriched("BA010").events[0]) == "10"
    assert zone_number_of(enriched("CL501").events[0]) is None


def test_summary_names_the_zone() -> None:
    """A configured zone reads as its name."""
    summary = summarise_with_user_names(
        enriched("BA10"), None, {"10": "Office Window Sensor"}
    )
    assert summary == "Burglary Alarm - Office Window Sensor (area 1)"


def test_zone_names_never_rename_users() -> None:
    """A user number that matches a zone number keeps its user meaning."""
    summary = summarise_with_user_names(enriched("CL10"), None, {"10": "Office"})
    assert summary == "Closing Report - User number 10 (area 1)"


def test_zone_list_parses_like_the_user_list() -> None:
    """Same format, same normalisation, same duplicate rule."""
    zones, invalid = parse_zones("010: Office Window Sensor\n11 = Hall")
    assert zones == {"10": "Office Window Sensor", "11": "Hall"}
    assert invalid == []
    assert parse_zones("10: A\n010: B")[1] == ["010: B"]


# --- end to end --------------------------------------------------------------


async def test_zone_name_shown_in_activity(hass: HomeAssistant) -> None:
    """The activity log reads the zone's name."""
    entry = await make_entry(hass, accounts=named_account())

    await send_udp(hass, entry, body("BA10"))

    activity = hass.states.get("sensor.front_door_last_activity")
    assert activity.state == "Burglary Alarm - Office Window Sensor (area 1)"
    latest = activity.attributes["events"][0]
    assert latest["zone_number"] == "10"
    assert latest["zone_name"] == "Office Window Sensor"


async def test_changed_by_names_the_user(hass: HomeAssistant) -> None:
    """Arming by a named user puts their name on the panel and the sensor."""
    entry = await make_entry(hass, accounts=named_account())

    await send_udp(hass, entry, body("CL501"))

    panel = hass.states.get(PANEL)
    status = hass.states.get(STATUS)
    assert panel.state == "armed_away"
    assert panel.attributes["changed_by"] == "Mikkel"
    assert status.attributes["changed_by"] == "Mikkel"
    assert status.attributes["changed_by_zoneorpoint"] is None


async def test_changed_by_falls_back_to_the_number(hass: HomeAssistant) -> None:
    """An unnamed user and an unnamed zone are shown by number."""
    entry = await make_entry(hass, accounts=named_account())

    await send_udp(hass, entry, body("CL777", sequence="0001"))
    assert hass.states.get(PANEL).attributes["changed_by"] == "777"

    await send_udp(hass, entry, body("BA42", sequence="0002"))
    status = hass.states.get(STATUS)
    assert status.state == "triggered"
    assert status.attributes["changed_by_zoneorpoint"] == "42"
    # The alarm came from a zone, not from the user who armed earlier.
    assert status.attributes["changed_by"] is None
    assert hass.states.get(PANEL).attributes["changed_by"] is None


async def test_zone_that_triggers_is_named(hass: HomeAssistant) -> None:
    """The zone that set the alarm off is recorded by name."""
    entry = await make_entry(hass, accounts=named_account())

    await send_udp(hass, entry, body("CL501", sequence="0001"))
    await send_udp(hass, entry, body("BA10", sequence="0002"))

    status = hass.states.get(STATUS)
    assert status.attributes["changed_by_zoneorpoint"] == "Office Window Sensor"
    panel = hass.states.get(PANEL)
    assert panel.attributes["changed_by_zoneorpoint"] == "Office Window Sensor"


async def test_message_that_changes_nothing_keeps_changed_by(
    hass: HomeAssistant,
) -> None:
    """Only a status change moves changed_by; tests and repeats do not."""
    entry = await make_entry(hass, accounts=named_account())

    await send_udp(hass, entry, body("CL501", sequence="0001"))
    await send_udp(hass, entry, body("RP", sequence="0002"))
    await send_udp(hass, entry, body("CL777", sequence="0003"))

    assert hass.states.get(PANEL).attributes["changed_by"] == "Mikkel"


async def test_set_status_clears_changed_by(hass: HomeAssistant) -> None:
    """A manual override was not made by any user of the panel."""
    entry = await make_entry(hass, accounts=named_account())
    await send_udp(hass, entry, body("CL501"))

    await hass.services.async_call(
        DOMAIN,
        "set_status",
        {"account": ACCOUNT, "status": "disarmed"},
        blocking=True,
    )

    state = hub_of(hass, entry).states[ACCOUNT]
    assert state.changed_by_user is None
    assert state.changed_by_zone is None


async def test_changed_by_survives_a_restart(hass: HomeAssistant) -> None:
    """The stored activity says who made the last change."""
    entry = await make_entry(hass, accounts=named_account())
    await send_udp(hass, entry, body("CL501", sequence="0001"))
    await send_udp(hass, entry, body("BA10", sequence="0002"))
    await send_udp(hass, entry, body("RP", sequence="0003"))

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hub_of(hass, entry).states[ACCOUNT]
    assert state.changed_by_zone == "10"
    assert state.changed_by_user is None
    assert (
        hass.states.get(STATUS).attributes["changed_by_zoneorpoint"]
        == "Office Window Sensor"
    )
