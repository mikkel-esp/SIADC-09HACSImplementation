"""Services exposed by the SIA DC-09 integration."""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import replace
from typing import Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_ACCOUNT,
    ATTR_END,
    ATTR_INCLUDE_TESTS,
    ATTR_LIMIT,
    ATTR_MESSAGE,
    ATTR_SEVERITY,
    ATTR_START,
    ATTR_STATUS,
    DOMAIN,
    SERVICE_CLEAR_ACTIVITY,
    SERVICE_DECODE_MESSAGE,
    SERVICE_GET_ACTIVITY,
    SERVICE_PURGE,
    SERVICE_SET_STATUS,
    SIA_DC09_HUB_UPDATED,
)
from .dc09 import (
    Dc09CryptoError,
    build_frame,
    decode,
    parse_key,
    to_hex,
    wire_to_bytes,
)
from .state_machine import SiaStatus
from .utils import normalise_account

_LOGGER = logging.getLogger(__name__)

GET_ACTIVITY_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ACCOUNT): cv.string,
        vol.Optional(ATTR_START): cv.datetime,
        vol.Optional(ATTR_END): cv.datetime,
        vol.Optional(ATTR_LIMIT, default=100): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=10000)
        ),
        vol.Optional(ATTR_INCLUDE_TESTS, default=False): cv.boolean,
        vol.Optional(ATTR_SEVERITY): cv.string,
    }
)

CLEAR_ACTIVITY_SCHEMA = vol.Schema({vol.Optional(ATTR_ACCOUNT): cv.string})

PURGE_SCHEMA = vol.Schema({})

SET_STATUS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ACCOUNT): cv.string,
        vol.Required(ATTR_STATUS): vol.In([status.value for status in SiaStatus]),
    }
)

DECODE_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional("key"): cv.string,
    }
)


def _hubs(hass: HomeAssistant) -> list[Any]:
    """Return every configured hub."""
    return list(hass.data.get(DOMAIN, {}).values())


def _hub_for_account(hass: HomeAssistant, account: str):
    """Return the hub that monitors an account."""
    wanted = normalise_account(account)
    for hub in _hubs(hass):
        if wanted in hub.accounts:
            return hub
    raise ServiceValidationError(f"No configured SIA DC-09 account named {account}")


async def _async_get_activity(call: ServiceCall) -> ServiceResponse:
    """Return stored activity, merged across every configured receiver."""
    account = call.data.get(ATTR_ACCOUNT)
    events: list[dict[str, Any]] = []

    hubs = [_hub_for_account(call.hass, account)] if account else _hubs(call.hass)
    for hub in hubs:
        events.extend(
            await hub.store.async_get_activity(
                account=normalise_account(account) if account else None,
                start=call.data.get(ATTR_START),
                end=call.data.get(ATTR_END),
                limit=call.data[ATTR_LIMIT],
                include_tests=call.data[ATTR_INCLUDE_TESTS],
                severity=call.data.get(ATTR_SEVERITY),
            )
        )

    events.sort(key=lambda row: row["received_at"], reverse=True)
    events = events[: call.data[ATTR_LIMIT]]
    return {"count": len(events), "events": events}


async def _async_clear_activity(call: ServiceCall) -> None:
    """Delete stored activity, for one account or for everything."""
    account = call.data.get(ATTR_ACCOUNT)
    hubs = [_hub_for_account(call.hass, account)] if account else _hubs(call.hass)
    for hub in hubs:
        deleted = await hub.store.async_clear(
            normalise_account(account) if account else None
        )
        _LOGGER.info("Cleared %d stored SIA DC-09 events", deleted)


async def _async_purge(call: ServiceCall) -> None:
    """Run the retention purge immediately instead of waiting for the timer."""
    for hub in _hubs(call.hass):
        deleted = await hub.store.async_purge(hub.retention_months)
        _LOGGER.info("Purged %d SIA DC-09 events past retention", deleted)


async def _async_set_status(call: ServiceCall) -> None:
    """Override an account's status.

    Needed because DC-09 is one way: if a panel is disarmed at the keypad and
    the closing report is lost, nothing will ever correct Home Assistant on its
    own.
    """
    account = normalise_account(call.data[ATTR_ACCOUNT])
    status = SiaStatus(call.data[ATTR_STATUS])
    hub = _hub_for_account(call.hass, account)

    current = hub.states[account]
    hub.states[account] = replace(current, status=status, previous=current.status)
    async_dispatcher_send(call.hass, SIA_DC09_HUB_UPDATED.format(hub.entry.entry_id))


def _to_datagram(raw: str) -> bytes:
    """Turn whatever the user pasted into bytes to decode.

    Three things are accepted, because all three are what people actually have
    to hand: a hex dump, a full wire capture including the framing, and a bare
    message body copied out of a panel manual or another receiver's log. The
    body is re-framed so its checksum and length are computed rather than
    reported as wrong.
    """
    with suppress(ValueError):
        return bytes.fromhex(raw.replace(" ", "").replace("\n", ""))

    datagram = wire_to_bytes(raw)
    if raw.startswith('"') or raw.startswith('*"'):
        return build_frame(raw)
    return datagram


async def _async_decode_message(call: ServiceCall) -> ServiceResponse:
    """Decode a DC-09 message by hand, for troubleshooting a panel."""
    raw = call.data[ATTR_MESSAGE].strip()

    key = None
    if raw_key := call.data.get("key"):
        try:
            key = parse_key(raw_key)
        except Dc09CryptoError as err:
            raise ServiceValidationError(str(err)) from err
        if key is None:
            raise ServiceValidationError("The supplied key is not valid")

    datagram = _to_datagram(raw)
    result = decode(datagram, key=key, received_at=dt_util.utcnow())
    frame = result.frame
    payload = result.payload

    return {
        "ok": result.ok,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "hex": to_hex(datagram),
        "frame": None
        if frame is None
        else {
            "protocol": frame.protocol,
            "encrypted": frame.encrypted,
            "sequence": frame.sequence,
            "receiver": frame.receiver,
            "line_prefix": frame.line_prefix,
            "account": frame.account,
            "body": frame.body,
            "crc_valid": frame.crc.valid,
            "length_valid": frame.length.valid,
            "timestamp": frame.timestamp,
        },
        "events": []
        if payload is None
        else [
            {
                "code": event.code,
                "qualifier": event.qualifier,
                "address": event.address,
                "area": event.area,
                "user": event.user,
                "text": event.text,
            }
            for event in payload.events
        ],
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's services once."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_ACTIVITY):
        return

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_ACTIVITY,
        _async_get_activity,
        schema=GET_ACTIVITY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_ACTIVITY,
        _async_clear_activity,
        schema=CLEAR_ACTIVITY_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PURGE, _async_purge, schema=PURGE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_STATUS, _async_set_status, schema=SET_STATUS_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DECODE_MESSAGE,
        _async_decode_message,
        schema=DECODE_MESSAGE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


@callback
def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the services when the last entry goes away."""
    for service in (
        SERVICE_GET_ACTIVITY,
        SERVICE_CLEAR_ACTIVITY,
        SERVICE_PURGE,
        SERVICE_SET_STATUS,
        SERVICE_DECODE_MESSAGE,
    ):
        hass.services.async_remove(DOMAIN, service)
