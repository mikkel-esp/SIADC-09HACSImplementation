"""The SIA DC-09 Control Center integration.

Receives alarm messages from panels and control centre transmitters that speak
ANSI/SIA DC-09, and turns each configured account into a Home Assistant device
with an alarm panel, a status sensor, a heartbeat and an activity log.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.typing import ConfigType

from .const import CONF_ACCOUNT, CONF_ACCOUNTS, DOMAIN, PLATFORMS
from .hub import SiaDc09Hub
from .services import (
    async_setup_reload_service,
    async_setup_services,
    async_unload_services,
)
from .store import ActivityStore
from .utils import normalise_account

_LOGGER = logging.getLogger(__name__)

type SiaDc09ConfigEntry = ConfigEntry[SiaDc09Hub]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services that must exist even when no receiver is running."""
    async_setup_reload_service(hass)
    return True


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

    _async_register_hub_device(hass, entry)
    _async_remove_stale_devices(hass, entry, hub)

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # The sockets are already open at this point. Home Assistant does not
        # call async_unload_entry for an entry that failed to set up, so
        # leaving them bound would make every later retry fail with "port in
        # use" until Home Assistant itself restarts.
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await hub.async_unload()
        raise

    async_setup_services(hass)

    entry.async_on_unload(entry.add_update_listener(async_update_options))
    return True


@callback
def _async_register_hub_device(hass: HomeAssistant, entry: SiaDc09ConfigEntry) -> None:
    """Create the receiver's device before any account refers to it.

    Account devices name the receiver as their ``via_device``. The platform
    that creates the receiver's own device loads after them, so without this
    they would point at a device that does not exist yet, which Home Assistant
    warns about and will stop accepting.
    """
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="SIA DC-09",
        model="Receiver",
        entry_type=dr.DeviceEntryType.SERVICE,
    )


@callback
def _async_remove_stale_devices(
    hass: HomeAssistant, entry: SiaDc09ConfigEntry, hub: SiaDc09Hub
) -> None:
    """Drop devices for accounts that are no longer configured.

    Removing an account in the options flow otherwise leaves its device and
    entities behind as permanently unavailable clutter, which the user then has
    to delete by hand one at a time.
    """
    registry = dr.async_get(hass)
    prefix = f"{entry.entry_id}_"

    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        for domain, identifier in device.identifiers:
            if domain != DOMAIN or not identifier.startswith(prefix):
                continue
            if identifier.removeprefix(prefix) not in hub.accounts:
                _LOGGER.debug("Removing device for unconfigured account %s", identifier)
                registry.async_update_device(
                    device.id, remove_config_entry_id=entry.entry_id
                )
            break


async def async_unload_entry(hass: HomeAssistant, entry: SiaDc09ConfigEntry) -> bool:
    """Stop listening and tear down the entry's entities."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False

    # Tolerate a partially set up entry: refusing to unload here would leave
    # the user unable to delete the integration at all.
    entries: dict[str, SiaDc09Hub] = hass.data.get(DOMAIN, {})
    hub = entries.pop(entry.entry_id, None)
    if hub is not None:
        await hub.async_unload()

    if DOMAIN in hass.data and not entries:
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


async def async_remove_entry(hass: HomeAssistant, entry: SiaDc09ConfigEntry) -> None:
    """Delete the entry's stored activity when the integration is removed.

    Home Assistant removes the config entry, its devices and its entities on
    its own, but the activity database lives outside all of that, so deleting
    the integration would otherwise leave two years of history on disk with
    nothing left to read it.
    """
    await ActivityStore(hass, entry.entry_id).async_remove()


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: SiaDc09ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Allow removing the device of an account that is no longer configured.

    Refusals explain themselves, because "rejected by integration" on its own
    leaves the user with no idea what to do instead.
    """
    configured = {
        normalise_account(str(account.get(CONF_ACCOUNT, "")))
        for account in {**entry.data, **entry.options}.get(CONF_ACCOUNTS, [])
    }
    prefix = f"{entry.entry_id}_"

    for domain, identifier in device_entry.identifiers:
        if domain != DOMAIN:
            continue
        if not identifier.startswith(prefix):
            # The receiver's own device, identified by the entry id alone. It
            # represents the config entry, so Home Assistant would recreate it
            # on the next reload and the deletion would look like it failed.
            raise HomeAssistantError(
                "The receiver cannot be deleted on its own because it is the "
                "integration itself. Use the three dot menu on the SIA DC-09 "
                "card and choose Delete to remove the receiver, its accounts "
                "and its stored activity."
            )
        account = identifier.removeprefix(prefix)
        if account in configured:
            raise HomeAssistantError(
                f"Account {account} is still configured, so its device would "
                "come back on the next reload. Remove the account first under "
                "Configure, Remove accounts."
            )
    return True
