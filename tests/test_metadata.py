"""Checks that the shipped metadata stays consistent with the code."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from custom_components.sia_dc09 import config_flow
from custom_components.sia_dc09.const import (
    DOMAIN,
    SERVICE_CLEAR_ACTIVITY,
    SERVICE_DECODE_MESSAGE,
    SERVICE_GET_ACTIVITY,
    SERVICE_PURGE,
    SERVICE_SET_STATUS,
    UNKNOWN_ACCOUNT_POLICIES,
)
from custom_components.sia_dc09.state_machine import SiaStatus

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "sia_dc09"
SERVICES = {
    SERVICE_GET_ACTIVITY,
    SERVICE_CLEAR_ACTIVITY,
    SERVICE_PURGE,
    SERVICE_SET_STATUS,
    SERVICE_DECODE_MESSAGE,
}


def load(name: str) -> dict:
    """Read one of the component's JSON files."""
    return json.loads((COMPONENT / name).read_text(encoding="utf-8"))


def test_manifest_matches_the_domain() -> None:
    """The manifest must name the same domain the code uses."""
    manifest = load("manifest.json")
    assert manifest["domain"] == DOMAIN
    assert manifest["config_flow"] is True
    # cryptography is pinned by Home Assistant core, so declaring it would
    # risk fighting the pin.
    assert manifest["requirements"] == []


def test_translations_match_strings() -> None:
    """en.json must stay a copy of strings.json."""
    assert load("translations/en.json") == load("strings.json")


def test_every_config_step_is_translated() -> None:
    """Each flow step the code can show has translations."""
    strings = load("strings.json")

    config_steps = {"user", "account", "add_another"}
    assert config_steps <= set(strings["config"]["step"])

    option_steps = {
        "init",
        "receiver",
        "add_account",
        "edit_account",
        "remove_account",
    }
    assert option_steps <= set(strings["options"]["step"])


def test_every_error_is_translated() -> None:
    """Each error the validators can return has a message."""
    errors = {
        "invalid_host",
        "no_transport",
        "invalid_account",
        "duplicate_account",
        "invalid_key",
    }
    strings = load("strings.json")
    assert errors <= set(strings["config"]["error"])
    assert errors <= set(strings["options"]["error"])
    assert "no_accounts" in strings["options"]["abort"]


def test_receiver_fields_are_translated() -> None:
    """Every field in the receiver schema is labelled."""
    strings = load("strings.json")
    fields = {str(key.schema) for key in config_flow.receiver_schema().schema}
    assert fields == set(strings["config"]["step"]["user"]["data"])
    assert fields == set(strings["options"]["step"]["receiver"]["data"])


def test_account_fields_are_translated() -> None:
    """Every field in the account schema is labelled, in all three steps."""
    strings = load("strings.json")
    fields = {str(key.schema) for key in config_flow.account_schema().schema}
    for step, section in (
        ("account", "config"),
        ("add_account", "options"),
        ("edit_account", "options"),
    ):
        assert fields == set(strings[section]["step"][step]["data"]), step


def test_unknown_account_policies_are_translated() -> None:
    """Each selectable policy has a label."""
    strings = load("strings.json")
    options = strings["selector"]["unknown_account_policy"]["options"]
    assert set(UNKNOWN_ACCOUNT_POLICIES) == set(options)


def test_statuses_are_translated() -> None:
    """Every status the sensor can report has a label."""
    strings = load("strings.json")
    states = strings["entity"]["sensor"]["status"]["state"]
    assert {status.value for status in SiaStatus} == set(states)


def test_services_yaml_matches_the_registered_services() -> None:
    """services.yaml, strings.json and the code agree on the service list."""
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))
    strings = load("strings.json")
    assert set(services) == SERVICES
    assert set(strings["services"]) == SERVICES


def test_service_fields_are_documented() -> None:
    """Each field in services.yaml has a description."""
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))
    strings = load("strings.json")
    for name, definition in services.items():
        fields = set((definition or {}).get("fields") or {})
        documented = set(strings["services"][name].get("fields", {}))
        assert fields == documented, name


def test_set_status_offers_every_status() -> None:
    """The set_status selector lists exactly the statuses the code accepts."""
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))
    options = services[SERVICE_SET_STATUS]["fields"]["status"]["selector"]["select"][
        "options"
    ]
    assert set(options) == {status.value for status in SiaStatus}
