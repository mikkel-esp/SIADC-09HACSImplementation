"""Logbook entries for SIA DC-09 events.

Turns the raw bus events into readable lines like "Front door reported Burglary
Alarm - Zone or point 3", so the logbook is useful without the user having to
know SIA codes.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, SIA_DC09_EVENT_ALL


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Register the describer for this integration's bus events."""

    @callback
    def async_describe_sia_event(event: Event) -> dict[str, str]:
        """Describe one received message."""
        data = event.data
        account = data.get("account", "?")
        summary = data.get("summary") or data.get("code_title") or data.get("code")

        message = f"reported {summary}" if summary else "sent a message"
        if data.get("status_changed") and (status := data.get("status_after")):
            message = f"{message}, now {status.replace('_', ' ')}"

        described = {
            LOGBOOK_ENTRY_NAME: _name_for(hass, account),
            LOGBOOK_ENTRY_MESSAGE: message,
        }
        if entity_id := _entity_for(hass, account):
            described[LOGBOOK_ENTRY_ENTITY_ID] = entity_id
        return described

    async_describe_event(DOMAIN, SIA_DC09_EVENT_ALL, async_describe_sia_event)


def _name_for(hass: HomeAssistant, account: str) -> str:
    """Return the friendly name of an account, falling back to its number."""
    if (entity_id := _entity_for(hass, account)) and (
        state := hass.states.get(entity_id)
    ):
        return state.name
    return f"Account {account}"


def _entity_for(hass: HomeAssistant, account: str) -> str | None:
    """Return the status sensor of an account, if it exists."""
    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if (
            entry.platform == DOMAIN
            and entry.domain == "sensor"
            and entry.unique_id.endswith(f"_{account}_status")
        ):
            return entry.entity_id
    return None
