"""Config and options flow for the SIA DC-09 integration."""

from __future__ import annotations

import socket
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ACCOUNT,
    CONF_ACCOUNTS,
    CONF_ARM_AWAY_TARGET,
    CONF_ARM_HOME_TARGET,
    CONF_ARM_NIGHT_TARGET,
    CONF_BIND_HOST,
    CONF_DISARM_TARGET,
    CONF_ENCRYPTION_KEY,
    CONF_HEARTBEAT_TIMEOUT,
    CONF_IGNORE_TIMESTAMPS,
    CONF_NAK_ON_BAD_CRC,
    CONF_RECENT_EVENTS,
    CONF_RESPOND,
    CONF_RETENTION_MONTHS,
    CONF_TCP_PORT,
    CONF_UDP_PORT,
    CONF_UNKNOWN_ACCOUNT_POLICY,
    CONF_USERS,
    DEFAULT_BIND_HOST,
    DEFAULT_HEARTBEAT_TIMEOUT,
    DEFAULT_IGNORE_TIMESTAMPS,
    DEFAULT_NAK_ON_BAD_CRC,
    DEFAULT_RECENT_EVENTS,
    DEFAULT_RESPOND,
    DEFAULT_RETENTION_MONTHS,
    DEFAULT_TCP_PORT,
    DEFAULT_UDP_PORT,
    DEFAULT_UNKNOWN_ACCOUNT_POLICY,
    DOMAIN,
    UNKNOWN_ACCOUNT_POLICIES,
)
from .utils import (
    format_users,
    is_valid_account,
    is_valid_key,
    normalise_account,
    parse_users,
)

ARM_TARGET_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(
        domain=["script", "scene", "button", "input_button", "switch"]
    )
)

#: Panels report who armed or disarmed as a bare number, so the list of users
#: is free text: one ``number = name`` pair per line.
USER_LIST_SELECTOR = selector.TextSelector(selector.TextSelectorConfig(multiline=True))


def receiver_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return the schema for the receiver's own settings."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_BIND_HOST, default=defaults.get(CONF_BIND_HOST, DEFAULT_BIND_HOST)
            ): str,
            vol.Optional(
                CONF_UDP_PORT,
                default=defaults.get(CONF_UDP_PORT) or DEFAULT_UDP_PORT,
            ): vol.All(vol.Coerce(int), vol.Range(min=0, max=65535)),
            vol.Optional(
                CONF_TCP_PORT,
                default=defaults.get(CONF_TCP_PORT) or DEFAULT_TCP_PORT,
            ): vol.All(vol.Coerce(int), vol.Range(min=0, max=65535)),
            vol.Required(
                CONF_RESPOND, default=defaults.get(CONF_RESPOND, DEFAULT_RESPOND)
            ): bool,
            vol.Required(
                CONF_NAK_ON_BAD_CRC,
                default=defaults.get(CONF_NAK_ON_BAD_CRC, DEFAULT_NAK_ON_BAD_CRC),
            ): bool,
            vol.Required(
                CONF_UNKNOWN_ACCOUNT_POLICY,
                default=defaults.get(
                    CONF_UNKNOWN_ACCOUNT_POLICY, DEFAULT_UNKNOWN_ACCOUNT_POLICY
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=UNKNOWN_ACCOUNT_POLICIES,
                    translation_key="unknown_account_policy",
                )
            ),
            vol.Required(
                CONF_RETENTION_MONTHS,
                default=defaults.get(CONF_RETENTION_MONTHS, DEFAULT_RETENTION_MONTHS),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
            vol.Required(
                CONF_RECENT_EVENTS,
                default=defaults.get(CONF_RECENT_EVENTS, DEFAULT_RECENT_EVENTS),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=500)),
        }
    )


def _users_text(value: Any) -> str:
    """Return the user list as the text the form edits.

    Stored accounts hold a mapping, but a form redisplayed after a validation
    error holds whatever was typed, which must come back unaltered so the
    mistake is still visible.
    """
    if isinstance(value, Mapping):
        return format_users(value)
    return value if isinstance(value, str) else ""


