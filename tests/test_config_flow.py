"""Tests for the config and options flows."""

from __future__ import annotations

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sia_dc09.const import (
    CONF_ACCOUNT,
    CONF_ACCOUNTS,
    CONF_BIND_HOST,
    CONF_ENCRYPTION_KEY,
    CONF_TCP_PORT,
    CONF_UDP_PORT,
    DOMAIN,
)

RECEIVER = {
    CONF_BIND_HOST: "127.0.0.1",
    CONF_UDP_PORT: 10100,
    CONF_TCP_PORT: 10100,
    "respond": True,
    "nak_on_bad_crc": True,
    "unknown_account_policy": "discover",
    "retention_months": 24,
    "recent_events_in_attributes": 50,
}

ACCOUNT = {
    CONF_ACCOUNT: "1234",
    "name": "Front door",
    "heartbeat_timeout": 90,
}


@pytest.fixture(autouse=True)
def _auto_enable(enable_custom_integrations):
    """Let Home Assistant load the integration from custom_components."""
    return


async def _start(hass: HomeAssistant, receiver: dict | None = None):
    """Run the first step of the config flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {**RECEIVER, **(receiver or {})}
    )


async def test_full_flow_creates_entry(hass: HomeAssistant) -> None:
    """A receiver plus one account produces a config entry."""
    result = await _start(hass, {CONF_UDP_PORT: 10101, CONF_TCP_PORT: 10101})
    assert result["step_id"] == "account"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(ACCOUNT)
    )
    assert result["step_id"] == "add_another"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"add_another": False}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "SIA DC-09 (10101)"
    assert result["data"][CONF_ACCOUNTS] == [
        {
            CONF_ACCOUNT: "1234",
            "name": "Front door",
            "heartbeat_timeout": 90,
            "ignore_timestamps": True,
        }
    ]


async def test_flow_accepts_several_accounts(hass: HomeAssistant) -> None:
    """The add-another loop collects more than one account."""
    result = await _start(hass, {CONF_UDP_PORT: 10102, CONF_TCP_PORT: 10102})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(ACCOUNT)
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"add_another": True}
    )
    assert result["step_id"] == "account"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ACCOUNT, CONF_ACCOUNT: "ABCD", "name": "Garage"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"add_another": False}
    )
    await hass.async_block_till_done()

    accounts = result["data"][CONF_ACCOUNTS]
    assert [item[CONF_ACCOUNT] for item in accounts] == ["1234", "ABCD"]


async def test_flow_accepts_user_names(hass: HomeAssistant) -> None:
    """User numbers typed one per line become a number to name mapping."""
    result = await _start(hass, {CONF_UDP_PORT: 10103, CONF_TCP_PORT: 10103})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**ACCOUNT, "users": "501: Mikkel\n0502 = Anna\n"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"add_another": False}
    )
    await hass.async_block_till_done()

    # Leading zeros are dropped: panels pad the same user inconsistently.
    assert result["data"][CONF_ACCOUNTS][0]["users"] == {
        "501": "Mikkel",
        "502": "Anna",
    }


async def test_flow_rejects_unparsable_users(hass: HomeAssistant) -> None:
    """A malformed line is reported rather than silently dropped."""
    result = await _start(hass, {CONF_UDP_PORT: 10104, CONF_TCP_PORT: 10104})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ACCOUNT, "users": "501: Mikkel\nwho even knows"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"users": "invalid_users"}


async def test_flow_accepts_zone_names(hass: HomeAssistant) -> None:
    """Zone numbers typed one per line become a number to name mapping."""
    result = await _start(hass, {CONF_UDP_PORT: 10191, CONF_TCP_PORT: 10191})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**ACCOUNT, "zones": "010: Office Window Sensor\n11 = Hall"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"add_another": False}
    )
    await hass.async_block_till_done()

    assert result["data"][CONF_ACCOUNTS][0]["zones"] == {
        "10": "Office Window Sensor",
        "11": "Hall",
    }


async def test_flow_rejects_unparsable_zones(hass: HomeAssistant) -> None:
    """A malformed zone line is reported against the zones field."""
    result = await _start(hass, {CONF_UDP_PORT: 10192, CONF_TCP_PORT: 10192})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ACCOUNT, "zones": "10: Office\n10: Hall"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"zones": "invalid_zones"}


async def test_flow_rejects_duplicate_user_numbers(hass: HomeAssistant) -> None:
    """The same number twice is a typo, not an instruction to overwrite."""
    result = await _start(hass, {CONF_UDP_PORT: 10105, CONF_TCP_PORT: 10105})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ACCOUNT, "users": "501: Mikkel\n501: Anna"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"users": "invalid_users"}


async def test_invalid_bind_host(hass: HomeAssistant) -> None:
    """A hostname is rejected because the receiver binds an address."""
    result = await _start(hass, {CONF_BIND_HOST: "not-an-ip"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_BIND_HOST: "invalid_host"}


async def test_both_ports_disabled(hass: HomeAssistant) -> None:
    """Turning off both transports leaves nothing to listen on."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**RECEIVER, CONF_UDP_PORT: 0, CONF_TCP_PORT: 0},
    )
    assert result["errors"] == {"base": "no_transport"}


