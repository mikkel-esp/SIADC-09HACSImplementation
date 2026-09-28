"""CRC-16 used by ANSI/SIA DC-09 (identical to CRC-16/ARC).

Polynomial x^16 + x^15 + x^2 + 1, reflected (0xA001), initial value 0x0000.
"""

from __future__ import annotations


def crc16(data: bytes) -> int:
    """Return the DC-09 CRC-16 of ``data``."""
    crc = 0
    for byte in data:
        temp = byte
        for _ in range(8):
            if (crc ^ temp) & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
            temp >>= 1
    return crc & 0xFFFF


def crc16_hex(data: bytes) -> str:
    """Return the CRC rendered as the 4 uppercase hex characters DC-09 uses."""
    return f"{crc16(data):04X}"
