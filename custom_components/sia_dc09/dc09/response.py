"""Builders for the DC-09 receiver responses: ACK, NAK and DUH."""

from __future__ import annotations

from datetime import datetime

from .frame import build_frame
from .models import Dc09Frame
from .timestamp import format_timestamp


def _header(
    token: str,
    sequence: str,
    receiver: str | None,
    line_prefix: str,
    account: str,
) -> str:
    receiver_field = f"R{receiver}" if receiver is not None else ""
    account_field = f"#{account}" if account else ""
    return f'"{token}"{sequence}{receiver_field}L{line_prefix}{account_field}[]'


def build_ack(frame: Dc09Frame, now: datetime | None = None) -> bytes:
    """Build the positive acknowledgement for a received message.

    The receiver echoes the sequence, receiver number, line prefix and account,
    and returns its own current UTC time so the panel can synchronise its clock.
    """
    base = _header(
        "ACK", frame.sequence, frame.receiver, frame.line_prefix, frame.account
    )
    return build_frame(f"{base}_{format_timestamp(now)}" if frame.timestamp else base)


def build_nak(now: datetime | None = None) -> bytes:
    """Build a NAK.

    DC-09 defines a fixed NAK payload (``R0L0A0[]``) with sequence ``0000``; the
    timestamp lets the panel correct a clock that is out of range.
    """
    return build_frame(f'"NAK"0000R0L0A0[]_{format_timestamp(now)}')


def build_duh(frame: Dc09Frame, now: datetime | None = None) -> bytes:
    """Build a DUH ("Data Unintelligible Here") response.

    Used when the message was framed correctly but the receiver cannot act on
    its content.
    """
    base = _header(
        "DUH", frame.sequence, frame.receiver, frame.line_prefix, frame.account
    )
    return build_frame(f"{base}_{format_timestamp(now)}" if frame.timestamp else base)
