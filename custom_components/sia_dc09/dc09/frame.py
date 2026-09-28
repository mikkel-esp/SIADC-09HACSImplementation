"""DC-09 framing: parsing and building the outer message envelope."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from .bytes_util import WIRE_ENCODING, bytes_to_wire, wire_to_bytes
from .constants import CR, LF
from .crc import crc16_hex
from .crypto import Dc09CryptoError, decrypt_body
from .models import Dc09ExtendedData, Dc09Frame, Dc09Protocol, Dc09Validation
from .timestamp import parse_timestamp

HEX4 = re.compile(r"^[0-9a-fA-F]{4}$")
SEQUENCE = re.compile(r"^\d{4}$")

KNOWN_PROTOCOLS: dict[str, Dc09Protocol] = {
    "SIA-DCS": "SIA-DCS",
    "ADM-CID": "ADM-CID",
    "NULL": "NULL",
}


class Dc09ParseError(Exception):
    """Raised when a datagram cannot be interpreted as a DC-09 message."""


@dataclass(frozen=True, slots=True)
class ParseFrameResult:
    """A parsed frame plus any non-fatal complaints about it."""

    frame: Dc09Frame
    warnings: tuple[str, ...]


def parse_frame(
    datagram: bytes,
    key: bytes | None = None,
    received_at: datetime | None = None,
) -> ParseFrameResult:
    r"""Parse a DC-09 datagram.

    The wire format is::

        <LF><CRC><0LLL>"<id>"<seq>[R<rcvr>]L<pref>#<acct>[<data>][<x-data>]_<ts><CR>

    The CRC and the length field both cover the portion from the opening quote
    of the id token up to (but not including) the terminating CR.
    """
    warnings: list[str] = []
    text = bytes_to_wire(datagram)

    if not text.startswith(LF):
        warnings.append("Message does not start with the required <LF> (0x0A).")
    else:
        text = text[1:]

    if not text.endswith(CR):
        warnings.append("Message does not end with the required <CR> (0x0D).")
    else:
        text = text[:-1]

    if len(text) < 9:
        raise Dc09ParseError(
            "Message is too short to contain a CRC, length field and id token."
        )

    crc_field = text[0:4]
    length_field = text[4:8]
    message = text[8:]

    if not HEX4.match(crc_field):
        raise Dc09ParseError(f'CRC field "{crc_field}" is not 4 hex digits.')
    if not HEX4.match(length_field):
        raise Dc09ParseError(f'Length field "{length_field}" is not 4 hex digits.')

    calculated_crc = crc16_hex(wire_to_bytes(message))
    declared_length = int(length_field, 16)
    actual_length = len(message.encode(WIRE_ENCODING))

    header = _parse_header(message)
    encrypted = header.token.startswith("*")
    protocol_token = header.token[1:] if encrypted else header.token

    tail = header.after_bracket
    if encrypted:
        if key is None:
            raise Dc09ParseError("Message is encrypted but no AES key is configured.")
        try:
            tail = decrypt_body(header.after_bracket, key)
        except Dc09CryptoError as err:
            raise Dc09ParseError(str(err)) from err

    parsed = _parse_tail(tail)
    if parsed.trailing:
        warnings.append(
            f'Unexpected trailing characters after the message: "{parsed.trailing}".'
        )

    timestamp_utc = parse_timestamp(parsed.timestamp) if parsed.timestamp else None
    if parsed.timestamp and timestamp_utc is None:
        warnings.append(
            f'Timestamp "{parsed.timestamp}" is not in HH:MM:SS,MM-DD-YYYY format.'
        )

    arrival = received_at or datetime.now(UTC)
    drift = (
        round((arrival - timestamp_utc).total_seconds())
        if timestamp_utc is not None
        else None
    )

    frame = Dc09Frame(
        token=header.token,
        protocol_token=protocol_token,
        protocol=KNOWN_PROTOCOLS.get(protocol_token, "OTHER"),
        encrypted=encrypted,
        sequence=header.sequence,
        receiver=header.receiver,
        line_prefix=header.line_prefix,
        account=header.account,
        body=parsed.body,
        extended_data=parsed.extended_data,
        timestamp=parsed.timestamp,
        timestamp_utc=timestamp_utc,
        timestamp_drift_seconds=drift,
        crc=Dc09Validation(
            received=crc_field.upper(),
            calculated=calculated_crc,
            valid=crc_field.upper() == calculated_crc,
        ),
        length=Dc09Validation(
            received=length_field.upper(),
            calculated=f"{actual_length:04X}",
            valid=declared_length == actual_length,
        ),
        message=message,
    )

    return ParseFrameResult(frame=frame, warnings=tuple(warnings))


@dataclass(frozen=True, slots=True)
class Dc09Header:
    """The cleartext header of a DC-09 message.

    Readable without an AES key, because DC-09 leaves the header in the clear
    even for encrypted messages. That is what makes per-account keys possible:
    the receiver learns which account is talking before it has to decrypt.
    """

    token: str
    protocol_token: str
    encrypted: bool
    sequence: str
    receiver: str | None
    line_prefix: str
    account: str


def peek_header(datagram: bytes) -> Dc09Header | None:
    """Read a message's header without decrypting or validating it.

    Returns ``None`` when the datagram is too malformed to yield a header.
    """
    text = bytes_to_wire(datagram)
    if text.startswith(LF):
        text = text[1:]
    if text.endswith(CR):
        text = text[:-1]
    if len(text) < 9:
        return None

    try:
        header = _parse_header(text[8:])
    except Dc09ParseError:
        return None

    encrypted = header.token.startswith("*")
    return Dc09Header(
        token=header.token,
        protocol_token=header.token[1:] if encrypted else header.token,
        encrypted=encrypted,
        sequence=header.sequence,
        receiver=header.receiver,
        line_prefix=header.line_prefix,
        account=header.account,
    )


@dataclass(frozen=True, slots=True)
class _ParsedHeader:
    token: str
    sequence: str
    receiver: str | None
    line_prefix: str
    account: str
    after_bracket: str


def _parse_header(message: str) -> _ParsedHeader:
    if not message.startswith('"'):
        raise Dc09ParseError("Message does not start with a quoted id token.")
    closing_quote = message.find('"', 1)
    if closing_quote < 0:
        raise Dc09ParseError("The id token is not terminated by a closing quote.")

    token = message[1:closing_quote]
    if not token:
        raise Dc09ParseError("The id token is empty.")

    cursor = closing_quote + 1
    sequence = message[cursor : cursor + 4]
    if not SEQUENCE.match(sequence):
        raise Dc09ParseError(f'Sequence "{sequence}" is not 4 decimal digits.')
    cursor += 4

    receiver: str | None = None
    if cursor < len(message) and message[cursor] == "R":
        prefix_index = message.find("L", cursor)
        if prefix_index < 0:
            raise Dc09ParseError(
                "Receiver field is not followed by an L prefix field."
            )
        receiver = message[cursor + 1 : prefix_index]
        cursor = prefix_index

    if cursor >= len(message) or message[cursor] != "L":
        raise Dc09ParseError("Message is missing the mandatory L prefix field.")
    cursor += 1

    bracket_index = message.find("[", cursor)
    if bracket_index < 0:
        raise Dc09ParseError("Message is missing the `[` data block.")

    line_prefix, account = _split_account(message[cursor:bracket_index])

    return _ParsedHeader(
        token=token,
        sequence=sequence,
        receiver=receiver,
        line_prefix=line_prefix,
        account=account,
        after_bracket=message[bracket_index + 1 :],
    )


def _split_account(segment: str) -> tuple[str, str]:
    """Split the text between ``L`` and ``[`` into line prefix and account.

    The account is normally introduced by ``#``; NAK responses use ``A``
    instead (the DC-09 static NAK payload is ``R0L0A0[]``).
    """
    hash_index = segment.find("#")
    if hash_index >= 0:
        return segment[:hash_index], segment[hash_index + 1 :]

    marker = segment.rfind("A")
    if marker >= 0:
        return segment[:marker], segment[marker + 1 :]

    return segment, ""


@dataclass(frozen=True, slots=True)
class _ParsedTail:
    body: str
    extended_data: tuple[Dc09ExtendedData, ...]
    timestamp: str | None
    trailing: str | None


def _parse_tail(tail: str) -> _ParsedTail:
    """Parse everything that follows the opening ``[`` of the data block.

    For encrypted messages this is the decrypted plaintext, which has exactly
    the same shape.
    """
    close = tail.find("]")
    if close < 0:
        raise Dc09ParseError("The data block is not terminated by `]`.")

    body = tail[:close]
    extended: list[Dc09ExtendedData] = []
    cursor = close + 1

    while cursor < len(tail) and tail[cursor] == "[":
        end = tail.find("]", cursor + 1)
        if end < 0:
            raise Dc09ParseError("An extended data block is not terminated by `]`.")
        content = tail[cursor + 1 : end]
        extended.append(Dc09ExtendedData(id=content[:1], value=content[1:]))
        cursor = end + 1

    timestamp: str | None = None
    trailing: str | None = None
    rest = tail[cursor:]
    if rest.startswith("_"):
        timestamp = rest[1:]
    elif rest:
        trailing = rest

    return _ParsedTail(
        body=body,
        extended_data=tuple(extended),
        timestamp=timestamp,
        trailing=trailing,
    )


def build_frame(message: str) -> bytes:
    """Wrap a message body in DC-09 framing, computing the CRC and length."""
    data = wire_to_bytes(message)
    if len(data) > 0xFFF:
        raise Dc09ParseError("Message exceeds the maximum DC-09 length of 0xFFF bytes.")
    return wire_to_bytes(f"{LF}{crc16_hex(data)}{len(data):04X}{message}{CR}")
