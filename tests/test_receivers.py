"""Adding a second receiver, and reloading one by hand.

Both come from the same complaint: a receiver that was added but never
appeared. The cause is always a port that could not be bound, so the flow now
refuses up front and a reload service exists to retry once the port is free.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sia_dc09.const import (
    CONF_ACCOUNT,
    CONF_ACCOUNTS,
    CONF_BIND_HOST,
    CONF_NAK_ON_BAD_CRC,
    CONF_RESPOND,
    CONF_TCP_PORT,
    CONF_UDP_PORT,
    DEFAULT_BIND_HOST,
    DOMAIN,
    SERVICE_RELOAD,
)

from .test_integration import ACCOUNT, _auto_enable, hub_of, make_entry  # noqa: F401


def free_port() -> int:
    """Return a port number nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def receiver_input(**overrides: Any) -> dict[str, Any]:
    """Return a complete receiver form submission."""
    return {
        CONF_BIND_HOST: "127.0.0.1",
        CONF_UDP_PORT: free_port(),
        CONF_TCP_PORT: free_port(),
        CONF_RESPOND: True,
        CONF_NAK_ON_BAD_CRC: True,
        "unknown_account_policy": "discover",
        "retention_months": 24,
        "recent_events_in_attributes": 20,
        **overrides,
    }


async def test_second_receiver_is_set_up(hass: HomeAssistant) -> None:
    """Two receivers on different ports both produce entities."""
    first = await make_entry(hass)
    second = await make_entry(
        hass, accounts=[{CONF_ACCOUNT: "9876", "name": "Back door"}]
    )

    assert first.state is ConfigEntryState.LOADED
    assert second.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    assert er.async_entries_for_config_entry(registry, second.entry_id)
    assert hass.states.get("alarm_control_panel.back_door") is not None


async def test_second_receiver_reusing_a_port_is_refused(hass: HomeAssistant) -> None:
    """Sharing a port would bind-fail after the entry was created.

    Home Assistant would then retry the entry forever and the user would see a
    receiver that never grows any entities, which is what makes this worth
    catching in the form.
    """
    taken = free_port()
    existing = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BIND_HOST: DEFAULT_BIND_HOST,
            CONF_UDP_PORT: taken,
            CONF_TCP_PORT: free_port(),
            CONF_ACCOUNTS: [{CONF_ACCOUNT: ACCOUNT, "name": "Front door"}],
        },
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], receiver_input(**{CONF_UDP_PORT: taken})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_UDP_PORT: "port_in_use"}


async def test_different_interfaces_may_share_a_port(hass: HomeAssistant) -> None:
    """Two explicit addresses do not compete, so this must still be allowed."""
    shared = free_port()
    existing = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BIND_HOST: "127.0.0.2",
            CONF_UDP_PORT: shared,
            CONF_TCP_PORT: shared,
            CONF_ACCOUNTS: [{CONF_ACCOUNT: ACCOUNT, "name": "Front door"}],
        },
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        receiver_input(**{CONF_UDP_PORT: shared, CONF_TCP_PORT: shared}),
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "account"


async def test_editing_a_receiver_may_keep_its_own_ports(hass: HomeAssistant) -> None:
    """The entry being edited must not be counted as a conflict with itself."""
    entry = await make_entry(hass)
    hub = hub_of(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "receiver"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        receiver_input(**{CONF_UDP_PORT: hub.udp_port, CONF_TCP_PORT: hub.tcp_port}),
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.state is ConfigEntryState.LOADED


async def test_reload_service_restarts_the_receiver(hass: HomeAssistant) -> None:
    """The reload the user asked for, without touching the configuration."""
    entry = await make_entry(hass)
    before = hub_of(hass, entry)

    await hass.services.async_call(DOMAIN, SERVICE_RELOAD, {}, blocking=True)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hub_of(hass, entry) is not before
    assert hass.states.get("alarm_control_panel.front_door") is not None


async def test_reload_service_can_target_one_account(hass: HomeAssistant) -> None:
    """Reloading everything would drop messages for unrelated receivers."""
    first = await make_entry(hass)
    second = await make_entry(
        hass, accounts=[{CONF_ACCOUNT: "9876", "name": "Back door"}]
    )
    untouched = hub_of(hass, second)

    await hass.services.async_call(
        DOMAIN, SERVICE_RELOAD, {"account": ACCOUNT}, blocking=True
    )
    await hass.async_block_till_done()

    assert hub_of(hass, first).entry.entry_id == first.entry_id
    assert hub_of(hass, second) is untouched


async def test_reload_service_rejects_an_unknown_account(hass: HomeAssistant) -> None:
    """Silently reloading nothing would look like the service was broken."""
    await make_entry(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_RELOAD, {"account": "4321"}, blocking=True
        )


async def test_reload_service_recovers_a_failed_receiver(hass: HomeAssistant) -> None:
    """The case that matters: a receiver whose port was taken at startup.

    Its entry never finished setting up, so none of the other services exist.
    Reload has to be registered independently of that to be any use here.
    """
    port = free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", port))

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BIND_HOST: "127.0.0.1",
            CONF_UDP_PORT: port,
            CONF_TCP_PORT: 0,
            CONF_ACCOUNTS: [{CONF_ACCOUNT: ACCOUNT, "name": "Front door"}],
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY

    blocker.close()
    await hass.services.async_call(DOMAIN, SERVICE_RELOAD, {}, blocking=True)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("alarm_control_panel.front_door") is not None
