"""Binary sensors for each configured SIA DC-09 account.

The code tables mirror those in Home Assistant's built-in ``sia`` integration
so the same panel messages produce the same sensor behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    KEY_CONNECTIVITY,
    KEY_MOISTURE,
    KEY_POWER,
    KEY_SMOKE,
)
from .entity import SiaDc09Entity
from .hub import AccountConfig, SiaDc09Hub
from .models import SiaDc09Event

#: ``code -> is_on``. A code that is absent leaves the sensor untouched, which
#: is how an unrelated message avoids clearing an active alarm.
SMOKE_CODES: dict[str, bool] = {
    "GA": True,  # Gas Alarm
    "GH": False,  # Gas Alarm Restore
    "FA": True,  # Fire Alarm
    "FH": False,  # Fire Alarm Restore
    "FR": False,  # Fire Restoral
    "KA": True,  # Heat Alarm
    "KH": False,  # Heat Alarm Restore
    "KR": False,  # Heat Restoral
    "SA": True,  # Sprinkler Alarm
    "SR": False,  # Sprinkler Restoral
}

MOISTURE_CODES: dict[str, bool] = {
    "WA": True,  # Water Alarm
    "WH": False,  # Water Alarm Restore
    "WR": False,  # Water Restoral
    "ZA": True,  # Freeze Alarm
    "ZR": False,  # Freeze Restoral
}

POWER_CODES: dict[str, bool] = {
    "AT": False,  # AC Trouble - mains lost
    "AR": True,  # AC Restoral - mains back
}

BATTERY_CODES: dict[str, bool] = {
    "YT": True,  # System Battery Trouble
    "YR": False,  # System Battery Restoral
    "XT": True,  # Transmitter Battery Trouble
    "XR": False,  # Transmitter Battery Restoral
}


@dataclass(frozen=True, kw_only=True)
class SiaDc09BinarySensorDescription(BinarySensorEntityDescription):
    """Describes a code driven binary sensor."""

    #: Maps a SIA code to the state the sensor should take.
    codes: dict[str, bool]
    #: ``True`` when the sensor should start off rather than unknown.
    default_off: bool = False


DESCRIPTIONS: tuple[SiaDc09BinarySensorDescription, ...] = (
    SiaDc09BinarySensorDescription(
        key=KEY_SMOKE,
        device_class=BinarySensorDeviceClass.SMOKE,
        codes=SMOKE_CODES,
        default_off=True,
    ),
    SiaDc09BinarySensorDescription(
        key=KEY_MOISTURE,
        device_class=BinarySensorDeviceClass.MOISTURE,
        codes=MOISTURE_CODES,
        default_off=True,
    ),
    SiaDc09BinarySensorDescription(
        key=KEY_POWER,
        device_class=BinarySensorDeviceClass.POWER,
        codes=POWER_CODES,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SiaDc09BinarySensorDescription(
        key="battery",
        device_class=BinarySensorDeviceClass.BATTERY,
        codes=BATTERY_CODES,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create the binary sensors for every configured account."""
    hub: SiaDc09Hub = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = []
    for account in hub.accounts.values():
        entities.append(SiaDc09ConnectivitySensor(hub, account))
        entities.extend(
            SiaDc09CodeBinarySensor(hub, account, description)
            for description in DESCRIPTIONS
        )
    async_add_entities(entities)


class SiaDc09ConnectivitySensor(SiaDc09Entity, BinarySensorEntity):
    """Whether the account has reported within its heartbeat window."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: SiaDc09Hub, account: AccountConfig):
        """Build the connectivity sensor for one account."""
        super().__init__(hub, account, KEY_CONNECTIVITY)

    @property
    def is_on(self) -> bool | None:
        """Return whether the link is considered alive."""
        return self.hub.is_online(self.account.account)

    @property
    def available(self) -> bool:
        """Stay available so the user can see the link is down."""
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose when the account was last heard from.

        An attribute rather than a sensor of its own: a timestamp state changes
        on every message, and each change would flood the device's activity
        log. Attribute updates do not appear there.
        """
        state = self.hub.states.get(self.account.account)
        last = state.last_message_at if state else None
        return {
            **super().extra_state_attributes,
            "last_heartbeat": last.isoformat() if last else None,
            "timeout_minutes": self.account.heartbeat_timeout,
        }

    def handle_event(self, event: SiaDc09Event) -> bool:
        """Any message proves the link works, tests included."""
        return True


class SiaDc09CodeBinarySensor(SiaDc09Entity, BinarySensorEntity):
    """A binary sensor driven by a fixed table of SIA codes."""

    entity_description: SiaDc09BinarySensorDescription

    def __init__(
        self,
        hub: SiaDc09Hub,
        account: AccountConfig,
        description: SiaDc09BinarySensorDescription,
    ):
        """Build a code driven binary sensor."""
        super().__init__(hub, account, description.key)
        self.entity_description = description
        self._attr_is_on: bool | None = False if description.default_off else None

    @property
    def is_on(self) -> bool | None:
        """Return the sensor's state."""
        return self._attr_is_on

    def handle_event(self, event: SiaDc09Event) -> bool:
        """Apply the event only if its code appears in this sensor's table."""
        if event.code is None:
            return False
        new_state = self.entity_description.codes.get(event.code.upper())
        if new_state is None or new_state == self._attr_is_on:
            return False
        self._attr_is_on = new_state
        return True

    def async_restore(self, state) -> None:
        """Adopt the state from before the restart."""
        if state.state == "on":
            self._attr_is_on = True
        elif state.state == "off":
            self._attr_is_on = False
