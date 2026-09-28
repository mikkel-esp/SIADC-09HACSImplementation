"""End to end tests: a real datagram in, entity states and bus events out."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sia_dc09.const import (
    CONF_ACCOUNT,
    CONF_ACCOUNTS,
    CONF_BIND_HOST,
    CONF_ENCRYPTION_KEY,
    CONF_NAK_ON_BAD_CRC,
    CONF_RESPOND,
    CONF_TCP_PORT,
    CONF_UDP_PORT,
    CONF_UNKNOWN_ACCOUNT_POLICY,
    DOMAIN,
    POLICY_IGNORE,
    SIA_DC09_EVENT_ALL,
)
from custom_components.sia_dc09.dc09 import build_frame

ACCOUNT = "1234"
KEY_HEX = "ABCDABCDABCDABCDABCDABCDABCDABCD"


@pytest.fixture(autouse=True)
def _auto_enable(enable_custom_integrations):
    """Let Home Assistant load the integration from custom_components."""
    return


def body(code: str, account: str = ACCOUNT, sequence: str = "0001") -> str:
    """Return a DC-09 body for one SIA code."""
    return f'"SIA-DCS"{sequence}R0L0#{account}[#{account}|Nri1/{code}]'


async def make_entry(
    hass: HomeAssistant, accounts: list[dict[str, Any]] | None = None, **overrides: Any
) -> MockConfigEntry:
    """Set up the integration on ephemeral ports and return its entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BIND_HOST: "127.0.0.1",
            # Port 0 asks the OS for a free port, so tests never collide.
            CONF_UDP_PORT: 0,
            CONF_TCP_PORT: 0,
            CONF_RESPOND: True,
            CONF_NAK_ON_BAD_CRC: True,
            CONF_ACCOUNTS: accounts
            if accounts is not None
            else [{CONF_ACCOUNT: ACCOUNT, "name": "Front door"}],
            **overrides,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def send_udp(hass: HomeAssistant, entry: MockConfigEntry, message: str) -> bytes:
    """Send one datagram to the receiver and return its reply."""
    hub = hass.data[DOMAIN][entry.entry_id]
    loop = asyncio.get_running_loop()
    replies: asyncio.Queue[bytes] = asyncio.Queue()

    class _Client(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr) -> None:
            replies.put_nowait(data)

    transport, _ = await loop.create_datagram_endpoint(
        _Client, remote_addr=("127.0.0.1", hub.udp_port)
    )
    try:
        transport.sendto(build_frame(message))
        reply = await asyncio.wait_for(replies.get(), timeout=5)
    finally:
        transport.close()

    await hass.async_block_till_done()
    return reply


async def send_tcp(hass: HomeAssistant, entry: MockConfigEntry, message: str) -> bytes:
    """Send one message over TCP and return the reply."""
    hub = hass.data[DOMAIN][entry.entry_id]
    reader, writer = await asyncio.open_connection("127.0.0.1", hub.tcp_port)
    try:
        writer.write(build_frame(message))
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(256), timeout=5)
    finally:
        writer.close()
        await writer.wait_closed()

    await hass.async_block_till_done()
    return reply


async def test_setup_creates_entities(hass: HomeAssistant) -> None:
    """Setting up the entry produces one device worth of entities."""
    await make_entry(hass)

    assert hass.states.get("alarm_control_panel.front_door") is not None
    assert hass.states.get("sensor.front_door_status") is not None
    assert hass.states.get("sensor.front_door_last_heartbeat") is not None
    assert hass.states.get("sensor.front_door_last_activity") is not None
    assert hass.states.get("binary_sensor.front_door_connectivity") is not None
    assert hass.states.get("binary_sensor.front_door_smoke") is not None


async def test_udp_message_arms_and_acknowledges(hass: HomeAssistant) -> None:
    """A closing report arms the panel and is acknowledged."""
    entry = await make_entry(hass)

    reply = await send_udp(hass, entry, body("CL001"))
    assert b"ACK" in reply

    assert hass.states.get("sensor.front_door_status").state == "armed_away"
    assert hass.states.get("alarm_control_panel.front_door").state == "armed_away"


