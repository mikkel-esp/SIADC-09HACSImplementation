"""Alarm control panel for each configured SIA DC-09 account."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ACCOUNT,
    CONF_ACCOUNTS,
    CONF_ARM_AWAY_TARGET,
    CONF_ARM_HOME_TARGET,
    CONF_ARM_NIGHT_TARGET,
    CONF_DISARM_TARGET,
    DOMAIN,
    KEY_ALARM,
)
from .entity import SiaDc09Entity
from .hub import AccountConfig, SiaDc09Hub
from .models import SiaDc09Event
from .state_machine import SiaStatus, panel_state
from .utils import normalise_account

_LOGGER = logging.getLogger(__name__)

#: Which configured target drives which arming action.
_TARGETS = {
    "arm_away": CONF_ARM_AWAY_TARGET,
    "arm_home": CONF_ARM_HOME_TARGET,
    "arm_night": CONF_ARM_NIGHT_TARGET,
    "disarm": CONF_DISARM_TARGET,
}

_FEATURES = {
    "arm_away": AlarmControlPanelEntityFeature.ARM_AWAY,
    "arm_home": AlarmControlPanelEntityFeature.ARM_HOME,
    "arm_night": AlarmControlPanelEntityFeature.ARM_NIGHT,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create one alarm panel per configured account."""
    hub: SiaDc09Hub = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        SiaDc09AlarmControlPanel(hub, account, raw)
        for account, raw in _accounts_with_config(hub)
    )


def _accounts_with_config(hub: SiaDc09Hub):
    """Pair each parsed account with its raw configuration dictionary."""
    raw_by_account = {
        normalise_account(raw.get(CONF_ACCOUNT, "")): raw
        for raw in hub.options.get(CONF_ACCOUNTS, [])
    }
    for account in hub.accounts.values():
        yield account, raw_by_account.get(account.account, {})


class SiaDc09AlarmControlPanel(SiaDc09Entity, AlarmControlPanelEntity):
    """Reports an account's arm state, and optionally arms it.

    DC-09 is a one way reporting protocol: a receiver cannot command a panel.
    Arming therefore only works when the user has pointed the account at a
    script, scene or button that reaches the panel by some other route, such as
    a cloud integration or a relay module.
    """

    _attr_name = None
    _attr_code_arm_required = False

    def __init__(self, hub: SiaDc09Hub, account: AccountConfig, raw: dict[str, Any]):
        """Build the panel and wire up whichever arming targets exist."""
        super().__init__(hub, account, KEY_ALARM)
        self._targets: dict[str, str] = {
            action: raw[key] for action, key in _TARGETS.items() if raw.get(key)
        }

        features = AlarmControlPanelEntityFeature(0)
        for action, feature in _FEATURES.items():
            if action in self._targets:
                features |= feature
        self._attr_supported_features = features

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Return the panel state derived from the messages received.

        ``panic`` has no equivalent in Home Assistant, so it is reported as
        ``triggered``; the status sensor keeps the distinction.
        """
        value = panel_state(self.hub.status_of(self.account.account))
        return AlarmControlPanelState(value) if value else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the finer grained status alongside the panel state."""
        status = self.hub.status_of(self.account.account)
        state = self.hub.states.get(self.account.account)
        attributes = {
            **super().extra_state_attributes,
            "sia_status": status.value,
            "last_code": state.last_code if state else None,
        }
        if status is SiaStatus.PANIC:
            attributes["panic"] = True
        return attributes

    def handle_event(self, event: SiaDc09Event) -> bool:
        """Report a change only when the status actually moved."""
        return event.status_changed

    def async_restore(self, state) -> None:
        """Adopt the last known status after a restart."""
        if state.state in (None, "unknown", "unavailable"):
            return
        stored = self.hub.states.get(self.account.account)
        if stored is None or stored.status is not SiaStatus.UNKNOWN:
            return
        try:
            restored = SiaStatus(state.attributes.get("sia_status") or state.state)
        except ValueError:
            return
        self.hub.states[self.account.account] = replace(
            stored, status=restored, previous=restored
        )

    # --- arming --------------------------------------------------------------

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Run the configured arm-away target."""
        await self._async_run_target("arm_away")

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Run the configured arm-home target."""
        await self._async_run_target("arm_home")

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Run the configured arm-night target."""
        await self._async_run_target("arm_night")

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Run the configured disarm target."""
        await self._async_run_target("disarm")

    async def _async_run_target(self, action: str) -> None:
        """Invoke the entity the user nominated for an arming action.

        The status is deliberately not changed here. It only ever follows the
        messages the panel actually sends, so a target that silently fails
        cannot leave Home Assistant claiming the alarm is armed.
        """
        target = self._targets.get(action)
        if target is None:
            _LOGGER.warning(
                "No %s target configured for account %s",
                action,
                self.account.account,
            )
            return

        domain = target.split(".", 1)[0]
        service = {
            "script": "turn_on",
            "scene": "turn_on",
            "button": "press",
            "input_button": "press",
            "switch": "turn_on" if action != "disarm" else "turn_off",
        }.get(domain)

        if service is None:
            _LOGGER.error(
                "Cannot use %s as the %s target for account %s",
                target,
                action,
                self.account.account,
            )
            return

        await self.hass.services.async_call(
            domain,
            service,
            {"entity_id": target},
            blocking=True,
            context=self._context,
        )
