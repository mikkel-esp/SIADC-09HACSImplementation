"""Data model for decoded DC-09 messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Dc09Protocol = Literal["SIA-DCS", "ADM-CID", "NULL", "OTHER"]
EventSeverity = Literal[
    "alarm", "trouble", "supervisory", "status", "test", "unknown"
]
ResponseKind = Literal["ACK", "NAK", "DUH", "NONE"]
Transport = Literal["udp", "tcp"]


@dataclass(frozen=True, slots=True)
class Dc09Validation:
    """A field the receiver recomputes and compares against the wire value."""

    received: str
    calculated: str
    valid: bool


@dataclass(frozen=True, slots=True)
class Dc09ExtendedData:
    """One ``[<id><value>]`` extended data block."""

    id: str
    value: str

    @property
    def name(self) -> str | None:
        """Human readable meaning of the identifier, when known."""
        from .constants import EXTENDED_DATA_MEANINGS

        return EXTENDED_DATA_MEANINGS.get(self.id)


@dataclass(frozen=True, slots=True)
class Dc09Frame:
    """The framing layer of a DC-09 message."""

    #: Token exactly as it appeared on the wire, e.g. ``*SIA-DCS``.
    token: str
    #: Token with the encryption marker removed, e.g. ``SIA-DCS``.
    protocol_token: str
    protocol: Dc09Protocol
    encrypted: bool
    sequence: str
    line_prefix: str
    account: str
    #: Contents of the ``[...]`` block, decrypted when the message was encrypted.
    body: str
    crc: Dc09Validation
    length: Dc09Validation
    #: The portion the CRC and length are computed over.
    message: str
    receiver: str | None = None
    extended_data: tuple[Dc09ExtendedData, ...] = ()
    #: ``HH:MM:SS,MM-DD-YYYY`` as transmitted (always UTC per DC-09).
    timestamp: str | None = None
    timestamp_utc: datetime | None = None
    #: Difference between the message timestamp and receive time, in seconds.
    timestamp_drift_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class AlarmEvent:
    """A single event reported inside a message body."""

    #: SIA data code (``BA``) or Contact ID event code (``130``).
    code: str
    #: The token this event was parsed from.
    source: str
    #: SIA modifier letter (``N``/``R``) or Contact ID qualifier digit.
    qualifier: str | None = None
    qualifier_meaning: str | None = None
    #: Zone, point, user or condition number carried with the event.
    address: str | None = None
    area: str | None = None
    #: User / identification number from the ``id`` modifier.
    user: str | None = None
    #: Partition mask from the ``pm`` modifier.
    partition: str | None = None
    #: Free text descriptor (``^...^`` in SIA).
    text: str | None = None


@dataclass(frozen=True, slots=True)
class SiaBlock:
    """One ``/``-separated block of a SIA body, for the decoded view."""

    raw: str
    kind: Literal["event", "modifier", "unknown"]
    code: str
    value: str
    meaning: str | None = None


@dataclass(frozen=True, slots=True)
class DecodedPayload:
    """Protocol specific contents of a message body."""

    protocol: Dc09Protocol
    events: tuple[AlarmEvent, ...] = ()
    blocks: tuple[SiaBlock, ...] = ()
    account: str | None = None
    #: Set for ``NULL`` link-test messages.
    link_test: bool = False


@dataclass(frozen=True, slots=True)
class DecodeResult:
    """Outcome of decoding one datagram."""

    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    frame: Dc09Frame | None = None
    payload: DecodedPayload | None = None


@dataclass(frozen=True, slots=True)
class EnrichedEvent:
    """An :class:`AlarmEvent` joined to the SIA / Contact ID code tables."""

    event: AlarmEvent
    known: bool
    severity: EventSeverity
    category: str
    title: str | None = None
    description: str | None = None
    #: What the address field represents for this code, e.g. ``Zone or point``.
    address_meaning: str | None = None
    #: Human sentence, e.g. ``Zone or point 12``.
    address_label: str | None = None

    @property
    def code(self) -> str:
        """The SIA or Contact ID code of the underlying event."""
        return self.event.code


@dataclass(frozen=True, slots=True)
class EnrichedMessage:
    """A decoded message rendered the way an operator wants to read it."""

    protocol: Dc09Protocol
    link_test: bool
    summary: str
    events: tuple[EnrichedEvent, ...] = ()
    account: str | None = None
    receiver: str | None = None
    line_prefix: str | None = None
    sequence: str | None = None
    timestamp_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class Dc09Response:
    """A reply the receiver sent, or tried to send, back to the panel."""

    kind: ResponseKind
    ascii: str
    hex: str
    sent: bool
    error: str | None = None


@dataclass(slots=True)
class ReceivedMessage:
    """Everything known about one datagram that arrived on a listener."""

    received_at: datetime
    transport: Transport
    local_port: int
    remote_ip: str
    remote_port: int
    raw: bytes
    decode: DecodeResult
    enriched: EnrichedMessage | None = None
    response: Dc09Response | None = None
    extras: dict[str, object] = field(default_factory=dict)

    @property
    def account(self) -> str | None:
        """Account the message claims to come from, if it parsed at all."""
        if self.decode.frame is None:
            return None
        return self.decode.frame.account
