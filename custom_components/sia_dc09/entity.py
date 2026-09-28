"""Base class for every entity this integration creates."""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import ATTR_ACCOUNT, DOMAIN, SIA_DC09_EVENT, SIA_DC09_HUB_UPDATED
from .hub import AccountConfig, SiaDc09Hub
from .models import SiaDc09Event
from .utils import device_identifier, unique_id


class SiaDc09Entity(RestoreEntity, Entity):
    """One entity belonging to one monitored account.

    Entities are push driven: the hub dispatches an event and the entity
    decides whether it says anything about its own state. An entity that does
    not recognise an event writes nothing, which is what keeps an unrelated
    code from resetting an alarm.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: SiaDc09Hub, account: AccountConfig, key: str):
        """Build the entity for one account and one measurement."""
        self.hub = hub
        self.account = account
        self.key = key
        self._available = True
        self._attr_unique_id = unique_id(hub.entry.entry_id, account.account, key)
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={device_identifier(hub.entry.entry_id, account.account)},
            name=account.name,
            manufacturer="SIA DC-09",
            model="Alarm transmitter",
            via_device=(DOMAIN, hub.entry.entry_id),
        )

    @property
    def available(self) -> bool:
        """Return whether the account has been heard from recently.

        An account that has never reported is still available: the user has
        configured it and is waiting for its first message.
        """
        online = self.hub.is_online(self.account.account)
        return online is not False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return attributes common to every entity of an account."""
        return {ATTR_ACCOUNT: self.account.account}

    async def async_added_to_hass(self) -> None:
        """Subscribe to this account's events and to hub level updates."""
        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            self.async_restore(last_state)

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIA_DC09_EVENT.format(self.account.account),
                self._async_handle_event,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIA_DC09_HUB_UPDATED.format(self.hub.entry.entry_id),
                self._async_handle_hub_update,
            )
        )

    def async_restore(self, state) -> None:
        """Adopt the entity's state from before the restart.

        Alarm status is not something a panel repeats on demand, so losing it
        across a Home Assistant restart would leave the user blind until the
        next message.
        """

    @abstractmethod
    def handle_event(self, event: SiaDc09Event) -> bool:
        """Apply an event and return whether the entity's state changed."""

    @callback
    def _async_handle_event(self, event: SiaDc09Event) -> None:
        """Write state only when the event actually said something."""
        if self.handle_event(event):
            self._available = self.available
            self.async_write_ha_state()

    @callback
    def _async_handle_hub_update(self) -> None:
        """Refresh derived state, but only when something visible changed.

        This fires on a timer as well as on traffic, so writing every time
        would churn the state machine for no reason.
        """
        available = self.available
        if available != self._available:
            self._available = available
            self.async_write_ha_state()
