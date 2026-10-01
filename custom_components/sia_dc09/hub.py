"""The hub owns the receiver and everything derived from it.

One hub exists per config entry. It resolves per-account AES keys, folds
messages into per-account status, writes the activity log, and publishes each
event twice: on the dispatcher for entities and on the event bus for
automations. That dual dispatch mirrors Home Assistant's built-in ``sia``
integration so automations port across with minimal change.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
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
    CONF_USERS,
    CONF_ZONES,
    DEFAULT_BIND_HOST,
    DEFAULT_HEARTBEAT_TIMEOUT,
    DEFAULT_IGNORE_TIMESTAMPS,
    DEFAULT_NAK_ON_BAD_CRC,
    DEFAULT_RESPOND,
    DEFAULT_RETENTION_MONTHS,
    DEFAULT_UNKNOWN_ACCOUNT_POLICY,
    DUPLICATE_WINDOW_SECONDS,
    POLICY_AUTO_CREATE,
    POLICY_DISCOVER,
    POLICY_IGNORE,
    REPLAY_CACHE_SIZE,
    SIA_DC09_EVENT,
    SIA_DC09_EVENT_ALL,
    SIA_DC09_HUB_UPDATED,
    TCP_IDLE_TIMEOUT,
    TIMEBAND_FUTURE,
    TIMEBAND_PAST,
)
from .dc09 import ReceivedMessage, parse_key
from .discovery import UnknownAccountLog
from .listener import Dc09Receiver, ReceiverConfig
from .models import SiaDc09Event, build_event
from .state_machine import (
    AccountState,
    SiaStatus,
    StatusMapping,
    apply_message,
    restore_state,
)
from .store import ActivityStore
from .utils import clean_users, clean_zones, event_to_record, normalise_account

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
    #: User numbers mapped to the names they should be shown as.
    users: dict[str, str] = field(default_factory=dict)
    #: Zone and point numbers mapped to the names they should be shown as.
    zones: dict[str, str] = field(default_factory=dict)
    mapping: StatusMapping = field(default_factory=StatusMapping.build)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccountConfig:
        """Build an account configuration from stored config entry data."""
        account = normalise_account(data[CONF_ACCOUNT])
        raw_key = data.get(CONF_ENCRYPTION_KEY)
        key = parse_key(raw_key) if raw_key else None
        return cls(
            account=account,
            name=data.get(CONF_NAME) or account,
            key=key,
            heartbeat_timeout=data.get(
                CONF_HEARTBEAT_TIMEOUT, DEFAULT_HEARTBEAT_TIMEOUT
            ),
            # Encryption on its own does not prove freshness, so an encrypted
            # account enforces timestamps unless the user opts out. An
            # unencrypted account gains nothing from the check, because an
            # attacker could simply write whatever timestamp they liked.
            ignore_timestamps=data.get(
                CONF_IGNORE_TIMESTAMPS,
                False if key is not None else DEFAULT_IGNORE_TIMESTAMPS,
            ),
            users=clean_users(data.get(CONF_USERS)),
            zones=clean_zones(data.get(CONF_ZONES)),
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

        #: Accounts seen on the wire that are not configured, with the messages
        #: they sent and where they came from.
        self.unknown_accounts = UnknownAccountLog()
        self.message_count = 0

        self._receiver: Dc09Receiver | None = None
        self._unsubscribe: list[Any] = []

        #: Recently applied message digests per account, used to reject
        #: replays and panel retransmissions. Bounded by size and by age.
        self._recent_digests: defaultdict[str, deque[tuple[bytes, datetime]]] = (
            defaultdict(lambda: deque(maxlen=REPLAY_CACHE_SIZE))
        )

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
        await self.async_restore_states()

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

    async def async_restore_states(self) -> None:
        """Work out where each account stood, from what it has already sent.

        A panel only reports changes, so nothing arrives to say "still armed"
        after a restart. Without this the house would read as unknown until the
        next arm or disarm, which for an alarm left armed overnight is exactly
        when the state matters.

        Only accounts with no state yet are restored, so a reload caused by an
        options change never overwrites what is already known.
        """
        for account in self.accounts:
            if self.states.get(account, AccountState()).status is not SiaStatus.UNKNOWN:
                continue
            history = await self.store.async_get_history(account)
            if not history.statuses and history.last_message_at is None:
                continue
            self.states[account] = restore_state(
                history.statuses,
                last_message_at=history.last_message_at,
                last_activity_at=history.last_activity_at,
                last_code=history.last_code,
                changed_by_user=history.changed_by_user,
                changed_by_zone=history.changed_by_zone,
            )
            _LOGGER.debug(
                "Restored account %s as %s from stored activity",
                account,
                self.states[account].status,
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
            # Either the frame is corrupt, or it is cleartext for an account
            # that requires encryption. In both cases the payload is
            # attacker-controlled and unauthenticated, so it must not reach
            # entities or the event bus - a forged code would otherwise be
            # able to trip a smoke or power sensor. It is still recorded so
            # the user can see a misconfigured or misbehaving panel.
            _LOGGER.warning(
                "Rejecting message for account %s: %s",
                account,
                "; ".join(message.decode.errors),
            )
            if config is not None:
                await self._async_record_only(
                    build_event(message, account, None, config.users, config.zones)
                )
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

        if self._is_duplicate(config, account, message):
            # Most likely a panel retransmitting because our ACK was lost.
            # The listener has already acknowledged it, so applying it a
            # second time would be wrong.
            _LOGGER.debug(
                "Ignoring duplicate message for account %s (sequence %s)",
                account,
                frame.sequence,
            )
            return

        transition = apply_message(
            self.states.get(account, AccountState()),
            message.decode,
            config.mapping,
            message.received_at,
        )
        event = build_event(message, account, transition, config.users, config.zones)
        state = transition.state
        if transition.changed:
            # Who and what caused a change is replaced on every change, even
            # with nothing, so an alarm from a zone is never credited to the
            # user who armed the system earlier.
            state = replace(
                state,
                changed_by_user=event.user_number,
                changed_by_zone=event.zone_number,
            )
        self.states[account] = state

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

    async def _async_record_only(self, event: SiaDc09Event) -> None:
        """Store an event for auditing without letting anything act on it.

        Used for messages that failed validation. They belong in the activity
        log so a bad panel is visible, but they must never reach entities or
        automations, because nothing in them has been authenticated.
        """
        await self.store.async_add(event_to_record(event))
        async_dispatcher_send(
            self.hass, SIA_DC09_HUB_UPDATED.format(self.entry.entry_id)
        )

    # --- policies ------------------------------------------------------------

    async def _async_handle_unknown_account(
        self, account: str, message: ReceivedMessage
    ) -> None:
        """Apply the configured policy to an account we do not monitor."""
        policy = self.unknown_account_policy

        if policy == POLICY_IGNORE:
            # Deliberately records nothing at all: this policy exists so a
            # receiver exposed to noisy neighbours can be told to shut up.
            return

        event = build_event(message, account, None)
        # Kept in memory rather than only counted, so the user can see who is
        # transmitting and what they said without turning on debug logging.
        self.unknown_accounts.record(account, event)

        if policy == POLICY_AUTO_CREATE:
            _LOGGER.info("Adding account %s seen on the wire", account)
            accounts = [*self.options.get(CONF_ACCOUNTS, []), {CONF_ACCOUNT: account}]
            self.hass.config_entries.async_update_entry(
                self.entry, options={**self.entry.options, CONF_ACCOUNTS: accounts}
            )
            # The options update listener reloads the entry, which replays
            # nothing; log the message that prompted the creation so it is not
            # lost entirely.
            await self.store.async_add(event_to_record(event))
            return

        # POLICY_DISCOVER: surface it, but change nothing automatically.
        _LOGGER.debug(
            "Message from unconfigured account %s via %s from %s",
            account,
            message.transport,
            message.remote_ip,
        )
        await self._async_publish(event)

    def _timestamp_is_acceptable(
        self, config: AccountConfig, message: ReceivedMessage
    ) -> bool:
        """Return whether a message's timestamp is within the allowed band.

        Rejecting stale timestamps defeats replay attacks, which is the reason
        DC-09 carries one at all, but a panel with a wrong clock would then go
        silent. Accounts without a key therefore ignore timestamps by default,
        since an unauthenticated timestamp is attacker-writable anyway.
        """
        if config.ignore_timestamps:
            return True
        frame = message.decode.frame
        if frame is None:
            return False
        if frame.timestamp_drift_seconds is None:
            # Enforcement is on but the message carries no usable timestamp.
            # Accepting it would let an attacker bypass the check simply by
            # omitting or corrupting the field.
            return False
        drift = frame.timestamp_drift_seconds
        return -TIMEBAND_FUTURE <= drift <= TIMEBAND_PAST

    def _is_duplicate(
        self, config: AccountConfig, account: str, message: ReceivedMessage
    ) -> bool:
        """Return whether this exact message arrived moments ago.

        Keyed on a digest of the raw bytes, so it catches a panel retransmitting
        after a lost ACK as well as an immediate replay.

        How long a digest is worth keeping depends on whether timestamps are
        enforced. When they are, the cache has to outlive the window in which a
        captured message still looks fresh, or a replay simply waits for the
        digest to expire; a genuinely new event carries a different timestamp or
        sequence number, so it hashes differently and is never mistaken for a
        duplicate. When timestamps are ignored, two identical events really are
        indistinguishable, so the window stays short and this only absorbs
        retransmissions. Either way it deduplicates, it does not authenticate.
        """
        digest = hashlib.sha256(message.raw).digest()
        now = message.received_at
        seen = self._recent_digests[account]

        window = (
            DUPLICATE_WINDOW_SECONDS
            if config.ignore_timestamps
            else TIMEBAND_PAST + TIMEBAND_FUTURE
        )
        cutoff = now - timedelta(seconds=window)
        while seen and seen[0][1] < cutoff:
            seen.popleft()

        if any(known == digest for known, _ in seen):
            return True

        seen.append((digest, now))
        return False

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

    def changed_by(self, account: str) -> str | None:
        """Return who last changed an account's status.

        That is the configured name for the user number when there is one, and
        the bare number otherwise. Names are looked up here rather than when
        the message arrives, so renaming a user shows immediately.
        """
        account = normalise_account(account)
        state = self.states.get(account)
        config = self.accounts.get(account)
        if state is None or state.changed_by_user is None:
            return None
        names = config.users if config is not None else {}
        return names.get(state.changed_by_user, state.changed_by_user)

    def changed_by_zone(self, account: str) -> str | None:
        """Return the zone or point that last changed an account's status.

        The configured name when there is one, the bare number otherwise.
        """
        account = normalise_account(account)
        state = self.states.get(account)
        config = self.accounts.get(account)
        if state is None or state.changed_by_zone is None:
            return None
        names = config.zones if config is not None else {}
        return names.get(state.changed_by_zone, state.changed_by_zone)

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
        return [record.account for record in self.unknown_accounts.busiest()]

    @property
    def unknown_account_details(self) -> list[dict[str, Any]]:
        """Return what has been heard from each unconfigured account."""
        if self.unknown_account_policy == POLICY_IGNORE:
            return []
        return self.unknown_accounts.as_list()


__all__ = [
    "POLICY_AUTO_CREATE",
    "POLICY_DISCOVER",
    "POLICY_IGNORE",
    "AccountConfig",
    "SiaDc09Hub",
]
