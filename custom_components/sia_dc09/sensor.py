"""Sensors for each configured SIA DC-09 account."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, ClassVar

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_RECENT_EVENTS,
    DEFAULT_RECENT_EVENTS,
    DOMAIN,
    KEY_LAST_ACTIVITY,
    KEY_LAST_HEARTBEAT,
    KEY_MESSAGES,
    KEY_STATUS,
    KEY_UNKNOWN_ACCOUNTS,
    POLICY_IGNORE,
    SIA_DC09_HUB_UPDATED,
)
from .discovery import ATTRIBUTE_ACCOUNTS, ATTRIBUTE_MESSAGES
from .entity import SiaDc09Entity
from .hub import AccountConfig, SiaDc09Hub
from .models import SiaDc09Event
from .state_machine import SiaStatus


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create the sensors for every configured account, plus hub diagnostics."""
    hub: SiaDc09Hub = hass.data[DOMAIN][entry.entry_id]
    recent = hub.options.get(CONF_RECENT_EVENTS, DEFAULT_RECENT_EVENTS)
    _async_remove_heartbeat_sensors(hass, entry)

    entities: list[SensorEntity] = []
    for account in hub.accounts.values():
        entities.extend(
            [
                SiaDc09StatusSensor(hub, account),
                SiaDc09ActivitySensor(hub, account, recent),
            ]
        )

    entities.append(SiaDc09MessageCountSensor(hub))
    entities.append(SiaDc09UnknownAccountsSensor(hub))
    async_add_entities(entities)


@callback
def _async_remove_heartbeat_sensors(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the retired per-account heartbeat sensors.

    Their state changed on every message, so each heartbeat landed in the
    device's activity log and buried the events that matter. The time now lives
    in the ``last_heartbeat`` attribute of the connectivity sensor instead.
    """
    registry = er.async_get(hass)
    suffix = f"_{KEY_LAST_HEARTBEAT}"
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.domain == "sensor" and entity.unique_id.endswith(suffix):
            registry.async_remove(entity.entity_id)


class SiaDc09StatusSensor(SiaDc09Entity, SensorEntity):
    """The account's alarm status, including ``panic``.

    Separate from the alarm panel because Home Assistant's panel states have no
    room for ``panic``, and because a plain sensor is easier to use in
    templates and dashboards.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options: ClassVar[list[str]] = [
        status.value for status in SiaStatus if status != SiaStatus.UNKNOWN
    ]

    def __init__(self, hub: SiaDc09Hub, account: AccountConfig):
        """Build the status sensor for one account."""
        super().__init__(hub, account, KEY_STATUS)

    @property
    def native_value(self) -> str | None:
        """Return the current status, or nothing if never heard from."""
        status = self.hub.status_of(self.account.account)
        return None if status is SiaStatus.UNKNOWN else status.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the restore target and what last changed the status."""
        state = self.hub.states.get(self.account.account)
        return {
            **super().extra_state_attributes,
            "previous_status": state.previous.value if state else None,
            "last_code": state.last_code if state else None,
            "changed_by": self.hub.changed_by(self.account.account),
            "changed_by_zoneorpoint": self.hub.changed_by_zone(self.account.account),
        }

    def handle_event(self, event: SiaDc09Event) -> bool:
        """Only a status change is worth writing."""
        return event.status_changed


class SiaDc09ActivitySensor(SiaDc09Entity, SensorEntity):
    """The account's most recent real activity, with a rolling log.

    Automatic tests are excluded, because a log dominated by four-hourly test
    reports is a log nobody reads. The full history, tests included, is
    available through the ``sia_dc09.get_activity`` service.
    """

    def __init__(self, hub: SiaDc09Hub, account: AccountConfig, recent: int):
        """Build the activity sensor and its in-memory ring buffer."""
        super().__init__(hub, account, KEY_LAST_ACTIVITY)
        self._recent: deque[dict[str, Any]] = deque(maxlen=max(1, recent))
        self._value: str | None = None
        self._changed_at: datetime | None = None

    @property
    def native_value(self) -> str | None:
        """Return a short description of the latest real event."""
        return self._value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the recent activity log and when it last changed."""
        return {
            **super().extra_state_attributes,
            "changed_at": self._changed_at,
            "events": list(reversed(self._recent)),
        }

    def handle_event(self, event: SiaDc09Event) -> bool:
        """Record anything that is not routine supervision."""
        if event.is_test:
            return False

        self._value = (
            event.summary or event.code_title or event.code or "Unknown event"
        )[:255]
        self._changed_at = event.received_at
        self._recent.append(
            {
                "received_at": event.received_at.isoformat(),
                "code": event.code,
                "title": event.code_title,
                "summary": event.summary,
                "severity": event.severity,
                "category": event.category,
                "zone": event.zone,
                "area": event.area,
                "user": event.user,
                "user_number": event.user_number,
                "user_name": event.user_name,
                "zone_number": event.zone_number,
                "zone_name": event.zone_name,
                "status_after": event.status_after,
            }
        )
        return True

    def async_restore(self, state) -> None:
        """Restore the last description, but not the whole log.

        The log lives in the activity database; rebuilding it here on every
        restart would duplicate that for no benefit.
        """
        if state.state not in (None, "unknown", "unavailable"):
            self._value = state.state
        changed = state.attributes.get("changed_at")
        if isinstance(changed, str):
            self._changed_at = dt_util.parse_datetime(changed)


class SiaDc09HubSensor(SensorEntity):
    """A sensor describing the receiver rather than any one account."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: SiaDc09Hub, key: str):
        """Build a hub level sensor."""
        self.hub = hub
        self._attr_unique_id = f"{hub.entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, hub.entry.entry_id)},
            name=hub.entry.title,
            manufacturer="SIA DC-09",
            model="Receiver",
            entry_type="service",
        )

    async def async_added_to_hass(self) -> None:
        """Refresh whenever the hub reports activity."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIA_DC09_HUB_UPDATED.format(self.hub.entry.entry_id),
                self.async_write_ha_state,
            )
        )