def account_schema(
    defaults: dict[str, Any] | None = None, *, editing: bool = False
) -> vol.Schema:
    """Return the schema for adding or editing one account.

    The entity targets carry no default because an empty string is not a valid
    entity ID; ``add_suggested_values_to_schema`` prefills them instead.

    The timestamp option is offered only when editing. On the creation form the
    key has not been entered yet, so the checkbox would have to render its
    insecure default and would then be submitted as an explicit choice, leaving
    a new encrypted account open to replay. Leaving the field out lets
    ``clean_account`` derive it from the key that was actually supplied.
    """
    defaults = defaults or {}
    schema: dict[Any, Any] = {
        vol.Required(CONF_ACCOUNT, default=defaults.get(CONF_ACCOUNT, "")): str,
        vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): str,
        vol.Optional(
            CONF_ENCRYPTION_KEY, default=defaults.get(CONF_ENCRYPTION_KEY, "")
        ): str,
        vol.Required(
            CONF_HEARTBEAT_TIMEOUT,
            default=defaults.get(CONF_HEARTBEAT_TIMEOUT, DEFAULT_HEARTBEAT_TIMEOUT),
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=10080)),
        vol.Optional(
            CONF_USERS, default=_users_text(defaults.get(CONF_USERS))
        ): USER_LIST_SELECTOR,
    }
    if editing:
        schema[
            vol.Required(
                CONF_IGNORE_TIMESTAMPS,
                default=defaults.get(
                    CONF_IGNORE_TIMESTAMPS,
                    # An encrypted account enforces timestamps by default,
                    # because that is what makes replay protection possible.
                    False
                    if defaults.get(CONF_ENCRYPTION_KEY)
                    else DEFAULT_IGNORE_TIMESTAMPS,
                ),
            )
        ] = bool
    schema.update(
        {
            vol.Optional(CONF_ARM_AWAY_TARGET): ARM_TARGET_SELECTOR,
            vol.Optional(CONF_ARM_HOME_TARGET): ARM_TARGET_SELECTOR,
            vol.Optional(CONF_ARM_NIGHT_TARGET): ARM_TARGET_SELECTOR,
            vol.Optional(CONF_DISARM_TARGET): ARM_TARGET_SELECTOR,
        }
    )
    return vol.Schema(schema)


TARGET_KEYS = (
    CONF_ARM_AWAY_TARGET,
    CONF_ARM_HOME_TARGET,
    CONF_ARM_NIGHT_TARGET,
    CONF_DISARM_TARGET,
)


