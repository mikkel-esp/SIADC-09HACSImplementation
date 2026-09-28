"""Constants for the SIA DC-09 Control Center integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "sia_dc09"

PLATFORMS: Final[list[Platform]] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
]

# --- Config entry keys -------------------------------------------------------

CONF_BIND_HOST: Final = "bind_host"
CONF_UDP_PORT: Final = "udp_port"
CONF_TCP_PORT: Final = "tcp_port"
CONF_RESPOND: Final = "respond"
CONF_NAK_ON_BAD_CRC: Final = "nak_on_bad_crc"
CONF_UNKNOWN_ACCOUNT_POLICY: Final = "unknown_account_policy"
CONF_RETENTION_MONTHS: Final = "retention_months"
CONF_RECENT_EVENTS: Final = "recent_events_in_attributes"

CONF_ACCOUNTS: Final = "accounts"
CONF_ACCOUNT: Final = "account"
CONF_ENCRYPTION_KEY: Final = "encryption_key"
CONF_HEARTBEAT_TIMEOUT: Final = "heartbeat_timeout"
CONF_IGNORE_TIMESTAMPS: Final = "ignore_timestamps"
CONF_ARM_AWAY_TARGET: Final = "arm_away_target"
CONF_ARM_HOME_TARGET: Final = "arm_home_target"
CONF_ARM_NIGHT_TARGET: Final = "arm_night_target"
CONF_DISARM_TARGET: Final = "disarm_target"
CONF_STATUS_MAP_OVERRIDE: Final = "status_map_override"
CONF_TEST_CODES_OVERRIDE: Final = "test_codes_override"

# --- Defaults ----------------------------------------------------------------

DEFAULT_BIND_HOST: Final = "0.0.0.0"  # noqa: S104 - binding all interfaces is the point
DEFAULT_UDP_PORT: Final = 10000
DEFAULT_TCP_PORT: Final = 10000
DEFAULT_RESPOND: Final = True
DEFAULT_NAK_ON_BAD_CRC: Final = True
DEFAULT_RETENTION_MONTHS: Final = 24
DEFAULT_RECENT_EVENTS: Final = 50
DEFAULT_HEARTBEAT_TIMEOUT: Final = 90  # minutes
DEFAULT_IGNORE_TIMESTAMPS: Final = True

#: Allowed clock drift, in seconds, when timestamps are enforced. Matches the
#: default timeband of Home Assistant's built-in SIA integration.
TIMEBAND_PAST: Final = 80
TIMEBAND_FUTURE: Final = 40

#: Drops an idle panel connection after five minutes so sockets cannot pile up.
TCP_IDLE_TIMEOUT: Final = 5 * 60

#: A re-sent message with the same account and sequence inside this window is
#: answered again but only logged once.
DUPLICATE_WINDOW_SECONDS: Final = 10

# --- Unknown account policies ------------------------------------------------

POLICY_DISCOVER: Final = "discover"
POLICY_IGNORE: Final = "ignore"
POLICY_AUTO_CREATE: Final = "auto_create"
UNKNOWN_ACCOUNT_POLICIES: Final = [POLICY_DISCOVER, POLICY_IGNORE, POLICY_AUTO_CREATE]
DEFAULT_UNKNOWN_ACCOUNT_POLICY: Final = POLICY_DISCOVER

# --- Dispatcher signals and bus events ---------------------------------------

#: Dispatcher signal and bus event type, formatted with the account number.
SIA_DC09_EVENT: Final = "sia_dc09_event_{}"
#: Catch-all bus event fired for every account.
SIA_DC09_EVENT_ALL: Final = "sia_dc09_event"
#: Dispatcher signal for hub level statistics updates.
SIA_DC09_HUB_UPDATED: Final = "sia_dc09_hub_updated_{}"

# --- Entity keys -------------------------------------------------------------

KEY_ALARM: Final = "alarm"
KEY_STATUS: Final = "status"
KEY_LAST_HEARTBEAT: Final = "last_heartbeat"
KEY_LAST_ACTIVITY: Final = "last_activity"
KEY_CONNECTIVITY: Final = "connectivity"
KEY_POWER: Final = "power"
KEY_SMOKE: Final = "smoke"
KEY_MOISTURE: Final = "moisture"
KEY_MESSAGES: Final = "messages_received"
KEY_UNKNOWN_ACCOUNTS: Final = "unknown_accounts"

# --- Services ----------------------------------------------------------------

SERVICE_GET_ACTIVITY: Final = "get_activity"
SERVICE_CLEAR_ACTIVITY: Final = "clear_activity"
SERVICE_PURGE: Final = "purge"
SERVICE_SET_STATUS: Final = "set_status"
SERVICE_DECODE_MESSAGE: Final = "decode_message"

# --- Event payload attributes ------------------------------------------------

ATTR_ACCOUNT: Final = "account"
ATTR_CODE: Final = "code"
ATTR_INCLUDE_TESTS: Final = "include_tests"
ATTR_LIMIT: Final = "limit"
ATTR_MESSAGE: Final = "message"
ATTR_SEVERITY: Final = "severity"
ATTR_START: Final = "start"
ATTR_END: Final = "end"
ATTR_STATUS: Final = "status"
ATTR_ZONE: Final = "zone"
