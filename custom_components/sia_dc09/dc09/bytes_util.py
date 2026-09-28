"""Byte level helpers for the DC-09 wire format.

DC-09 messages are byte oriented: the CRC and the length field count bytes, not
Unicode code points. ``latin-1`` gives a lossless 1:1 byte <-> character mapping,
so all protocol level work is done in that encoding.
"""

from __future__ import annotations

WIRE_ENCODING = "latin-1"


def bytes_to_wire(data: bytes) -> str:
    """Decode raw bytes into the latin-1 string used for protocol parsing."""
    return bytes(data).decode(WIRE_ENCODING)


def wire_to_bytes(text: str) -> bytes:
    """Encode a protocol string back into raw bytes."""
    return text.encode(WIRE_ENCODING)


def to_hex(data: bytes) -> str:
    """Render bytes as space separated uppercase hex, e.g. ``0A 32 37``."""
    return " ".join(f"{byte:02X}" for byte in data)


def to_printable(data: bytes) -> str:
    """Render bytes with control characters made visible, e.g. ``<LF>2729<CR>``."""
    out: list[str] = []
    for byte in data:
        if byte == 0x0A:
            out.append("<LF>")
        elif byte == 0x0D:
            out.append("<CR>")
        elif byte < 0x20 or byte == 0x7F:
            out.append(f"<{byte:02X}>")
        else:
            out.append(chr(byte))
    return "".join(out)


def to_display_text(text: str) -> str:
    """Best effort human readable rendering of a text descriptor.

    Text descriptors in DC-09 are often UTF-8 encoded, so try UTF-8 first and
    fall back to the raw latin-1 characters.
    """
    try:
        return wire_to_bytes(text).decode("utf-8")
    except UnicodeDecodeError:
        return text
