"""SIA DC-09 protocol implementation.

A dependency-free (beyond Home Assistant's own ``cryptography``) port of the
receiver logic from the SIADC09Debugger project.
"""

from __future__ import annotations

from datetime import datetime

from .bytes_util import (
    WIRE_ENCODING,
    bytes_to_wire,
    to_display_text,
    to_hex,
    to_printable,
    wire_to_bytes,
)
from .cid import parse_cid_payload
from .cid_codes import CID_CODES, CID_QUALIFIERS, cid_address_meaning, cid_category
from .constants import (
    CR,
    EXTENDED_DATA_MEANINGS,
    LF,
    MAX_FRAME_BYTES,
    SIA_MODIFIERS,
    SIA_QUALIFIERS,
    SIGNALLING_TOKENS,
)
from .crc import crc16, crc16_hex
from .crypto import Dc09CryptoError, decrypt_body, encrypt_body, parse_key
from .enrich import enrich
from .frame import Dc09Header, Dc09ParseError, build_frame, parse_frame, peek_header
from .models import (
    AlarmEvent,
    Dc09ExtendedData,
    Dc09Frame,
    Dc09Protocol,
    Dc09Response,
    Dc09Validation,
    DecodedPayload,
    DecodeResult,
    EnrichedEvent,
    EnrichedMessage,
    EventSeverity,
    ReceivedMessage,
    ResponseKind,
    SiaBlock,
    Transport,
)
from .response import build_ack, build_duh, build_nak
from .sia import parse_sia_payload
from .sia_codes import (
    SIA_CATEGORY_BY_LETTER,
    SiaCodeMeta,
    category_for,
    lookup_sia_code,
    severity_for,
    sia_codes,
)
from .stream import FrameExtraction, extract_frames
from .timestamp import format_timestamp, parse_timestamp

__all__ = [
    "CID_CODES",
    "CID_QUALIFIERS",
    "CR",
    "EXTENDED_DATA_MEANINGS",
    "LF",
    "MAX_FRAME_BYTES",
    "SIA_CATEGORY_BY_LETTER",
    "SIA_MODIFIERS",
    "SIA_QUALIFIERS",
    "SIGNALLING_TOKENS",
    "WIRE_ENCODING",
    "AlarmEvent",
    "Dc09CryptoError",
    "Dc09ExtendedData",
    "Dc09Frame",
    "Dc09Header",
    "Dc09ParseError",
    "Dc09Protocol",
    "Dc09Response",
    "Dc09Validation",
    "DecodeResult",
    "DecodedPayload",
    "EnrichedEvent",
    "EnrichedMessage",
    "EventSeverity",
    "FrameExtraction",
    "ReceivedMessage",
    "ResponseKind",
    "SiaBlock",
    "SiaCodeMeta",
    "Transport",
    "build_ack",
    "build_duh",
    "build_frame",
    "build_nak",
    "bytes_to_wire",
    "category_for",
    "cid_address_meaning",
    "cid_category",
    "crc16",
    "crc16_hex",
    "decode",
    "decrypt_body",
    "encrypt_body",
    "enrich",
    "extract_frames",
    "format_timestamp",
    "lookup_sia_code",
    "parse_cid_payload",
    "parse_frame",
    "parse_key",
    "parse_sia_payload",
    "parse_timestamp",
    "peek_header",
    "severity_for",
    "sia_codes",
    "to_display_text",
    "to_hex",
    "to_printable",
    "wire_to_bytes",
]


def decode(
    datagram: bytes,
    key: bytes | None = None,
    received_at: datetime | None = None,
    require_encryption: bool = False,
) -> DecodeResult:
    """Decode a raw DC-09 datagram into a frame plus its protocol payload.

    Set ``require_encryption`` to reject cleartext messages, which is what
    the receiver does for any account that has a key configured.
    """
    try:
        parsed = parse_frame(
            datagram,
            key=key,
            received_at=received_at,
        )
    except Dc09ParseError as err:
        return DecodeResult(ok=False, errors=(str(err),))
    except Exception as err:
        return DecodeResult(ok=False, errors=(f"Unexpected decoding failure: {err}",))

    frame = parsed.frame
    errors: list[str] = []

    if not frame.crc.valid:
        errors.append(
            f"CRC mismatch: message declares {frame.crc.received} "
            f"but the content hashes to {frame.crc.calculated}."
        )
    if not frame.length.valid:
        errors.append(
            f"Length mismatch: message declares 0x{frame.length.received} "
            f"but the content is 0x{frame.length.calculated} bytes."
        )
    if require_encryption and not frame.encrypted:
        # The account has a key configured, so cleartext cannot be trusted:
        # anyone able to reach the port could have forged it. The frame is
        # still returned so the caller can NAK it and log the account.
        errors.append("Message is not encrypted but this account requires encryption.")

    return DecodeResult(
        ok=not errors,
        errors=tuple(errors),
        warnings=parsed.warnings,
        frame=frame,
        payload=_decode_payload(frame.protocol_token, frame.body),
    )


def _decode_payload(token: str, body: str) -> DecodedPayload:
    if token == "SIA-DCS":
        return parse_sia_payload(body)
    if token == "ADM-CID":
        return parse_cid_payload(body)
    if token == "NULL":
        return DecodedPayload(protocol="NULL", link_test=True)
    if token in SIGNALLING_TOKENS:
        return DecodedPayload(protocol="OTHER")

    # Unknown tokens still carry a SIA style payload often enough to be worth trying.
    payload = parse_sia_payload(body)
    return DecodedPayload(
        protocol="OTHER",
        account=payload.account,
        events=payload.events,
        blocks=payload.blocks,
    )