@pytest.mark.parametrize(
    "account",
    ["", "12", "12G4", "0123456789ABCDEF0"],
    ids=["empty", "too_short", "not_hex", "too_long"],
)
async def test_invalid_account_numbers(hass: HomeAssistant, account: str) -> None:
    """Account numbers must be 3 to 16 hex characters."""
    result = await _start(hass, {CONF_UDP_PORT: 10103, CONF_TCP_PORT: 10103})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ACCOUNT, CONF_ACCOUNT: account}
    )
    assert result["errors"] == {CONF_ACCOUNT: "invalid_account"}


@pytest.mark.parametrize(
    "key",
    ["ABCD", "ABCDABCDABCDABCDABCDABCDABCDABCZ", "ABCDABCDABCDABCDABCDABCDABCDABC"],
    ids=["too_short", "not_hex", "odd_length"],
)
async def test_invalid_keys(hass: HomeAssistant, key: str) -> None:
    """Keys must be 32, 48 or 64 hex characters."""
    result = await _start(hass, {CONF_UDP_PORT: 10104, CONF_TCP_PORT: 10104})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ACCOUNT, CONF_ENCRYPTION_KEY: key}
    )
    assert result["errors"] == {CONF_ENCRYPTION_KEY: "invalid_key"}


@pytest.mark.parametrize(
    "key",
    [
        "ABCDABCDABCDABCDABCDABCDABCDABCD",
        "ABCDABCDABCDABCDABCDABCDABCDABCDABCDABCDABCDABCD",
        "ABCDABCDABCDABCDABCDABCDABCDABCDABCDABCDABCDABCDABCDABCDABCDABCD",
    ],
    ids=["128_bit", "192_bit", "256_bit"],
)
async def test_valid_keys(hass: HomeAssistant, key: str) -> None:
    """All three DC-09 key lengths are accepted."""
    result = await _start(hass, {CONF_UDP_PORT: 10105, CONF_TCP_PORT: 10105})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ACCOUNT, CONF_ENCRYPTION_KEY: key}
    )
    assert result["step_id"] == "add_another"


async def test_new_encrypted_account_enforces_timestamps(hass: HomeAssistant) -> None:
    """Adding a key must switch on replay protection for a new account.

    The timestamp option is deliberately absent from the creation form: the
    key is entered on the same form, so a checkbox there would have to render
    the insecure default and would then be stored as a deliberate choice.
    """
    result = await _start(hass, {CONF_UDP_PORT: 10111, CONF_TCP_PORT: 10111})
    assert "ignore_timestamps" not in {
        str(key.schema) for key in result["data_schema"].schema
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**ACCOUNT, CONF_ENCRYPTION_KEY: "ABCDABCDABCDABCDABCDABCDABCDABCD"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"add_another": False}
    )
    await hass.async_block_till_done()

    assert result["data"][CONF_ACCOUNTS][0]["ignore_timestamps"] is False


async def test_duplicate_account_rejected(hass: HomeAssistant) -> None:
    """The same account cannot be added twice."""
    result = await _start(hass, {CONF_UDP_PORT: 10106, CONF_TCP_PORT: 10106})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(ACCOUNT)
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"add_another": True}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ACCOUNT, CONF_ACCOUNT: "1234"}
    )
    assert result["errors"] == {CONF_ACCOUNT: "duplicate_account"}


async def test_account_is_normalised(hass: HomeAssistant) -> None:
    """Account numbers are upper-cased so matching is consistent."""
    result = await _start(hass, {CONF_UDP_PORT: 10107, CONF_TCP_PORT: 10107})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ACCOUNT, CONF_ACCOUNT: "abcd", "name": ""}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"add_another": False}
    )
    await hass.async_block_till_done()

    account = result["data"][CONF_ACCOUNTS][0]
    assert account[CONF_ACCOUNT] == "ABCD"
    # An empty name falls back to the account number.
    assert account["name"] == "ABCD"


