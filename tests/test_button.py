"""Receiver reload control."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.sia_dc09.const import DOMAIN

from .test_integration import (  # noqa: F401
    _auto_enable,
    body,
    hub_of,
    make_entry,
    send_udp,
)


async def test_reload_button_restarts_only_its_receiver(hass: HomeAssistant) -> None:
    entry = await make_entry(hass)
    other = await make_entry(hass, accounts=[{"account": "5678", "name": "Garage"}])
    old_hub = hub_of(hass, entry)
    other_hub = hub_of(hass, other)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_reload"
    )
    assert entity_id is not None
    entity = registry.async_get(entity_id)
    receiver = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, entry.entry_id)}
    )
    assert entity.device_id == receiver.id

    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True
    )
    await hass.async_block_till_done()

    assert hub_of(hass, entry) is not old_hub
    assert hub_of(hass, other) is other_hub
    assert hass.states.get(entity_id).state != "unavailable"
    await send_udp(hass, entry, body("CL501"))
    assert hass.states.get("alarm_control_panel.front_door").state == "armed_away"


async def test_reload_button_reports_failure(hass: HomeAssistant) -> None:
    entry = await make_entry(hass)
    entity_id = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_reload"
    )
    with (
        patch.object(
            hass.config_entries,
            "async_reload",
            new=AsyncMock(return_value=False),
        ),
        pytest.raises(HomeAssistantError, match="could not reload"),
    ):
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
