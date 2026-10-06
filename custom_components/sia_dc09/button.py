"""Receiver-level controls."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add a reload control to this receiver."""
    async_add_entities([SiaDc09ReloadButton(entry)])


class SiaDc09ReloadButton(ButtonEntity):
    """Reload this receiver without restarting Home Assistant."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "reload"
    _attr_icon = "mdi:reload"

    def __init__(self, entry: ConfigEntry) -> None:
        """Associate the button with the receiver, not an account."""
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_reload"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="SIA DC-09",
            model="Receiver",
            entry_type="service",
        )

    async def async_press(self) -> None:
        """Reload the entry and surface an unsuccessful restart."""
        if not await self.hass.config_entries.async_reload(self._entry_id):
            raise HomeAssistantError(
                "SIA DC-09 receiver could not reload. Check the Home Assistant logs."
            )
