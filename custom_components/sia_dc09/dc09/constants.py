"""Protocol constants for SIA DC-09 and DC-03."""

from __future__ import annotations

from typing import Final

LF: Final = "\n"
CR: Final = "\r"

LF_BYTE: Final = 0x0A
CR_BYTE: Final = 0x0D

#: Extended data block identifiers defined by DC-09.
EXTENDED_DATA_MEANINGS: Final[dict[str, str]] = {
    "A": "Authentication hash",
    "E": "Event / sequence qualifier",
    "H": "Occurrence",
    "I": "Alarm text",
    "K": "Key exchange",
    "L": "Location",
    "M": "MAC address",
    "N": "Network address",
    "O": "Building name",
    "P": "Programming data",
    "R": "Room",
    "S": "Site name",
    "T": "Alarm trigger",
    "V": "Verification",
    "X": "Longitude",
    "Y": "Latitude",
    "Z": "Altitude",
}

#: Lower case block codes defined by SIA DC-03. They qualify the events that
#: follow them rather than reporting an event themselves.
SIA_MODIFIERS: Final[dict[str, str]] = {
    "ti": "Time of event (HH:MM:SS)",
    "da": "Date of event (MM-DD-YYYY)",
    "id": "User / identification number",
    "ri": "Area (region) identifier",
    "pi": "Peripheral identifier",
    "pa": "Programmatically assigned address",
    "pm": "Partition mask",
}

#: The SIA new-event / old-event message modifier.
SIA_QUALIFIERS: Final[dict[str, str]] = {
    "N": "New event",
    "R": "Old (previously reported) event",
}

#: Message tokens that carry no event data.
SIGNALLING_TOKENS: Final[frozenset[str]] = frozenset({"ACK", "NAK", "DUH", "RSP"})

#: Largest frame DC-09 allows, used to bound the TCP reassembly buffer.
MAX_FRAME_BYTES: Final = 8192
