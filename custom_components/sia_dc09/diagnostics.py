"""Diagnostics for a SIA DC-09 config entry."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ACCOUNTS, CONF_ENCRYPTION_KEY, CONF_USERS, DOMAIN
from .hub import SiaDc09Hub

TO_REDACT = {CONF_ENCRYPTION_KEY, "remote_ip", "remote_ips"}


def _redact_entry(data: dict[str, Any]) -> dict[str, Any]:
    """Return the entry configuration without its secrets or people's names.

    User names are the one piece of configuration that identifies real people,
    and diagnostics get pasted into public issues. The numbers are kept because
    "is this number even configured" is the question diagnostics are for.
    """
    redacted = async_redact_data(data, {CONF_ENCRYPTION_KEY})
    accounts = redacted.get(CONF_ACCOUNTS)
    if isinstance(accounts, list):
        redacted[CONF_ACCOUNTS] = [
            {**account, CONF_USERS: sorted(account[CONF_USERS])}
            if isinstance(account, dict) and isinstance(account.get(CONF_USERS), dict)
            else account
            for account in accounts
        ]
    return redacted


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return everything needed to debug a receiver, minus the secrets."""
    hub: SiaDc09Hub = hass.data[DOMAIN][entry.entry_id]

    recent = await hub.store.async_get_activity(limit=25, include_tests=True)

    return {
        "entry": _redact_entry({**entry.data, **entry.options}),
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
                "named_users": len(account.users),
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
        "unknown_accounts": async_redact_data(
            hub.unknown_accounts.as_list(), TO_REDACT
        ),
        "stored_events": await hub.store.async_count(),
        "recent_activity": async_redact_data(recent, TO_REDACT),
    }
