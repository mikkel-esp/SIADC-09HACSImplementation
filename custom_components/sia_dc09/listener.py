"""UDP and TCP receivers for DC-09 alarm messages.

Deliberately free of Home Assistant imports so the network layer can be tested
on its own. The integration supplies two callables:

``key_for(account)``
    Returns the AES key configured for an account, or ``None``. Called with the
    account read from the *cleartext* header, which is why per-account keys work
    at all.

``on_message(message)``
    An awaitable invoked for every decoded message, including ones that failed
    to decode. It must not raise; anything it does raise is logged and dropped
    so one bad panel cannot take the receiver down.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from .dc09 import (
    MAX_FRAME_BYTES,
    Dc09Frame,
    Dc09Response,
    DecodeResult,
    ReceivedMessage,
    Transport,
    build_ack,
    build_duh,
    build_nak,
    decode,
    enrich,
    extract_frames,
    to_hex,
)

_LOGGER = logging.getLogger(__name__)

KeyResolver = Callable[[str], bytes | None]
MessageHandler = Callable[[ReceivedMessage], Awaitable[None]]
AccountFilter = Callable[[str], bool]


class ReceiverConfig:
    """Everything the receivers need that is not a socket."""

    def __init__(
        self,
        bind_host: str,
        udp_port: int | None,
        tcp_port: int | None,
        respond: bool,
        nak_on_bad_crc: bool,
        key_for: KeyResolver,
        on_message: MessageHandler,
        is_known_account: AccountFilter | None = None,
        tcp_idle_timeout: float = 300.0,
    ):
        """Store the receiver configuration."""
        self.bind_host = bind_host
        self.udp_port = udp_port
        self.tcp_port = tcp_port
        self.respond = respond
        self.nak_on_bad_crc = nak_on_bad_crc
        self.key_for = key_for
        self.on_message = on_message
        self.is_known_account = is_known_account or (lambda _account: True)
        self.tcp_idle_timeout = tcp_idle_timeout


def _decode_with_account_key(
    datagram: bytes, config: ReceiverConfig, received_at: datetime
) -> DecodeResult:
    """Decode a datagram, picking the key from the cleartext account header."""
    from .dc09 import peek_header

    header = peek_header(datagram)
    key = config.key_for(header.account) if header is not None else None
    return decode(datagram, key=key, received_at=received_at)


def _choose_response(
    result: DecodeResult, config: ReceiverConfig, now: datetime
) -> tuple[str, bytes] | None:
    """Decide what to send back, mirroring the DC-09 acknowledgement rules.

    * A message that cannot be framed at all gets nothing - there is no
      sequence number to echo, so a NAK would be meaningless to the panel.
    * A bad CRC or length gets a NAK, if the user asked for that, so the panel
      retransmits.
    * A well formed message for an account we do not serve gets a DUH
      ("Don't UnderstandHandle"), which tells the panel to stop retrying.
    * Everything else gets an ACK.
    """
    if not config.respond:
        return None

    frame: Dc09Frame | None = result.frame
    if frame is None:
        return None

    if not result.ok:
        if not config.nak_on_bad_crc:
            return None
        return ("NAK", build_nak(now))

    if not config.is_known_account(frame.account):
        return ("DUH", build_duh(frame, now))

    return ("ACK", build_ack(frame, now))


async def _handle_datagram(
    raw: bytes,
    config: ReceiverConfig,
    transport_kind: Transport,
    local_port: int,
    remote: tuple[str, int],
    send: Callable[[bytes], None],
) -> None:
    """Decode one frame, answer the panel, then hand the result upstream."""
    received_at = datetime.now(UTC)
    result = _decode_with_account_key(raw, config, received_at)

    response: Dc09Response | None = None
    chosen = _choose_response(result, config, received_at)
    if chosen is not None:
        kind, payload = chosen
        error: str | None = None
        sent = False
        try:
            send(payload)
            sent = True
        except OSError as err:  # pragma: no cover - transport specific
            error = str(err)
            _LOGGER.warning("Failed to send %s to %s: %s", kind, remote[0], err)
        response = Dc09Response(
            kind=kind,
            ascii=payload.decode("latin-1"),
            hex=to_hex(payload),
            sent=sent,
            error=error,
        )

    message = ReceivedMessage(
        received_at=received_at,
        transport=transport_kind,
        local_port=local_port,
        remote_ip=remote[0],
        remote_port=remote[1],
        raw=raw,
        decode=result,
        enriched=enrich(result),
        response=response,
    )

    try:
        await config.on_message(message)
    except Exception:
        _LOGGER.exception("Error handling message from %s", remote[0])


class Dc09DatagramProtocol(asyncio.DatagramProtocol):
    """Receives DC-09 messages over UDP."""

    def __init__(self, config: ReceiverConfig, port: int):
        """Store the configuration and the port being served."""
        self._config = config
        #: Overwritten with the real port once an ephemeral bind resolves.
        self.port = port
        self._transport: asyncio.DatagramTransport | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Remember the transport so replies can be sent."""
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle one datagram. UDP guarantees one message per datagram."""
        transport = self._transport
        if transport is None:  # pragma: no cover - defensive
            return

        task = asyncio.get_running_loop().create_task(
            _handle_datagram(
                data,
                self._config,
                "UDP",
                self.port,
                addr,
                lambda payload: transport.sendto(payload, addr),
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def error_received(self, exc: Exception) -> None:
        """Log transport errors. UDP errors are rarely fatal."""
        _LOGGER.debug("UDP error on port %s: %s", self.port, exc)


class Dc09Receiver:
    """Owns the UDP socket and the TCP server for one config entry."""

    def __init__(self, config: ReceiverConfig):
        """Store the configuration without opening any sockets."""
        self.config = config
        #: The port actually bound, which differs from the configured one when
        #: the caller asked for an ephemeral port.
        self.udp_port: int | None = None
        self.tcp_port: int | None = None
        self._udp_transport: asyncio.DatagramTransport | None = None
        self._tcp_server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()

    @property
    def is_running(self) -> bool:
        """Return whether at least one listener is open."""
        return self._udp_transport is not None or self._tcp_server is not None

    async def async_start(self) -> None:
        """Open the configured listeners.

        Partially started listeners are torn down before the error propagates,
        so a failure never leaves a stray socket bound.
        """
        loop = asyncio.get_running_loop()
        try:
            if self.config.udp_port is not None:
                transport, protocol = await loop.create_datagram_endpoint(
                    lambda: Dc09DatagramProtocol(self.config, self.config.udp_port),
                    local_addr=(self.config.bind_host, self.config.udp_port),
                )
                self._udp_transport = transport
                self.udp_port = int(transport.get_extra_info("sockname")[1])
                protocol.port = self.udp_port
                _LOGGER.debug(
                    "Listening for DC-09 over UDP on %s:%s",
                    self.config.bind_host,
                    self.udp_port,
                )

            if self.config.tcp_port is not None:
                self._tcp_server = await asyncio.start_server(
                    self._handle_connection,
                    host=self.config.bind_host,
                    port=self.config.tcp_port,
                )
                self.tcp_port = int(self._tcp_server.sockets[0].getsockname()[1])
                _LOGGER.debug(
                    "Listening for DC-09 over TCP on %s:%s",
                    self.config.bind_host,
                    self.tcp_port,
                )
        except OSError:
            await self.async_stop()
            raise

    async def async_stop(self) -> None:
        """Close the listeners and drop any open panel connections."""
        if self._udp_transport is not None:
            self._udp_transport.close()
            self._udp_transport = None

        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None

        for task in list(self._connections):
            task.cancel()
        if self._connections:
            await asyncio.gather(*self._connections, return_exceptions=True)
        self._connections.clear()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve one TCP connection until it goes idle or the panel hangs up."""
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)

        peer = writer.get_extra_info("peername") or ("unknown", 0)
        remote = (str(peer[0]), int(peer[1]) if len(peer) > 1 else 0)
        buffer = b""

        def send(payload: bytes) -> None:
            writer.write(payload)

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(4096), timeout=self.config.tcp_idle_timeout
                    )
                except TimeoutError:
                    _LOGGER.debug("Closing idle DC-09 connection from %s", remote[0])
                    break

                if not chunk:
                    break

                buffer += chunk
                extraction = extract_frames(buffer, MAX_FRAME_BYTES)
                buffer = extraction.rest
                if extraction.discarded:
                    _LOGGER.debug(
                        "Discarded %d unframed bytes from %s",
                        extraction.discarded,
                        remote[0],
                    )

                for frame_bytes in extraction.frames:
                    await _handle_datagram(
                        frame_bytes,
                        self.config,
                        "TCP",
                        self.tcp_port or 0,
                        remote,
                        send,
                    )
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            _LOGGER.debug("DC-09 connection from %s reset", remote[0])
        except asyncio.CancelledError:
            raise
        finally:
            if task is not None:
                self._connections.discard(task)
            writer.close()
            with contextlib.suppress(OSError, asyncio.CancelledError):
                await writer.wait_closed()
