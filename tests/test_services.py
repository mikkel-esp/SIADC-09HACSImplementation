"""Tests for the services, diagnostics and logbook surfaces."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.sia_dc09.const import (
    DOMAIN,
    SERVICE_CLEAR_ACTIVITY,
    SERVICE_DECODE_MESSAGE,
    SERVICE_GET_ACTIVITY,
    SERVICE_PURGE,
    SERVICE_SET_STATUS,
)
from custom_components.sia_dc09.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.test_integration import ACCOUNT, KEY_HEX, body, make_entry, send_udp


@pytest.fixture(autouse=True)
def _auto_enable(enable_custom_integrations):
    """Let Home Assistant load the integration from custom_components."""
    return


async def test_services_are_registered(hass: HomeAssistant) -> None:
    """Setting up an entry registers every service."""
    await make_entry(hass)
    for service in (
        SERVICE_GET_ACTIVITY,
        SERVICE_CLEAR_ACTIVITY,
        SERVICE_PURGE,
        SERVICE_SET_STATUS,
        SERVICE_DECODE_MESSAGE,
    ):
        assert hass.services.has_service(DOMAIN, service)


async def test_get_activity_returns_events(hass: HomeAssistant) -> None:
    """The service returns what the store holds."""
    entry = await make_entry(hass)
    await send_udp(hass, entry, body("BA001"))
    await send_udp(hass, entry, body("RP000", sequence="0002"))

    response = await hass.services.async_call(
        DOMAIN, SERVICE_GET_ACTIVITY, {}, blocking=True, return_response=True
    )
    assert response["count"] == 1
    assert response["events"][0]["code"] == "BA"

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_ACTIVITY,
        {"include_tests": True},
        blocking=True,
        return_response=True,
    )
    assert response["count"] == 2


async def test_get_activity_filters_by_account(hass: HomeAssistant) -> None:
    """An unknown account is a validation error, not an empty result."""
    await make_entry(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_ACTIVITY,
            {"account": "DEAD"},
            blocking=True,
            return_response=True,
        )


async def test_clear_activity(hass: HomeAssistant) -> None:
    """Clearing empties the log."""
    entry = await make_entry(hass)
    await send_udp(hass, entry, body("BA001"))

    await hass.services.async_call(DOMAIN, SERVICE_CLEAR_ACTIVITY, {}, blocking=True)

    response = await hass.services.async_call(
        DOMAIN, SERVICE_GET_ACTIVITY, {}, blocking=True, return_response=True
    )
    assert response["count"] == 0


async def test_set_status_overrides(hass: HomeAssistant) -> None:
    """A lost closing report can be corrected by hand."""
    entry = await make_entry(hass)
    await send_udp(hass, entry, body("BA001"))
    assert hass.states.get("sensor.front_door_status").state == "triggered"

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STATUS,
        {"account": ACCOUNT, "status": "disarmed"},
        blocking=True,
    )
    await hass.async_block_till_done()

    hub = hass.data[DOMAIN][entry.entry_id]
    assert hub.status_of(ACCOUNT).value == "disarmed"


async def test_set_status_rejects_unknown_account(hass: HomeAssistant) -> None:
    """Setting the status of an account that is not configured fails loudly."""
    await make_entry(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_STATUS,
            {"account": "DEAD", "status": "disarmed"},
            blocking=True,
        )


async def test_decode_message_service(hass: HomeAssistant) -> None:
    """A raw message can be decoded for troubleshooting."""
    await make_entry(hass)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_DECODE_MESSAGE,
        {"message": body("BA001")},
        blocking=True,
        return_response=True,
    )
    assert response["ok"] is True
    assert response["frame"]["account"] == ACCOUNT
    assert response["events"][0]["code"] == "BA"


async def test_decode_message_rejects_a_bad_key(hass: HomeAssistant) -> None:
    """A malformed key is reported rather than silently ignored."""
    await make_entry(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_DECODE_MESSAGE,
            {"message": body("BA001"), "key": "nope"},
            blocking=True,
            return_response=True,
        )


async def test_purge_runs(hass: HomeAssistant) -> None:
    """Purging keeps recent activity because it is inside the window."""
    entry = await make_entry(hass)
    await send_udp(hass, entry, body("BA001"))

    await hass.services.async_call(DOMAIN, SERVICE_PURGE, {}, blocking=True)

    response = await hass.services.async_call(
        DOMAIN, SERVICE_GET_ACTIVITY, {}, blocking=True, return_response=True
    )
    assert response["count"] == 1


async def test_services_removed_on_unload(hass: HomeAssistant) -> None:
    """The last entry going away takes the services with it."""
    entry = await make_entry(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, SERVICE_GET_ACTIVITY)


async def test_diagnostics_redacts_the_key(hass: HomeAssistant) -> None:
    """Diagnostics must never contain an encryption key."""
    entry = await make_entry(
        hass,
        accounts=[
            {"account": ACCOUNT, "name": "Front door", "encryption_key": KEY_HEX}
        ],
    )
    await send_udp(hass, entry, body("BA001"))

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert KEY_HEX not in str(diagnostics)
    assert diagnostics["accounts"][0]["encrypted"] is True
    assert diagnostics["accounts"][0]["status"] == "triggered"
    assert diagnostics["receiver"]["message_count"] == 1
    assert diagnostics["stored_events"] == 1
    assert diagnostics["recent_activity"][0]["remote_ip"] == "**REDACTED**"


async def test_logbook_describes_an_event(hass: HomeAssistant) -> None:
    """A bus event becomes a readable logbook line."""
    from custom_components.sia_dc09.logbook import async_describe_events

    entry = await make_entry(hass)
    described = {}

    def _capture(domain, event_type, describer):
        described[event_type] = describer

    async_describe_events(hass, _capture)

    events = []
    hass.bus.async_listen("sia_dc09_event", events.append)
    await send_udp(hass, entry, body("BA001"))

    description = described["sia_dc09_event"](events[0])
    assert description["name"] == "Front door Status"
    assert "Burglary Alarm" in description["message"]
    assert "now triggered" in description["message"]
    assert description["entity_id"] == "sensor.front_door_status"