async def test_options_edit_receiver(hass: HomeAssistant) -> None:
    """Receiver settings can be changed after setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**RECEIVER, CONF_ACCOUNTS: [dict(ACCOUNT)]},
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "receiver"}
    )
    assert result["step_id"] == "receiver"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**RECEIVER, CONF_UDP_PORT: 10200, CONF_TCP_PORT: 10200}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_UDP_PORT] == 10200
    # The accounts survive a receiver-only edit.
    assert len(result["data"][CONF_ACCOUNTS]) == 1


async def test_adding_a_key_to_an_existing_account_enforces_timestamps(
    hass: HomeAssistant,
) -> None:
    """Encrypting an existing account must switch on replay protection.

    The stored value predates the key, and the checkbox on the edit form was
    rendered before the key existed, so neither is a considered choice about
    replay protection for an encrypted account.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **RECEIVER,
            CONF_ACCOUNTS: [{**ACCOUNT, "ignore_timestamps": True}],
        },
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit_account"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ACCOUNT: "1234"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **ACCOUNT,
            "ignore_timestamps": True,
            CONF_ENCRYPTION_KEY: "ABCDABCDABCDABCDABCDABCDABCDABCD",
        },
    )

    assert result["data"][CONF_ACCOUNTS][0]["ignore_timestamps"] is False


async def test_editing_an_encrypted_account_keeps_a_deliberate_opt_out(
    hass: HomeAssistant,
) -> None:
    """Once the key is already there, the checkbox is the user's decision."""
    stored = {**ACCOUNT, CONF_ENCRYPTION_KEY: "ABCDABCDABCDABCDABCDABCDABCDABCD"}
    entry = MockConfigEntry(
        domain=DOMAIN, data={**RECEIVER, CONF_ACCOUNTS: [stored]}, options={}
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit_account"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ACCOUNT: "1234"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**stored, "ignore_timestamps": True}
    )

    assert result["data"][CONF_ACCOUNTS][0]["ignore_timestamps"] is True


async def test_options_add_and_remove_account(hass: HomeAssistant) -> None:
    """Accounts can be added and removed from the options flow."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={**RECEIVER, CONF_ACCOUNTS: [dict(ACCOUNT)]}, options={}
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_account"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**ACCOUNT, CONF_ACCOUNT: "5678", "name": "Shed"}
    )
    assert [item[CONF_ACCOUNT] for item in result["data"][CONF_ACCOUNTS]] == [
        "1234",
        "5678",
    ]

    hass.config_entries.async_update_entry(entry, options=result["data"])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_account"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ACCOUNTS: ["1234"]}
    )
    assert [item[CONF_ACCOUNT] for item in result["data"][CONF_ACCOUNTS]] == ["5678"]


async def test_options_edit_account(hass: HomeAssistant) -> None:
    """An existing account's name and key can be changed."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={**RECEIVER, CONF_ACCOUNTS: [dict(ACCOUNT)]}, options={}
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit_account"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ACCOUNT: "1234"}
    )
    assert result["step_id"] == "edit_account"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**ACCOUNT, "name": "Back door"}
    )
    assert result["data"][CONF_ACCOUNTS][0]["name"] == "Back door"


async def test_options_edit_shows_existing_users(hass: HomeAssistant) -> None:
    """Reopening an account shows the user list as text, ready to edit."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **RECEIVER,
            CONF_ACCOUNTS: [{**ACCOUNT, "users": {"501": "Mikkel", "502": "Anna"}}],
        },
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit_account"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ACCOUNT: "1234"}
    )

    default = result["data_schema"]({})["users"]
    assert default == "501: Mikkel\n502: Anna"

    # Editing one line leaves the rest intact.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**ACCOUNT, "users": "501: Mikkel\n502: Anna B"}
    )
    assert result["data"][CONF_ACCOUNTS][0]["users"] == {
        "501": "Mikkel",
        "502": "Anna B",
    }


async def test_options_edit_keeps_a_bad_user_list_on_screen(
    hass: HomeAssistant,
) -> None:
    """A rejected list is shown back as typed, not silently emptied."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={**RECEIVER, CONF_ACCOUNTS: [dict(ACCOUNT)]}, options={}
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit_account"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ACCOUNT: "1234"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**ACCOUNT, "users": "501: Mikkel\nnonsense"}
    )

    assert result["errors"] == {"users": "invalid_users"}
    assert result["data_schema"]({})["users"] == "501: Mikkel\nnonsense"


async def test_options_edit_with_no_accounts_aborts(hass: HomeAssistant) -> None:
    """Editing is not offered when nothing is configured."""
    entry = MockConfigEntry(domain=DOMAIN, data={**RECEIVER, CONF_ACCOUNTS: []})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit_account"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_accounts"