async def test_tcp_message_is_handled(hass: HomeAssistant) -> None:
    """The TCP transport feeds the same pipeline as UDP."""
    entry = await make_entry(hass)

    reply = await send_tcp(hass, entry, body("OP001"))
    assert b"ACK" in reply
    assert hass.states.get("sensor.front_door_status").state == "disarmed"


async def test_burglary_triggers_and_sticks(hass: HomeAssistant) -> None:
    """An alarm latches until it is explicitly cleared."""
    entry = await make_entry(hass)

    await send_udp(hass, entry, body("CL001"))
    await send_udp(hass, entry, body("BA001", sequence="0002"))
    assert hass.states.get("sensor.front_door_status").state == "triggered"

    # An unrelated informational code must not clear the alarm.
    await send_udp(hass, entry, body("RP000", sequence="0003"))
    assert hass.states.get("sensor.front_door_status").state == "triggered"

    await send_udp(hass, entry, body("OP001", sequence="0004"))
    assert hass.states.get("sensor.front_door_status").state == "disarmed"


async def test_panic_outranks_triggered(hass: HomeAssistant) -> None:
    """A holdup during a burglary escalates to panic."""
    entry = await make_entry(hass)

    await send_udp(hass, entry, body("BA001"))
    await send_udp(hass, entry, body("HA001", sequence="0002"))
    assert hass.states.get("sensor.front_door_status").state == "panic"
    # The alarm panel has no panic state of its own, so it reports triggered.
    assert hass.states.get("alarm_control_panel.front_door").state == "triggered"


async def test_binary_sensors_follow_their_codes(hass: HomeAssistant) -> None:
    """Smoke and power sensors react to their own codes only."""
    entry = await make_entry(hass)

    assert hass.states.get("binary_sensor.front_door_smoke").state == STATE_OFF
    await send_udp(hass, entry, body("FA001"))
    assert hass.states.get("binary_sensor.front_door_smoke").state == STATE_ON

    await send_udp(hass, entry, body("FH001", sequence="0002"))
    assert hass.states.get("binary_sensor.front_door_smoke").state == STATE_OFF

    # Power starts unknown because a panel only reports a change. AC trouble
    # means mains was lost, which for the power device class is "off".
    assert hass.states.get("binary_sensor.front_door_power").state == STATE_UNKNOWN
    await send_udp(hass, entry, body("AT001", sequence="0003"))
    assert hass.states.get("binary_sensor.front_door_power").state == STATE_OFF

    await send_udp(hass, entry, body("AR001", sequence="0004"))
    assert hass.states.get("binary_sensor.front_door_power").state == STATE_ON


async def test_heartbeat_updates_but_is_not_activity(hass: HomeAssistant) -> None:
    """An automatic test moves the heartbeat without logging activity."""
    entry = await make_entry(hass)
    hub = hass.data[DOMAIN][entry.entry_id]

    await send_udp(hass, entry, body("RP000"))

    assert hass.states.get("sensor.front_door_last_heartbeat").state != STATE_UNKNOWN
    stored = await hub.store.async_get_activity(limit=10, include_tests=False)
    assert stored == []
    # It is still recorded when tests are included.
    assert await hub.store.async_get_activity(limit=10, include_tests=True) != []


async def test_activity_is_logged(hass: HomeAssistant) -> None:
    """A real event lands in the activity log."""
    entry = await make_entry(hass)
    hub = hass.data[DOMAIN][entry.entry_id]

    await send_udp(hass, entry, body("BA001"))

    stored = await hub.store.async_get_activity(limit=10, include_tests=False)
    assert len(stored) == 1
    assert stored[0]["code"] == "BA"
    assert stored[0]["account"] == ACCOUNT


