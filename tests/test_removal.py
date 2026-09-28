"""Tests for removing accounts, devices and the integration itself."""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from custom_components.sia_dc09 import async_remove_config_entry_device
from custom_components.sia_dc09.const import CONF_ACCOUNT, CONF_ACCOUNTS, DOMAIN
from custom_components.sia_dc09.store import ActivityStore
from tests.test_integration import ACCOUNT, body, make_entry, send_udp


@pytest.fixture(autouse=True)
def _auto_enable(enable_custom_integrations):
    """Let Home Assistant load the integration from custom_components."""
    return


@pytest.fixture(autouse=True)
def _clean_database(hass: HomeAssistant):
    """Start each test with no database on disk.

    The test configuration directory is reused between runs, and these tests
    assert on the file itself, so a leftover from an earlier run would make
    them pass or fail for the wrong reason.
    """
    _unlink_database(hass)
    yield
    _unlink_database(hass)


def _unlink_database(hass: HomeAssistant) -> None:
    """Delete the activity database and its write-ahead sidecars."""
    base = db_path(hass)
    for path in (
        base,
        base.with_name(f"{base.name}-wal"),
        base.with_name(f"{base.name}-shm"),
    ):
        path.unlink(missing_ok=True)


def db_path(hass: HomeAssistant) -> Path:
    """Return where the activity database lives for this test run."""
    return Path(hass.config.path("sia_dc09.db"))


def device_for(hass: HomeAssistant, entry_id: str, identifier: str):
    """Return a device by its identifier suffix, or None."""
    registry = dr.async_get(hass)
    return registry.async_get_device(identifiers={(DOMAIN, identifier)})


async def test_removing_the_integration_deletes_its_history(
    hass: HomeAssistant,
) -> None:
    """Deleting the integration must not leave two years of alarms on disk."""
    entry = await make_entry(hass)
    await send_udp(hass, entry, body("BA001"))

    hub = hass.data[DOMAIN][entry.entry_id]
    await hub.store.async_flush()
    assert db_path(hass).exists()

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert not db_path(hass).exists()


async def test_removing_one_receiver_keeps_the_others_history(
    hass: HomeAssistant,
) -> None:
    """The database is shared, so one removal must not wipe another receiver."""
    first = await make_entry(hass)
    second = await make_entry(hass, accounts=[{CONF_ACCOUNT: "5678", "name": "Garage"}])

    await send_udp(hass, first, body("BA001"))
    await send_udp(hass, second, body("BA002", account="5678"))

    kept = hass.data[DOMAIN][second.entry_id]
    await kept.store.async_flush()

    assert await hass.config_entries.async_remove(first.entry_id)
    await hass.async_block_till_done()

    assert db_path(hass).exists()
    store = ActivityStore(hass, second.entry_id)
    assert await store.async_count() == 1
    remaining = await store.async_get_activity(include_tests=True)
    assert remaining[0]["account"] == "5678"


async def test_removing_an_unloaded_entry_still_cleans_up(
    hass: HomeAssistant,
) -> None:
    """An entry that was unloaded first must still take its history with it."""
    entry = await make_entry(hass)
    await send_udp(hass, entry, body("BA001"))

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert not db_path(hass).exists()


async def test_unloading_releases_the_ports(hass: HomeAssistant) -> None:
    """A receiver that keeps its sockets could never be removed cleanly."""
    entry = await make_entry(hass)
    hub = hass.data[DOMAIN][entry.entry_id]
    port = hub.udp_port

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # The same port is free again, which is the observable proof.
    reused = await make_entry(hass, udp_port=port, tcp_port=0)
    assert reused.state is ConfigEntryState.LOADED


async def test_hub_device_cannot_be_deleted_on_its_own(hass: HomeAssistant) -> None:
    """It represents the entry, so it would reappear on the next reload."""
    entry = await make_entry(hass)

    device = device_for(hass, entry.entry_id, entry.entry_id)
    assert device is not None
    with pytest.raises(HomeAssistantError, match="Delete"):
        await async_remove_config_entry_device(hass, entry, device)


async def test_configured_account_device_cannot_be_deleted(
    hass: HomeAssistant,
) -> None:
    """Deleting a device for an account still configured would be a lie."""
    entry = await make_entry(hass)

    device = device_for(hass, entry.entry_id, f"{entry.entry_id}_{ACCOUNT}")
    assert device is not None
    # The refusal has to say what to do instead, not just refuse.
    with pytest.raises(HomeAssistantError, match="Remove accounts"):
        await async_remove_config_entry_device(hass, entry, device)


async def test_stale_account_device_can_be_deleted(hass: HomeAssistant) -> None:
    """A device left over from a removed account is the user's to clear."""
    entry = await make_entry(hass)
    registry = dr.async_get(hass)
    stale = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_9999")},
        name="Gone",
    )

    assert await async_remove_config_entry_device(hass, entry, stale)


async def test_device_removal_works_while_the_entry_is_unloaded(
    hass: HomeAssistant,
) -> None:
    """The check must not depend on the hub being in memory."""
    entry = await make_entry(hass)
    device = device_for(hass, entry.entry_id, f"{entry.entry_id}_{ACCOUNT}")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # Previously raised KeyError, which the UI reported as a failed removal.
    with pytest.raises(HomeAssistantError):
        await async_remove_config_entry_device(hass, entry, device)


async def test_removing_an_account_removes_its_device(hass: HomeAssistant) -> None:
    """An account taken out of the options should not leave clutter behind."""
    entry = await make_entry(
        hass,
        accounts=[
            {CONF_ACCOUNT: ACCOUNT, "name": "Front door"},
            {CONF_ACCOUNT: "5678", "name": "Garage"},
        ],
    )
    assert device_for(hass, entry.entry_id, f"{entry.entry_id}_5678") is not None

    hass.config_entries.async_update_entry(
        entry,
        options={CONF_ACCOUNTS: [{CONF_ACCOUNT: ACCOUNT, "name": "Front door"}]},
    )
    await hass.async_block_till_done()

    assert device_for(hass, entry.entry_id, f"{entry.entry_id}_5678") is None
    assert device_for(hass, entry.entry_id, f"{entry.entry_id}_{ACCOUNT}") is not None
