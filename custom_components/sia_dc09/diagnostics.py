"""Diagnostics for a SIA DC-09 config entry."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ENCRYPTION_KEY, DOMAIN
from .hub import SiaDc09Hub

TO_REDACT = {CONF_ENCRYPTION_KEY, "remote_ip"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return everything needed to debug a receiver, minus the secrets."""
    hub: SiaDc09Hub = hass.data[DOMAIN][entry.entry_id]

    recent = await hub.store.async_get_activity(limit=25, include_tests=True)

    return {
        "entry": async_redact_data(
            {**entry.data, **entry.options}, {CONF_ENCRYPTION_KEY}
        ),
        "receiver": {
            "udp_port": hub.udp_port,
            "tcp_port": hub.tcp_port,
            "message_count": hub.message_count,
            "unknown_account_policy": hub.unknown_account_policy,
            "retention_months": hub.retention_months,
        },
        "accounts": [
            {
                "account": account.account,
                "name": account.name,
                # Whether a key exists matters; its value must never leave.
                "encrypted": account.key is not None,
                "heartbeat_timeout": account.heartbeat_timeout,
                "ignore_timestamps": account.ignore_timestamps,
                "status": hub.status_of(account.account).value,
                "online": hub.is_online(account.account),
                "last_message_at": (
                    state.last_message_at.isoformat()
                    if (state := hub.states.get(account.account))
                    and state.last_message_at
                    else None
                ),
                "last_code": state.last_code if state else None,
            }
            for account in hub.accounts.values()
        ],
        "unknown_accounts": dict(hub.unknown_accounts),
        "stored_events": await hub.store.async_count(),
        "recent_activity": async_redact_data(recent, TO_REDACT),
    }