async def test_bus_event_is_fired(hass: HomeAssistant) -> None:
    """Automations can subscribe to the catch-all event."""
    entry = await make_entry(hass)
    events = []
    hass.bus.async_listen(SIA_DC09_EVENT_ALL, events.append)

    await send_udp(hass, entry, body("BA001"))

    assert len(events) == 1
    payload = events[0].data
    assert payload["account"] == ACCOUNT
    assert payload["code"] == "BA"
    assert payload["status_after"] == "triggered"
    assert payload["transport"] == "udp"


async def test_encrypted_message_round_trip(hass: HomeAssistant) -> None:
    """An account with a key decodes encrypted traffic."""
    from custom_components.sia_dc09.dc09 import encrypt_body, parse_key

    entry = await make_entry(
        hass,
        accounts=[
            {
                CONF_ACCOUNT: ACCOUNT,
                "name": "Front door",
                CONF_ENCRYPTION_KEY: KEY_HEX,
            }
        ],
    )

    key = parse_key(KEY_HEX)
    token = encrypt_body(f"#{ACCOUNT}|Nri1/CL001]", key)
    message = f'"*SIA-DCS"0001R0L0#{ACCOUNT}[{token}'

    reply = await send_udp(hass, entry, message)
    assert b"ACK" in reply
    assert hass.states.get("sensor.front_door_status").state == "armed_away"


async def test_unknown_account_is_discovered(hass: HomeAssistant) -> None:
    """An unconfigured account is surfaced rather than silently dropped."""
    entry = await make_entry(hass)

    await send_udp(hass, entry, body("BA001", account="9999"))

    hub = hass.data[DOMAIN][entry.entry_id]
    assert hub.discovered_accounts == ["9999"]
    assert hass.states.get("sensor.mock_title_unknown_accounts").state == "1"


async def test_unknown_account_ignored(hass: HomeAssistant) -> None:
    """Under the ignore policy nothing is recorded and a DUH is sent."""
    entry = await make_entry(hass, **{CONF_UNKNOWN_ACCOUNT_POLICY: POLICY_IGNORE})

    reply = await send_udp(hass, entry, body("BA001", account="9999"))
    assert b"DUH" in reply

    hub = hass.data[DOMAIN][entry.entry_id]
    assert hub.discovered_accounts == []


async def test_bad_crc_is_nakked(hass: HomeAssistant) -> None:
    """A corrupted message is rejected so the panel retransmits."""
    entry = await make_entry(hass)
    hub = hass.data[DOMAIN][entry.entry_id]

    frame = bytearray(build_frame(body("BA001")))
    frame[1] = ord("0") if frame[1] != ord("0") else ord("1")

    loop = asyncio.get_running_loop()
    replies: asyncio.Queue[bytes] = asyncio.Queue()

    class _Client(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr) -> None:
            replies.put_nowait(data)

    transport, _ = await loop.create_datagram_endpoint(
        _Client, remote_addr=("127.0.0.1", hub.udp_port)
    )
    try:
        transport.sendto(bytes(frame))
        reply = await asyncio.wait_for(replies.get(), timeout=5)
    finally:
        transport.close()
    await hass.async_block_till_done()

    assert b"NAK" in reply
    assert hass.states.get("sensor.front_door_status").state == STATE_UNKNOWN


async def test_unload_releases_the_ports(hass: HomeAssistant) -> None:
    """Unloading closes the sockets so the entry can be set up again."""
    entry = await make_entry(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert DOMAIN not in hass.data or entry.entry_id not in hass.data.get(DOMAIN, {})
    assert hass.states.get("sensor.front_door_status").state == "unavailable"


async def test_options_update_reloads(hass: HomeAssistant) -> None:
    """Adding an account through options creates its entities."""
    entry = await make_entry(hass)

    hass.config_entries.async_update_entry(
        entry,
        options={
            CONF_ACCOUNTS: [
                {CONF_ACCOUNT: ACCOUNT, "name": "Front door"},
                {CONF_ACCOUNT: "5678", "name": "Garage"},
            ]
        },
    )
    await hass.async_block_till_done()

    assert hass.states.get("sensor.garage_status") is not None
