"""Shared fixtures and helpers for the test suite."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_socket

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ``pytest-homeassistant-custom-component`` calls ``pytest_socket.disable_socket``
# from its own ``pytest_runtest_setup`` hook, which runs after any conftest hook
# but before fixtures are built. That is too early to undo from a hook of our
# own, so the guard is neutralised outright. This integration is a network
# receiver whose tests bind real loopback sockets, and on Windows even
# constructing an asyncio event loop needs an AF_INET socketpair.
pytest_socket.disable_socket = lambda *args, **kwargs: None
pytest_socket.enable_socket()

from custom_components.sia_dc09.dc09 import (  # noqa: E402
    DecodeResult,
    decode,
    parse_key,
    wire_to_bytes,
)

#: The key used by the SIADC09Debugger fixtures.
TEST_KEY_HEX = "ABCDABCDABCDABCDABCDABCDABCDABCD"

#: Reference receive time used by the ported vectors.
RECEIVED_AT = datetime(2015, 6, 22, 13, 53, 11, tzinfo=UTC)


@pytest.fixture(name="key")
def key_fixture() -> bytes:
    """Return the AES key used by the ported fixtures."""
    parsed = parse_key(TEST_KEY_HEX)
    assert parsed is not None
    return parsed


def decode_text(
    text: str, key: bytes | None = None, received_at: datetime | None = None
) -> DecodeResult:
    """Decode a latin-1 message written as a Python string."""
    return decode(
        wire_to_bytes(text), key=key, received_at=received_at or RECEIVED_AT
    )
