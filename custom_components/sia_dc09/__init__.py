"""The SIA DC-09 Control Center integration.

Receives alarm messages from panels and control centre transmitters that speak
ANSI/SIA DC-09, and turns each configured account into a Home Assistant device
with an alarm panel, a status sensor, a heartbeat and an activity log.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, PLATFORMS
from .hub import SiaDc09Hub
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)

type SiaDc09ConfigEntry = ConfigEntry[SiaDc09Hub]


async def async_setup_entry(hass: HomeAssistant, entry: SiaDc09ConfigEntry) -> bool:
    """Start listening for DC-09 traffic for one config entry."""
    hub = SiaDc09Hub(hass, entry)

    try:
        await hub.async_setup()
    except OSError as err:
        # Almost always a port already in use, which is worth retrying because
        # the conflicting listener may be another integration still shutting
        # down.
        raise ConfigEntryNotReady(
            f"Could not listen for DC-09 messages: {err}"
        ) from err

    entry.runtime_data = hub
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_setup_services(hass)

    entry.async_on_unload(entry.add_update_listener(async_update_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SiaDc09ConfigEntry) -> bool:
    """Stop listening and tear down the entry's entities."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False

    hub: SiaDc09Hub = hass.data[DOMAIN].pop(entry.entry_id)
    await hub.async_unload()

    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)
        async_unload_services(hass)

    return True


async def async_update_options(hass: HomeAssistant, entry: SiaDc09ConfigEntry) -> None:
    """Reload the entry when its options change.

    Ports, keys and the account list all affect the open sockets and the
    entities, so a full reload is both the simplest and the most predictable
    response.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: SiaDc09ConfigEntry, device_entry
) -> bool:
    """Allow removing the device of an account that is no longer configured."""
    hub: SiaDc09Hub = hass.data[DOMAIN][entry.entry_id]
    prefix = f"{entry.entry_id}_"
    return not any(
        identifier[1].removeprefix(prefix) in hub.accounts
        for identifier in device_entry.identifiers
        if identifier[0] == DOMAIN
    )
