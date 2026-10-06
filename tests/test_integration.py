"""End to end tests: a real datagram in, entity states and bus events out."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from homeassistant.components.alarm_control_panel import AlarmControlPanelEntityFeature
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sia_dc09.const import (
    CONF_ACCOUNT,
    CONF_ACCOUNTS,
    CONF_ARM_AWAY_TARGET,
    CONF_BIND_HOST,
    CONF_ENCRYPTION_KEY,
    CONF_IGNORE_TIMESTAMPS,
    CONF_NAK_ON_BAD_CRC,
    CONF_RESPOND,
    CONF_TCP_PORT,
    CONF_UDP_PORT,
    CONF_UNKNOWN_ACCOUNT_POLICY,
    CONF_USERS,
    DOMAIN,
    POLICY_IGNORE,
    SIA_DC09_EVENT_ALL,
)
from custom_components.sia_dc09.dc09 import build_frame, encrypt_body, parse_key
from custom_components.sia_dc09.utils import unique_id

ACCOUNT = "1234"
KEY_HEX = "ABCDABCDABCDABCDABCDABCDABCDABCD"


@pytest.fixture(autouse=True)
def _auto_enable(enable_custom_integrations):
    """Let Home Assistant load the integration from custom_components."""
    return


def body(code: str, account: str = ACCOUNT, sequence: str = "0001") -> str:
    """Return a DC-09 body for one SIA code."""
    return f'"SIA-DCS"{sequence}R0L0#{account}[#{account}|Nri1/{code}]'


def stamp() -> str:
    """Return a DC-09 timestamp for right now.

    Encrypted accounts enforce timestamps by default, so tests that send
    encrypted traffic must look current rather than use a fixed date.
    """
    return datetime.now(UTC).strftime("%H:%M:%S,%m-%d-%Y")


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
    return await send_raw(hass, entry, build_frame(message))


def hub_of(hass: HomeAssistant, entry: MockConfigEntry):
    """Return the hub backing a config entry."""
    return hass.data[DOMAIN][entry.entry_id]


async def send_raw(
    hass: HomeAssistant, entry: MockConfigEntry, datagram: bytes
) -> bytes:
    """Send raw bytes to the receiver, returning the reply or b'' if silent."""
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
        transport.sendto(datagram)
        try:
            reply = await asyncio.wait_for(replies.get(), timeout=5)
        except TimeoutError:
            reply = b""
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
    assert hass.states.get("sensor.front_door_last_heartbeat") is None
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

    connectivity = hass.states.get("binary_sensor.front_door_connectivity")
    assert connectivity.attributes["last_heartbeat"] is not None
    assert connectivity.attributes["timeout_minutes"] > 0
    stored = await hub.store.async_get_activity(limit=10, include_tests=False)
    assert stored == []
    # It is still recorded when tests are included.
    assert await hub.store.async_get_activity(limit=10, include_tests=True) != []


async def test_retired_heartbeat_sensor_is_removed(hass: HomeAssistant) -> None:
    """An upgrade drops the old heartbeat sensor from the entity registry."""
    entry = await make_entry(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id(entry.entry_id, ACCOUNT, "last_heartbeat"),
        config_entry=entry,
    )

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(old.entity_id) is None
    assert hass.states.get("sensor.front_door_status") is not None


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
    token = encrypt_body(f"#{ACCOUNT}|Nri1/CL001]_{stamp()}", key)
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


async def test_unknown_account_shows_messages_and_source(hass: HomeAssistant) -> None:
    """Counting unknown accounts is useless without the message and the sender."""
    entry = await make_entry(hass)

    await send_udp(hass, entry, body("BA001", account="9999"))
    await send_udp(hass, entry, body("CL501", account="9999", sequence="0002"))

    state = hass.states.get("sensor.mock_title_unknown_accounts")
    assert state.state == "1"

    detail = state.attributes["details"][0]
    assert detail["account"] == "9999"
    assert detail["message_count"] == 2
    assert list(detail["remote_ips"]) == ["127.0.0.1"]

    # Newest first, so the most recent message is the one you read.
    summaries = [message["summary"] for message in detail["recent_messages"]]
    assert summaries[0].startswith("Closing Report")
    assert summaries[1].startswith("Burglary Alarm")
    assert detail["recent_messages"][0]["remote_ip"] == "127.0.0.1"
    assert detail["recent_messages"][0]["transport"] == "udp"


async def test_unknown_account_activity_is_queryable(hass: HomeAssistant) -> None:
    """Stored messages from an unconfigured account can still be read back."""
    entry = await make_entry(hass)

    await send_udp(hass, entry, body("BA001", account="9999"))

    events = await hass.services.async_call(
        DOMAIN,
        "get_activity",
        {"account": "9999"},
        blocking=True,
        return_response=True,
    )
    assert events["count"] == 1
    assert events["events"][0]["account"] == "9999"


async def test_unknown_accounts_are_capped(hass: HomeAssistant) -> None:
    """An attacker inventing account numbers cannot grow memory without limit."""
    entry = await make_entry(hass)
    hub = hass.data[DOMAIN][entry.entry_id]
    hub.unknown_accounts._max_accounts = 2

    for index in range(4):
        await send_udp(hass, entry, body("BA001", account=f"900{index}"))

    assert len(hub.unknown_accounts) == 2
    assert hub.unknown_accounts.dropped == 2


async def test_unknown_account_ignored(hass: HomeAssistant) -> None:
    """Under the ignore policy nothing is recorded and a DUH is sent."""
    entry = await make_entry(hass, **{CONF_UNKNOWN_ACCOUNT_POLICY: POLICY_IGNORE})

    reply = await send_udp(hass, entry, body("BA001", account="9999"))
    assert b"DUH" in reply

    hub = hass.data[DOMAIN][entry.entry_id]
    assert hub.discovered_accounts == []
    assert len(hub.unknown_accounts) == 0
    assert (
        hass.states.get("sensor.mock_title_unknown_accounts").attributes["details"]
        == []
    )


async def test_user_number_is_named(hass: HomeAssistant) -> None:
    """A configured user number reads as a name everywhere it is shown."""
    entry = await make_entry(
        hass,
        accounts=[
            {
                CONF_ACCOUNT: ACCOUNT,
                "name": "Front door",
                CONF_USERS: {"501": "Mikkel"},
            }
        ],
    )

    await send_udp(hass, entry, body("CL501"))

    activity = hass.states.get("sensor.front_door_last_activity")
    assert activity.state == "Closing Report - User Mikkel (area 1)"
    latest = activity.attributes["events"][0]
    assert latest["user_number"] == "501"
    assert latest["user_name"] == "Mikkel"


async def test_unnamed_user_number_is_left_alone(hass: HomeAssistant) -> None:
    """A number with no name keeps its number rather than reading oddly."""
    entry = await make_entry(
        hass,
        accounts=[
            {
                CONF_ACCOUNT: ACCOUNT,
                "name": "Front door",
                CONF_USERS: {"501": "Mikkel"},
            }
        ],
    )

    await send_udp(hass, entry, body("CL777"))

    activity = hass.states.get("sensor.front_door_last_activity")
    assert activity.state == "Closing Report - User number 777 (area 1)"
    assert activity.attributes["events"][0]["user_name"] is None


async def test_zone_number_is_never_named(hass: HomeAssistant) -> None:
    """A zone that happens to match a user number must not be renamed."""
    entry = await make_entry(
        hass,
        accounts=[
            {
                CONF_ACCOUNT: ACCOUNT,
                "name": "Front door",
                CONF_USERS: {"501": "Mikkel"},
            }
        ],
    )

    await send_udp(hass, entry, body("BA501"))

    activity = hass.states.get("sensor.front_door_last_activity")
    assert "Mikkel" not in activity.state
    assert activity.state == "Burglary Alarm - Zone or point 501 (area 1)"


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


async def test_account_edit_reloads_with_connected_panel(
    hass: HomeAssistant,
) -> None:
    """An account edit must finish reloading even with a persistent TCP panel."""
    entry = await make_entry(hass)
    old_hub = hub_of(hass, entry)
    reader, writer = await asyncio.open_connection("127.0.0.1", old_hub.tcp_port)
    try:
        writer.write(build_frame(body("CL501")))
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\r"), timeout=5)
        await hass.async_block_till_done()

        hass.config_entries.async_update_entry(
            entry,
            options={
                CONF_UDP_PORT: old_hub.udp_port,
                CONF_TCP_PORT: old_hub.tcp_port,
                CONF_ACCOUNTS: [
                    {
                        CONF_ACCOUNT: ACCOUNT,
                        "name": "Front door",
                        "zones": {"10": "Keypad"},
                    },
                    {CONF_ACCOUNT: "5678", "name": "Garage"},
                ],
            },
        )
        await asyncio.wait_for(hass.async_block_till_done(), timeout=5)
        assert await asyncio.wait_for(reader.read(), timeout=2) == b""
        new_hub = hub_of(hass, entry)
        assert new_hub is not old_hub
        assert new_hub.accounts[ACCOUNT].zones == {"10": "Keypad"}
        assert hass.states.get("sensor.garage_status") is not None
        reply = await send_tcp(hass, entry, body("OP501", sequence="0002"))
        assert b'"ACK"' in reply
        assert hass.states.get("alarm_control_panel.front_door").state == "disarmed"
    finally:
        writer.close()
        await writer.wait_closed()


async def test_no_arming_targets_means_no_features(hass: HomeAssistant) -> None:
    """Without a target the panel advertises nothing it cannot do."""
    await make_entry(hass)

    panel = hass.states.get("alarm_control_panel.front_door")
    assert panel.attributes["supported_features"] == 0


async def test_arming_runs_the_configured_target(hass: HomeAssistant) -> None:
    """Arming delegates to the nominated entity instead of failing."""
    assert await async_setup_component(
        hass,
        "input_button",
        {"input_button": {"panel_arm": {"name": "Panel arm"}}},
    )
    await hass.async_block_till_done()

    await make_entry(
        hass,
        accounts=[
            {
                CONF_ACCOUNT: ACCOUNT,
                "name": "Front door",
                CONF_ARM_AWAY_TARGET: "input_button.panel_arm",
            }
        ],
    )

    panel = hass.states.get("alarm_control_panel.front_door")
    assert panel.attributes["supported_features"] == (
        AlarmControlPanelEntityFeature.ARM_AWAY
    )

    before = hass.states.get("input_button.panel_arm").state
    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_arm_away",
        {"entity_id": "alarm_control_panel.front_door"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get("input_button.panel_arm").state != before
    # The status still only follows what the panel reports.
    assert hass.states.get("sensor.front_door_status").state == STATE_UNKNOWN


async def test_plaintext_cannot_disarm_an_encrypted_account(
    hass: HomeAssistant,
) -> None:
    """An attacker without the key must not be able to disarm.

    The account is configured WITH an encryption key, so unencrypted
    traffic for it must be rejected outright rather than applied.
    """
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
    assert key is not None

    def encrypted(code: str, sequence: str = "0001") -> str:
        tail = encrypt_body(f"|Nri1/{code}]_{stamp()}", key)
        return f'"*SIA-DCS"{sequence}R0L0#{ACCOUNT}[{tail}'

    # Arm the account using a properly encrypted message.
    await send_udp(hass, entry, encrypted("CL001"))
    assert hass.states.get("sensor.front_door_status").state == "armed_away"

    # Now try to disarm it with plaintext, i.e. without knowing the key.
    reply = await send_udp(hass, entry, body("OP001", sequence="0002"))

    assert hass.states.get("sensor.front_door_status").state == "armed_away"
    assert b"NAK" in reply


async def test_replay_is_blocked_while_the_timestamp_still_looks_fresh(
    hass: HomeAssistant, freezer
) -> None:
    """Deduplication must outlive the window in which a replay is accepted.

    A captured message stays inside the permitted timeband for a while, so if
    the digest cache expired first an attacker could simply wait and then
    replay it.
    """
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
    assert key is not None

    def encrypted(code: str, sequence: str) -> str:
        tail = encrypt_body(f"|Nri1/{code}]_{stamp()}", key)
        return f'"*SIA-DCS"{sequence}R0L0#{ACCOUNT}[{tail}'

    disarm = encrypted("OP001", "0001")
    await send_udp(hass, entry, disarm)
    await send_udp(hass, entry, encrypted("BA001", "0002"))
    assert hass.states.get("sensor.front_door_status").state == "triggered"

    # Long enough that a short deduplication window would have forgotten it,
    # but still inside the timeband, so the timestamp check alone lets it by.
    freezer.tick(60)
    await send_udp(hass, entry, disarm)

    assert hass.states.get("sensor.front_door_status").state == "triggered"


async def test_replayed_message_does_not_change_state(hass: HomeAssistant) -> None:
    """A captured message must not be usable twice.

    Without replay protection an attacker who records an encrypted disarm
    can suppress a later real alarm just by resending the same bytes.
    """
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
    assert key is not None

    def encrypted(code: str, sequence: str) -> str:
        tail = encrypt_body(f"|Nri1/{code}]_{stamp()}", key)
        return f'"*SIA-DCS"{sequence}R0L0#{ACCOUNT}[{tail}'

    disarm = encrypted("OP001", "0001")
    await send_udp(hass, entry, disarm)
    assert hass.states.get("sensor.front_door_status").state == "disarmed"

    # A genuine alarm arrives.
    await send_udp(hass, entry, encrypted("BA001", "0002"))
    assert hass.states.get("sensor.front_door_status").state == "triggered"

    # The attacker replays the captured disarm to hide it.
    await send_udp(hass, entry, disarm)

    assert hass.states.get("sensor.front_door_status").state == "triggered"


async def test_missing_timestamp_is_rejected_when_enforced(
    hass: HomeAssistant,
) -> None:
    """Omitting the timestamp must not bypass timestamp enforcement."""
    entry = await make_entry(
        hass,
        accounts=[
            {
                CONF_ACCOUNT: ACCOUNT,
                "name": "Front door",
                CONF_IGNORE_TIMESTAMPS: False,
            }
        ],
    )

    # No trailing _<timestamp>, so there is nothing to check against.
    await send_udp(hass, entry, body("BA001"))

    assert hass.states.get("sensor.front_door_status").state == STATE_UNKNOWN


async def test_fresh_timestamp_is_accepted_when_enforced(
    hass: HomeAssistant,
) -> None:
    """Enforcement must still let a correctly stamped message through."""
    entry = await make_entry(
        hass,
        accounts=[
            {
                CONF_ACCOUNT: ACCOUNT,
                "name": "Front door",
                CONF_IGNORE_TIMESTAMPS: False,
            }
        ],
    )
    stamp = datetime.now(UTC).strftime("%H:%M:%S,%m-%d-%Y")
    message = f'"SIA-DCS"0001R0L0#{ACCOUNT}[#{ACCOUNT}|Nri1/BA001]_{stamp}'

    await send_udp(hass, entry, message)

    assert hass.states.get("sensor.front_door_status").state == "triggered"


async def test_rejected_message_cannot_drive_entities_or_bus(
    hass: HomeAssistant,
) -> None:
    """A message that failed validation must not reach entities.

    The smoke sensor trusts the SIA code, so a forged cleartext ``FA`` for an
    encrypted account would otherwise be able to report a fire.
    """
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

    fired: list[Any] = []
    hass.bus.async_listen("sia_dc09_event", fired.append)

    await send_udp(hass, entry, body("FA001"))

    assert hass.states.get("binary_sensor.front_door_smoke").state == STATE_OFF
    assert fired == []

    # It is still auditable, which is the point of recording it.
    assert await hub_of(hass, entry).store.async_count() == 1


async def test_corrupt_frame_cannot_drive_entities(hass: HomeAssistant) -> None:
    """The same protection applies to a frame with a broken checksum."""
    entry = await make_entry(hass)

    fired: list[Any] = []
    hass.bus.async_listen("sia_dc09_event", fired.append)

    # Build a valid frame, then corrupt the CRC in place.
    frame = build_frame(body("FA001"))
    corrupt = frame[:1] + b"FFFF" + frame[5:]
    await send_raw(hass, entry, corrupt)

    assert hass.states.get("binary_sensor.front_door_smoke").state == STATE_OFF
    assert fired == []


async def test_encrypted_account_enforces_timestamps_by_default(
    hass: HomeAssistant,
) -> None:
    """A key implies freshness checking unless the user opts out.

    Encryption alone does not prove a message is recent, so a captured
    message stays replayable until timestamps are enforced.
    """
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
    assert key is not None

    stale = encrypt_body("|Nri1/OP001]_10:00:00,01-01-2024", key)
    await send_udp(hass, entry, f'"*SIA-DCS"0001R0L0#{ACCOUNT}[{stale}')

    assert hass.states.get("sensor.front_door_status").state == STATE_UNKNOWN


async def test_unencrypted_account_still_ignores_timestamps(
    hass: HomeAssistant,
) -> None:
    """Without a key the check buys nothing, so panels keep working."""
    entry = await make_entry(hass)

    await send_udp(
        hass,
        entry,
        f'"SIA-DCS"0001R0L0#{ACCOUNT}[#{ACCOUNT}|Nri1/BA001]_10:00:00,01-01-2024',
    )

    assert hass.states.get("sensor.front_door_status").state == "triggered"