class SiaDc09MessageCountSensor(SiaDc09HubSensor):
    """How many messages the receiver has handled since it started."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "messages"

    def __init__(self, hub: SiaDc09Hub):
        """Build the message counter."""
        super().__init__(hub, KEY_MESSAGES)

    @property
    def native_value(self) -> int:
        """Return the number of messages received."""
        return self.hub.message_count

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the ports actually in use."""
        return {
            "udp_port": self.hub.udp_port,
            "tcp_port": self.hub.tcp_port,
            "accounts": len(self.hub.accounts),
        }


class SiaDc09UnknownAccountsSensor(SiaDc09HubSensor):
    """Accounts seen on the wire that are not configured.

    This is how the ``discover`` policy surfaces itself: the user can see what
    is transmitting and decide whether to adopt it.
    """

    def __init__(self, hub: SiaDc09Hub):
        """Build the discovery sensor."""
        super().__init__(hub, KEY_UNKNOWN_ACCOUNTS)

    @property
    def native_value(self) -> int:
        """Return how many unconfigured accounts have been heard."""
        return len(self.hub.discovered_accounts)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """List the unconfigured accounts, where they came from and what they said.

        Only the most recent few messages of the busiest few accounts are
        included: state attributes are written to the recorder on every
        change, so the payload has to stay small. Diagnostics carry the lot.
        """
        return {
            "accounts": self.hub.discovered_accounts,
            "message_counts": self.hub.unknown_accounts.message_counts(),
            "details": self.hub.unknown_accounts.as_list(
                accounts=ATTRIBUTE_ACCOUNTS,
                messages=ATTRIBUTE_MESSAGES,
                include_raw=False,
            )
            if self.hub.unknown_account_policy != POLICY_IGNORE
            else [],
            "dropped_messages": self.hub.unknown_accounts.dropped,
            "policy": self.hub.unknown_account_policy,
        }
