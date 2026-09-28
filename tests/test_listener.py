"""Tests for the UDP and TCP receivers, exercised over real loopback sockets."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from custom_components.sia_dc09.dc09 import (
    ReceivedMessage,
    build_frame,
    encrypt_body,
    parse_key,
    wire_to_bytes,
)
from custom_components.sia_dc09.listener import Dc09Receiver, ReceiverConfig

ACCOUNT = "1234"
OTHER_ACCOUNT = "9999"
KEY_HEX = "ABCDABCDABCDABCDABCDABCDABCDABCD"


def sia_body(account: str = ACCOUNT, sequence: str = "0001", code: str = "BA") -> str:
    """Return a plaintext SIA-DCS message body."""
    return f'"SIA-DCS"{sequence}R0L0#{account}[|Nri1/{code}001]'


class Collector:
    """Captures messages handed up by the receiver."""

    def __init__(self) -> None:
        """Start with no messages and nobody waiting."""
        self.messages: list[ReceivedMessage] = []
        self._event = asyncio.Event()

    async def __call__(self, message: ReceivedMessage) -> None:
        """Record a message and wake anyone waiting for one."""
        self.messages.append(message)
        self._event.set()

    async def wait(self, count: int = 1, timeout: float = 5.0) -> None:
        """Block until at least ``count`` messages have arrived."""
        async with asyncio.timeout(timeout):
            while len(self.messages) < count:
                self._event.clear()
                await self._event.wait()


@contextlib.asynccontextmanager
async def running_receiver(**overrides):
    """Start a receiver on ephemeral ports and stop it afterwards."""
    collector = overrides.pop("collector", None) or Collector()
    config = ReceiverConfig(
        bind_host="127.0.0.1",
        udp_port=overrides.pop("udp_port", 0),
        tcp_port=overrides.pop("tcp_port", 0),
        respond=overrides.pop("respond", True),
        nak_on_bad_crc=overrides.pop("nak_on_bad_crc", True),
        key_for=overrides.pop("key_for", lambda _account: None),
        on_message=collector,
        is_known_account=overrides.pop("is_known_account", None),
        **overrides,
    )
    receiver = Dc09Receiver(config)
    await receiver.async_start()
    try:
        yield receiver, collector
    finally:
        await receiver.async_stop()


def udp_port_of(receiver: Dc09Receiver) -> int:
    """Return the port the UDP socket actually bound to."""
    assert receiver.udp_port is not None
    return receiver.udp_port


def tcp_port_of(receiver: Dc09Receiver) -> int:
    """Return the port the TCP server actually bound to."""
    assert receiver.tcp_port is not None
    return receiver.tcp_port


async def send_udp(port: int, payload: bytes, expect_reply: bool = True) -> bytes:
    """Send one datagram and optionally wait for the receiver's reply."""
    loop = asyncio.get_running_loop()
    replies: asyncio.Queue[bytes] = asyncio.Queue()

    class Client(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, _addr) -> None:
            replies.put_nowait(data)

    transport, _protocol = await loop.create_datagram_endpoint(
        Client, remote_addr=("127.0.0.1", port)
    )
    try:
        transport.sendto(payload)
        if not expect_reply:
            return b""
        async with asyncio.timeout(5):
            return await replies.get()
    finally:
        transport.close()


async def test_udp_message_is_decoded_and_acked() -> None:
    async with running_receiver(udp_port=0, tcp_port=None) as (receiver, collector):
        reply = await send_udp(udp_port_of(receiver), build_frame(sia_body()))
        await collector.wait()

    assert b'"ACK"' in reply
    message = collector.messages[0]
    assert message.transport == "UDP"
    assert message.decode.ok
    assert message.decode.frame.account == ACCOUNT
    assert message.response.kind == "ACK"
    assert message.response.sent


async def test_ack_echoes_the_sequence_and_account() -> None:
    async with running_receiver(udp_port=0, tcp_port=None) as (receiver, collector):
        reply = await send_udp(
            udp_port_of(receiver), build_frame(sia_body(sequence="0042"))
        )
        await collector.wait()

    text = reply.decode("latin-1")
    assert "0042" in text
    assert f"#{ACCOUNT}" in text


async def test_bad_crc_is_naked() -> None:
    framed = bytearray(build_frame(sia_body()))
    framed[1] = ord("0") if framed[1] != ord("0") else ord("1")

    async with running_receiver(udp_port=0, tcp_port=None) as (receiver, collector):
        reply = await send_udp(udp_port_of(receiver), bytes(framed))
        await collector.wait()

    assert b'"NAK"' in reply
    assert not collector.messages[0].decode.ok
    assert collector.messages[0].response.kind == "NAK"


async def test_bad_crc_is_silent_when_nak_is_disabled() -> None:
    framed = bytearray(build_frame(sia_body()))
    framed[1] = ord("0") if framed[1] != ord("0") else ord("1")

    async with running_receiver(
        udp_port=0, tcp_port=None, nak_on_bad_crc=False
    ) as (receiver, collector):
        await send_udp(udp_port_of(receiver), bytes(framed), expect_reply=False)
        await collector.wait()

    assert collector.messages[0].response is None


async def test_unknown_account_gets_duh() -> None:
    async with running_receiver(
        udp_port=0, tcp_port=None, is_known_account=lambda account: account == ACCOUNT
    ) as (receiver, collector):
        reply = await send_udp(
            udp_port_of(receiver), build_frame(sia_body(account=OTHER_ACCOUNT))
        )
        await collector.wait()

    assert b'"DUH"' in reply
    assert collector.messages[0].response.kind == "DUH"


