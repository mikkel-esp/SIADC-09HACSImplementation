"""The hub owns the receiver and everything derived from it.

One hub exists per config entry. It resolves per-account AES keys, folds
messages into per-account status, writes the activity log, and publishes each
event twice: on the dispatcher for entities and on the event bus for
automations. That dual dispatch mirrors Home Assistant's built-in ``sia``
integration so automations port across with minimal change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACCOUNT,
    CONF_ACCOUNTS,
    CONF_BIND_HOST,
    CONF_ENCRYPTION_KEY,
    CONF_HEARTBEAT_TIMEOUT,
    CONF_IGNORE_TIMESTAMPS,
    CONF_NAK_ON_BAD_CRC,
    CONF_RESPOND,
    CONF_RETENTION_MONTHS,
    CONF_STATUS_MAP_OVERRIDE,
    CONF_TCP_PORT,
    CONF_TEST_CODES_OVERRIDE,
    CONF_UDP_PORT,
    CONF_UNKNOWN_ACCOUNT_POLICY,
    DEFAULT_BIND_HOST,
    DEFAULT_HEARTBEAT_TIMEOUT,
    DEFAULT_IGNORE_TIMESTAMPS,
    DEFAULT_NAK_ON_BAD_CRC,
    DEFAULT_RESPOND,
    DEFAULT_RETENTION_MONTHS,
    DEFAULT_UNKNOWN_ACCOUNT_POLICY,
    POLICY_AUTO_CREATE,
    POLICY_DISCOVER,
    POLICY_IGNORE,
    SIA_DC09_EVENT,
    SIA_DC09_EVENT_ALL,
    SIA_DC09_HUB_UPDATED,
    TCP_IDLE_TIMEOUT,
    TIMEBAND_FUTURE,
    TIMEBAND_PAST,
)
from .dc09 import ReceivedMessage, parse_key
from .listener import Dc09Receiver, ReceiverConfig
from .models import SiaDc09Event, build_event
from .state_machine import AccountState, SiaStatus, StatusMapping, apply_message
from .store import ActivityStore
from .utils import event_to_record, normalise_account

_LOGGER = logging.getLogger(__name__)

#: How often the retention purge runs. Two years of history does not need a
#: tighter schedule than this.
PURGE_INTERVAL = timedelta(hours=12)

#: How often heartbeat timeouts are re-evaluated.
HEARTBEAT_INTERVAL = timedelta(minutes=1)


@dataclass
class AccountConfig:
    """The configuration of one monitored account."""

    account: str
    name: str
    key: bytes | None = None
    heartbeat_timeout: int = DEFAULT_HEARTBEAT_TIMEOUT
    ignore_timestamps: bool = DEFAULT_IGNORE_TIMESTAMPS
    mapping: StatusMapping = field(default_factory=StatusMapping.build)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccountConfig:
        """Build an account configuration from stored config entry data."""
        account = normalise_account(data[CONF_ACCOUNT])
        raw_key = data.get(CONF_ENCRYPTION_KEY)
        return cls(
            account=account,
            name=data.get(CONF_NAME) or account,
            key=parse_key(raw_key) if raw_key else None,
            heartbeat_timeout=data.get(
                CONF_HEARTBEAT_TIMEOUT, DEFAULT_HEARTBEAT_TIMEOUT
            ),
            ignore_timestamps=data.get(
                CONF_IGNORE_TIMESTAMPS, DEFAULT_IGNORE_TIMESTAMPS
            ),
            mapping=StatusMapping.build(
                data.get(CONF_STATUS_MAP_OVERRIDE),
                data.get(CONF_TEST_CODES_OVERRIDE),
            ),
        )


class SiaDc09Hub:
    """Receives DC-09 traffic for one config entry and fans it out."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Build the hub from a config entry without opening any sockets."""
        self.hass = hass
        self.entry = entry
        self.accounts: dict[str, AccountConfig] = {}
        self.states: dict[str, AccountState] = {}
        self.store = ActivityStore(hass, entry.entry_id)

        #: Accounts seen on the wire that are not configured, and how often.
        self.unknown_accounts: dict[str, int] = {}
        self.message_count = 0

        self._receiver: Dc09Receiver | None = None
        self._unsubscribe: list[Any] = []

        self.reload_options()

    # --- configuration -------------------------------------------------------

    @property
    def options(self) -> dict[str, Any]:
        """Return the entry's options, falling back to its initial data."""
        return {**self.entry.data, **self.entry.options}

    @property
    def unknown_account_policy(self) -> str:
        """Return what to do about accounts that are not configured."""
        return self.options.get(
            CONF_UNKNOWN_ACCOUNT_POLICY, DEFAULT_UNKNOWN_ACCOUNT_POLICY
        )

    @property
    def retention_months(self) -> int:
        """Return how long activity is kept."""
        return self.options.get(CONF_RETENTION_MONTHS, DEFAULT_RETENTION_MONTHS)

    def reload_options(self) -> None:
        """Rebuild the account table from the config entry.

        Existing per-account state is preserved so a settings change does not
        wipe an account's status or heartbeat.
        """
        options = self.options
        accounts = {}
        for raw in options.get(CONF_ACCOUNTS, []):
            config = AccountConfig.from_dict(raw)
            accounts[config.account] = config
        self.accounts = accounts

        for account in accounts:
            self.states.setdefault(account, AccountState())
        for stale in set(self.states) - set(accounts):
            del self.states[stale]

    # --- lifecycle -----------------------------------------------------------

    async def async_setup(self) -> None:
        """Open the database and start listening."""
        await self.store.async_setup()

        options = self.options
        self._receiver = Dc09Receiver(
            ReceiverConfig(
                bind_host=options.get(CONF_BIND_HOST, DEFAULT_BIND_HOST),
                udp_port=options.get(CONF_UDP_PORT),
                tcp_port=options.get(CONF_TCP_PORT),
                respond=options.get(CONF_RESPOND, DEFAULT_RESPOND),
                nak_on_bad_crc=options.get(CONF_NAK_ON_BAD_CRC, DEFAULT_NAK_ON_BAD_CRC),
                key_for=self.key_for,
                on_message=self.async_handle_message,
                is_known_account=self.is_known_account,
                tcp_idle_timeout=TCP_IDLE_TIMEOUT,
            )
        )
        await self._receiver.async_start()

        self._unsubscribe.append(
            async_track_time_interval(self.hass, self._async_purge, PURGE_INTERVAL)
        )
        self._unsubscribe.append(
            async_track_time_interval(
                self.hass, self._async_check_heartbeats, HEARTBEAT_INTERVAL
            )
        )

    async def async_unload(self) -> None:
        """Stop listening and flush anything still buffered."""
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()

        if self._receiver is not None:
            await self._receiver.async_stop()
            self._receiver = None

        await self.store.async_close()

    # --- receiver callbacks --------------------------------------------------

    def key_for(self, account: str) -> bytes | None:
        """Return the AES key configured for an account, if any.

        Called with the account read from the cleartext DC-09 header, before
        the body is decrypted.
        """
        config = self.accounts.get(normalise_account(account))
        return config.key if config is not None else None

    def is_known_account(self, account: str) -> bool:
        """Return whether an account should be acknowledged.

        Under the ``ignore`` policy an unconfigured account is answered with a
        DUH so the panel stops retrying. The other policies acknowledge it,
        because the user intends to adopt it.
        """
        if normalise_account(account) in self.accounts:
            return True
        return self.unknown_account_policy != POLICY_IGNORE

    async def async_handle_message(self, message: ReceivedMessage) -> None:
        """Process one received message."""
        self.message_count += 1

        frame = message.decode.frame
        if frame is None:
            _LOGGER.debug(
                "Undecodable message from %s: %s",
                message.remote_ip,
                "; ".join(message.decode.errors),
            )
            async_dispatcher_send(
                self.hass, SIA_DC09_HUB_UPDATED.format(self.entry.entry_id)
            )
            return

        account = normalise_account(frame.account)
        config = self.accounts.get(account)

        if not message.decode.ok:
            # The frame was readable enough to name an account, but its
            # checksum or length is wrong, so nothing in it can be trusted to
            # move an alarm state. It is still logged so the user can see a
            # panel that is transmitting badly.
            _LOGGER.warning(
                "Rejecting corrupt message for account %s: %s",
                account,
                "; ".join(message.decode.errors),
            )
            if config is not None:
                await self._async_publish(build_event(message, account, None))
            return

        if config is None:
            await self._async_handle_unknown_account(account, message)
            return

        if not self._timestamp_is_acceptable(config, message):
            _LOGGER.warning(
                "Rejecting message for account %s: timestamp drifts %s seconds",
                account,
                frame.timestamp_drift_seconds,
            )
            return

        transition = apply_message(
            self.states.get(account, AccountState()),
            message.decode,
            config.mapping,
            message.received_at,
        )
        self.states[account] = transition.state

        event = build_event(message, account, transition)
        await self._async_publish(event)

    # --- publishing ----------------------------------------------------------

    async def _async_publish(self, event: SiaDc09Event) -> None:
        """Store an event and fan it out to entities and automations.

        Everything is stored, including automatic tests, because a missing
        heartbeat is itself worth being able to audit. Readers filter tests out
        by default.
        """
        await self.store.async_add(event_to_record(event))

        payload = event.as_bus_payload()

        # Entities get the rich object over the dispatcher...
        async_dispatcher_send(self.hass, SIA_DC09_EVENT.format(event.account), event)
        # ...and automations get a JSON-safe payload on the bus. Both a
        # per-account event type and a catch-all are fired, so a user can
        # subscribe to one panel or to everything.
        self.hass.bus.async_fire(SIA_DC09_EVENT.format(event.account), payload)
        self.hass.bus.async_fire(SIA_DC09_EVENT_ALL, payload)
        async_dispatcher_send(
            self.hass, SIA_DC09_HUB_UPDATED.format(self.entry.entry_id)
        )

    # --- policies ------------------------------------------------------------

    async def _async_handle_unknown_account(
        self, account: str, message: ReceivedMessage
    ) -> None:
        """Apply the configured policy to an account we do not monitor."""
        self.unknown_accounts[account] = self.unknown_accounts.get(account, 0) + 1
        policy = self.unknown_account_policy

        if policy == POLICY_IGNORE:
            return

        if policy == POLICY_AUTO_CREATE:
            _LOGGER.info("Adding account %s seen on the wire", account)
            accounts = [*self.options.get(CONF_ACCOUNTS, []), {CONF_ACCOUNT: account}]
            self.hass.config_entries.async_update_entry(
                self.entry, options={**self.entry.options, CONF_ACCOUNTS: accounts}
            )
            # The options update listener reloads the entry, which replays
            # nothing; log the message that prompted the creation so it is not
            # lost entirely.
            await self.store.async_add(
                event_to_record(build_event(message, account, None))
            )
            return

        # POLICY_DISCOVER: surface it, but change nothing automatically.
        _LOGGER.debug("Message from unconfigured account %s", account)
        await self._async_publish(build_event(message, account, None))

    def _timestamp_is_acceptable(
        self, config: AccountConfig, message: ReceivedMessage
    ) -> bool:
        """Return whether a message's timestamp is within the allowed band.

        Rejecting stale timestamps defeats replay attacks, which is the reason
        DC-09 carries one at all, but a panel with a wrong clock would then go
        silent. The per-account default is therefore to ignore timestamps.
        """
        if config.ignore_timestamps:
            return True
        frame = message.decode.frame
        if frame is None or frame.timestamp_drift_seconds is None:
            return True
        drift = frame.timestamp_drift_seconds
        return -TIMEBAND_FUTURE <= drift <= TIMEBAND_PAST

    # --- scheduled work ------------------------------------------------------

    async def _async_purge(self, _now) -> None:
        """Drop activity older than the retention window."""
        await self.store.async_purge(self.retention_months)

    @callback
    def _async_check_heartbeats(self, _now) -> None:
        """Nudge entities so connectivity sensors can go stale on time."""
        async_dispatcher_send(
            self.hass, SIA_DC09_HUB_UPDATED.format(self.entry.entry_id)
        )

    # --- queries used by entities and services -------------------------------

    def status_of(self, account: str) -> SiaStatus:
        """Return the current status of an account."""
        state = self.states.get(normalise_account(account))
        return state.status if state is not None else SiaStatus.UNKNOWN

    def is_online(self, account: str) -> bool | None:
        """Return whether an account has reported within its heartbeat window.

        ``None`` means nothing has ever been heard from it.
        """
        config = self.accounts.get(normalise_account(account))
        state = self.states.get(normalise_account(account))
        if config is None or state is None or state.last_message_at is None:
            return None
        deadline = state.last_message_at + timedelta(minutes=config.heartbeat_timeout)
        return dt_util.utcnow() <= deadline

    @property
    def udp_port(self) -> int | None:
        """Return the UDP port actually bound, if any."""
        return self._receiver.udp_port if self._receiver is not None else None

    @property
    def tcp_port(self) -> int | None:
        """Return the TCP port actually bound, if any."""
        return self._receiver.tcp_port if self._receiver is not None else None

    @property
    def discovered_accounts(self) -> list[str]:
        """Return unconfigured accounts seen on the wire, busiest first."""
        if self.unknown_account_policy == POLICY_IGNORE:
            return []
        return sorted(
            self.unknown_accounts, key=self.unknown_accounts.get, reverse=True
        )


__all__ = [
    "POLICY_AUTO_CREATE",
    "POLICY_DISCOVER",
    "POLICY_IGNORE",
    "AccountConfig",
    "SiaDc09Hub",
]