def clean_receiver(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise receiver input, turning a zero port into a disabled one."""
    cleaned = dict(data)
    cleaned[CONF_BIND_HOST] = cleaned[CONF_BIND_HOST].strip()
    for key in (CONF_UDP_PORT, CONF_TCP_PORT):
        if not cleaned.get(key):
            cleaned[key] = None
    return cleaned


def validate_receiver(data: dict[str, Any]) -> dict[str, str]:
    """Return per-field errors for the receiver settings."""
    errors: dict[str, str] = {}

    host = data[CONF_BIND_HOST].strip()
    try:
        socket.inet_pton(socket.AF_INET, host)
    except OSError:
        try:
            socket.inet_pton(socket.AF_INET6, host)
        except OSError:
            errors[CONF_BIND_HOST] = "invalid_host"

    if not data.get(CONF_UDP_PORT) and not data.get(CONF_TCP_PORT):
        # Neither transport enabled means the integration would do nothing.
        errors["base"] = "no_transport"

    return errors


def validate_account(
    data: dict[str, Any], existing: list[dict[str, Any]], editing: str | None = None
) -> dict[str, str]:
    """Return per-field errors for one account."""
    errors: dict[str, str] = {}

    account = normalise_account(data.get(CONF_ACCOUNT, ""))
    if not is_valid_account(account):
        errors[CONF_ACCOUNT] = "invalid_account"
    elif account != editing and any(
        normalise_account(item[CONF_ACCOUNT]) == account for item in existing
    ):
        errors[CONF_ACCOUNT] = "duplicate_account"

    if (key := (data.get(CONF_ENCRYPTION_KEY) or "").strip()) and not is_valid_key(key):
        errors[CONF_ENCRYPTION_KEY] = "invalid_key"

    users = data.get(CONF_USERS)
    if isinstance(users, str) and parse_users(users)[1]:
        errors[CONF_USERS] = "invalid_users"

    return errors


def _ignore_timestamps(
    data: dict[str, Any], key: str, previous: dict[str, Any] | None
) -> bool:
    """Decide whether an account should ignore message timestamps.

    Mirrors ``AccountConfig.from_dict``: an encrypted account enforces
    timestamps unless the user deliberately turns that off.

    Adding a key to an existing account is treated as a fresh decision. Any
    stored value predates the key, and a form submitted before the key existed
    cannot have been a considered choice about replay protection.
    """
    if key and not (previous or {}).get(CONF_ENCRYPTION_KEY):
        return False
    return data.get(CONF_IGNORE_TIMESTAMPS, False if key else DEFAULT_IGNORE_TIMESTAMPS)


def clean_account(
    data: dict[str, Any], previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Normalise an account submission into what gets stored.

    ``previous`` is the account as it was before an edit, which is what tells
    us whether a key has just been added.
    """
    account = normalise_account(data[CONF_ACCOUNT])
    key = (data.get(CONF_ENCRYPTION_KEY) or "").strip()
    cleaned: dict[str, Any] = {
        CONF_ACCOUNT: account,
        CONF_NAME: (data.get(CONF_NAME) or "").strip() or account,
        CONF_HEARTBEAT_TIMEOUT: data.get(
            CONF_HEARTBEAT_TIMEOUT, DEFAULT_HEARTBEAT_TIMEOUT
        ),
        CONF_IGNORE_TIMESTAMPS: _ignore_timestamps(data, key, previous),
    }
    if key:
        cleaned[CONF_ENCRYPTION_KEY] = key
    if users := parse_users(_users_text(data.get(CONF_USERS)))[0]:
        cleaned[CONF_USERS] = users
    for target in TARGET_KEYS:
        if value := (data.get(target) or "").strip():
            cleaned[target] = value
    return cleaned


class SiaDc09ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Walk the user through setting up a receiver and its first account."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with nothing configured."""
        self._receiver: dict[str, Any] = {}
        self._accounts: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the receiver's ports and behaviour."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = validate_receiver(user_input)
            if not errors:
                cleaned = clean_receiver(user_input)
                self._async_abort_entries_match(
                    {
                        CONF_UDP_PORT: cleaned[CONF_UDP_PORT],
                        CONF_TCP_PORT: cleaned[CONF_TCP_PORT],
                    }
                )
                self._receiver = cleaned
                return await self.async_step_account()

        return self.async_show_form(
            step_id="user",
            data_schema=receiver_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add an account, then offer to add another."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = validate_account(user_input, self._accounts)
            if not errors:
                self._accounts.append(clean_account(user_input))
                return await self.async_step_add_another()

        return self.async_show_form(
            step_id="account",
            data_schema=self.add_suggested_values_to_schema(
                account_schema(user_input or {}),
                {key: (user_input or {}).get(key) for key in TARGET_KEYS},
            ),
            errors=errors,
            description_placeholders={"count": str(len(self._accounts))},
        )

    async def async_step_add_another(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask whether there is another account to configure."""
        if user_input is not None:
            if user_input["add_another"]:
                return await self.async_step_account()
            return self._async_create()

        return self.async_show_form(
            step_id="add_another",
            data_schema=vol.Schema({vol.Required("add_another", default=False): bool}),
            description_placeholders={
                "accounts": ", ".join(item[CONF_ACCOUNT] for item in self._accounts)
            },
        )

    @callback
    def _async_create(self) -> ConfigFlowResult:
        """Create the config entry from what was collected."""
        ports = [
            str(port)
            for port in (
                self._receiver.get(CONF_UDP_PORT),
                self._receiver.get(CONF_TCP_PORT),
            )
            if port
        ]
        title = f"SIA DC-09 ({', '.join(sorted(set(ports)))})"
        return self.async_create_entry(
            title=title,
            data={**self._receiver, CONF_ACCOUNTS: self._accounts},
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> SiaDc09OptionsFlow:
        """Return the options flow."""
        return SiaDc09OptionsFlow()


class SiaDc09OptionsFlow(OptionsFlow):
    """Lets the user change receiver settings and manage accounts."""

    def __init__(self) -> None:
        """Start with no pending edit."""
        self._editing: str | None = None

    @property
    def _current(self) -> dict[str, Any]:
        """Return the entry's effective configuration."""
        return {**self.config_entry.data, **self.config_entry.options}

    @property
    def _accounts(self) -> list[dict[str, Any]]:
        """Return the configured accounts."""
        return list(self._current.get(CONF_ACCOUNTS, []))

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the top level choices."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["receiver", "add_account", "edit_account", "remove_account"],
        )

    async def async_step_receiver(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the receiver's ports and behaviour."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = validate_receiver(user_input)
            if not errors:
                return self._async_save({**self._current, **clean_receiver(user_input)})

        return self.async_show_form(
            step_id="receiver",
            data_schema=receiver_schema(user_input or self._current),
            errors=errors,
        )

    async def async_step_add_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add another account to an existing receiver."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = validate_account(user_input, self._accounts)
            if not errors:
                accounts = [*self._accounts, clean_account(user_input)]
                return self._async_save({**self._current, CONF_ACCOUNTS: accounts})

        return self.async_show_form(
            step_id="add_account",
            data_schema=self.add_suggested_values_to_schema(
                account_schema(user_input or {}),
                {key: (user_input or {}).get(key) for key in TARGET_KEYS},
            ),
            errors=errors,
        )

    async def async_step_edit_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick an account to edit, then edit it."""
        accounts = self._accounts
        if not accounts:
            return self.async_abort(reason="no_accounts")

        if self._editing is None:
            if user_input is not None:
                self._editing = normalise_account(user_input[CONF_ACCOUNT])
                return await self.async_step_edit_account()
            return self.async_show_form(
                step_id="edit_account",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_ACCOUNT): vol.In(
                            {
                                item[CONF_ACCOUNT]: f"{item[CONF_ACCOUNT]}"
                                f" ({item.get(CONF_NAME, item[CONF_ACCOUNT])})"
                                for item in accounts
                            }
                        )
                    }
                ),
            )

        existing = next(
            item
            for item in accounts
            if normalise_account(item[CONF_ACCOUNT]) == self._editing
        )

        if user_input is not None:
            errors = validate_account(user_input, accounts, editing=self._editing)
            if not errors:
                updated = [
                    clean_account(user_input, previous=existing)
                    if normalise_account(item[CONF_ACCOUNT]) == self._editing
                    else item
                    for item in accounts
                ]
                self._editing = None
                return self._async_save({**self._current, CONF_ACCOUNTS: updated})
            return self.async_show_form(
                step_id="edit_account",
                data_schema=self.add_suggested_values_to_schema(
                    account_schema(user_input, editing=True),
                    {key: user_input.get(key) for key in TARGET_KEYS},
                ),
                errors=errors,
            )

        return self.async_show_form(
            step_id="edit_account",
            data_schema=self.add_suggested_values_to_schema(
                account_schema(existing, editing=True),
                {key: existing.get(key) for key in TARGET_KEYS},
            ),
        )

    async def async_step_remove_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove one or more accounts."""
        accounts = self._accounts
        if not accounts:
            return self.async_abort(reason="no_accounts")

        if user_input is not None:
            removing = {
                normalise_account(value) for value in user_input.get(CONF_ACCOUNTS, [])
            }
            remaining = [
                item
                for item in accounts
                if normalise_account(item[CONF_ACCOUNT]) not in removing
            ]
            return self._async_save({**self._current, CONF_ACCOUNTS: remaining})

        return self.async_show_form(
            step_id="remove_account",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCOUNTS, default=[]): cv_multi_select(
                        {
                            item[CONF_ACCOUNT]: f"{item[CONF_ACCOUNT]}"
                            f" ({item.get(CONF_NAME, item[CONF_ACCOUNT])})"
                            for item in accounts
                        }
                    )
                }
            ),
        )

    @callback
    def _async_save(self, options: dict[str, Any]) -> ConfigFlowResult:
        """Write the options back, which reloads the entry."""
        return self.async_create_entry(title="", data=options)


def cv_multi_select(options: dict[str, str]):
    """Return a multi-select selector for a mapping of value to label."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=value, label=label)
                for value, label in options.items()
            ],
            multiple=True,
        )
    )