async def test_responses_can_be_switched_off_entirely() -> None:
    async with running_receiver(udp_port=0, tcp_port=None, respond=False) as (
        receiver,
        collector,
    ):
        await send_udp(
            udp_port_of(receiver), build_frame(sia_body()), expect_reply=False
        )
        await collector.wait()

    assert collector.messages[0].response is None


async def test_unframeable_garbage_is_not_answered() -> None:
    async with running_receiver(udp_port=0, tcp_port=None) as (receiver, collector):
        await send_udp(
            udp_port_of(receiver), b"not a dc-09 message", expect_reply=False
        )
        await collector.wait()

    message = collector.messages[0]
    assert not message.decode.ok
    assert message.decode.frame is None
    assert message.response is None


async def test_per_account_key_is_chosen_from_the_cleartext_header() -> None:
    """The account is readable before decryption, which is the whole point."""
    key = parse_key(KEY_HEX)
    assert key is not None

    encrypted_tail = encrypt_body("|Nri1/BA001]_10:00:00,01-01-2024", key)
    body = f'"*SIA-DCS"0001R0L0#{ACCOUNT}[{encrypted_tail}'

    keys = {ACCOUNT: key}
    async with running_receiver(
        udp_port=0, tcp_port=None, key_for=keys.get
    ) as (receiver, collector):
        await send_udp(udp_port_of(receiver), build_frame(body))
        await collector.wait()

    message = collector.messages[0]
    assert message.decode.ok, message.decode.errors
    assert message.decode.frame.encrypted
    assert message.decode.payload.events[0].code == "BA"


async def test_encrypted_message_without_a_key_fails_cleanly() -> None:
    key = parse_key(KEY_HEX)
    assert key is not None
    body = f'"*SIA-DCS"0001R0L0#{ACCOUNT}[{encrypt_body("|Nri1/BA001]", key)}'

    async with running_receiver(udp_port=0, tcp_port=None) as (receiver, collector):
        await send_udp(udp_port_of(receiver), build_frame(body), expect_reply=False)
        await collector.wait()

    assert not collector.messages[0].decode.ok


async def test_tcp_message_is_decoded_and_acked() -> None:
    async with running_receiver(udp_port=None, tcp_port=0) as (receiver, collector):
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", tcp_port_of(receiver)
        )
        writer.write(build_frame(sia_body()))
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(256), timeout=5)
        await collector.wait()
        writer.close()
        await writer.wait_closed()

    assert b'"ACK"' in reply
    assert collector.messages[0].transport == "TCP"


async def test_tcp_reassembles_split_and_batched_frames() -> None:
    """Frames are self-delimiting, so arbitrary chunking must still work."""
    payload = b"".join(build_frame(sia_body(sequence=f"{i:04d}")) for i in range(3))

    async with running_receiver(udp_port=None, tcp_port=0) as (receiver, collector):
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", tcp_port_of(receiver)
        )
        # Split the stream mid-frame to prove the reassembly buffer works.
        writer.write(payload[:17])
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.write(payload[17:])
        await writer.drain()
        await collector.wait(count=3)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    assert [m.decode.frame.sequence for m in collector.messages] == [
        "0000",
        "0001",
        "0002",
    ]


async def test_tcp_leading_noise_is_discarded() -> None:
    async with running_receiver(udp_port=None, tcp_port=0) as (receiver, collector):
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", tcp_port_of(receiver)
        )
        writer.write(wire_to_bytes("junk") + build_frame(sia_body()))
        await writer.drain()
        await collector.wait()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    assert collector.messages[0].decode.ok


async def test_idle_tcp_connection_is_dropped() -> None:
    async with running_receiver(
        udp_port=None, tcp_port=0, tcp_idle_timeout=0.2
    ) as (receiver, _collector):
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", tcp_port_of(receiver)
        )
        # The receiver should close on us without us sending anything.
        assert await asyncio.wait_for(reader.read(1), timeout=5) == b""
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def test_a_failing_handler_does_not_kill_the_receiver() -> None:
    seen: list[str] = []

    async def handler(message: ReceivedMessage) -> None:
        seen.append(message.decode.frame.sequence)
        raise RuntimeError("handler exploded")

    config = ReceiverConfig(
        bind_host="127.0.0.1",
        udp_port=0,
        tcp_port=None,
        respond=True,
        nak_on_bad_crc=True,
        key_for=lambda _account: None,
        on_message=handler,
    )
    receiver = Dc09Receiver(config)
    await receiver.async_start()
    try:
        port = udp_port_of(receiver)
        await send_udp(port, build_frame(sia_body(sequence="0001")))
        await send_udp(port, build_frame(sia_body(sequence="0002")))
        async with asyncio.timeout(5):
            while len(seen) < 2:
                await asyncio.sleep(0.01)
    finally:
        await receiver.async_stop()

    assert seen == ["0001", "0002"]


async def test_start_and_stop_are_idempotent() -> None:
    async with running_receiver(udp_port=0, tcp_port=0) as (receiver, _collector):
        assert receiver.is_running

    assert not receiver.is_running
    await receiver.async_stop()


async def test_port_conflict_releases_the_other_listener() -> None:
    """A half-open receiver must not leave a stray socket bound."""
    async with running_receiver(udp_port=None, tcp_port=0) as (receiver, _collector):
        taken = tcp_port_of(receiver)

        clash = Dc09Receiver(
            ReceiverConfig(
                bind_host="127.0.0.1",
                udp_port=0,
                tcp_port=taken,
                respond=True,
                nak_on_bad_crc=True,
                key_for=lambda _account: None,
                on_message=Collector(),
            )
        )
        with pytest.raises(OSError):
            await clash.async_start()
        assert not clash.is_running
