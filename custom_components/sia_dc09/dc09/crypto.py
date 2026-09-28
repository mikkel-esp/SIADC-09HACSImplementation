"""AES handling for encrypted DC-09 messages.

Uses ``cryptography``, which Home Assistant pins as a core dependency, so no
extra requirement is declared in the manifest.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .bytes_util import WIRE_ENCODING

# AES key sizes permitted by DC-09, in bytes.
VALID_KEY_LENGTHS = (16, 24, 32)

# A zero IV is mandated by DC-09; the random plaintext prefix supplies the entropy.
ZERO_IV = b"\x00" * 16


class Dc09CryptoError(Exception):
    """Raised when a key is unusable or a message cannot be decrypted."""


def parse_key(value: str | None) -> bytes | None:
    """Parse a DC-09 AES key.

    Accepts hex (32, 48 or 64 hex digits, the usual notation) or a raw
    16/24/32 character passphrase. Returns ``None`` for an empty value.
    """
    if value is None:
        return None
    trimmed = "".join(value.split())
    if not trimmed:
        return None

    if len(trimmed) // 2 in VALID_KEY_LENGTHS and len(trimmed) % 2 == 0:
        try:
            return bytes.fromhex(trimmed)
        except ValueError:
            pass

    encoded = trimmed.encode(WIRE_ENCODING)
    if len(encoded) in VALID_KEY_LENGTHS:
        return encoded

    raise Dc09CryptoError(
        "AES key must be 32, 48 or 64 hex digits (128/192/256 bit) "
        "or a 16/24/32 character passphrase."
    )


def decrypt_body(hex_text: str, key: bytes) -> str:
    """Decrypt the ciphertext of a ``*``-prefixed DC-09 message.

    DC-09 uses AES-CBC with an all-zero IV and no trailing padding. Instead the
    plaintext is prefixed with random padding terminated by a ``|``, which is
    consumed. Padding never contains ``|``, ``[`` or ``]``, so the first of
    those characters reliably marks the end of the padding.
    """
    normalized = hex_text.strip()
    if not normalized or len(normalized) % 32 != 0:
        raise Dc09CryptoError(
            "Encrypted data block is not a whole number of AES blocks in hex."
        )
    try:
        ciphertext = bytes.fromhex(normalized)
    except ValueError as err:
        raise Dc09CryptoError("Encrypted data block is not valid hex.") from err

    decryptor = Cipher(algorithms.AES(key), modes.CBC(ZERO_IV)).decryptor()
    plain = (decryptor.update(ciphertext) + decryptor.finalize()).decode(WIRE_ENCODING)
    return strip_padding(plain)


def strip_padding(plain: str) -> str:
    """Remove the random plaintext prefix that DC-09 uses instead of padding."""
    pipe = plain.find("|")
    bracket = plain.find("]")

    # A `|` terminates the padding and is consumed.
    if pipe >= 0 and (bracket < 0 or pipe < bracket):
        return plain[pipe + 1 :]
    # Otherwise the data block was empty and `]` is the first real character.
    if bracket >= 0:
        return plain[bracket:]

    raise Dc09CryptoError(
        "Decryption produced no `|` or `]` delimiter - the AES key is probably wrong."
    )


def encrypt_body(plaintext: str, key: bytes, padding: str | None = None) -> str:
    """Encrypt a data block the way a panel would, for tests and the debugger.

    ``padding`` lets a caller supply deterministic padding; otherwise random
    bytes that avoid ``|``, ``[`` and ``]`` are generated.
    """
    import secrets

    body = f"|{plaintext}"
    needed = (-len(body)) % 16
    if padding is None:
        alphabet = "".join(
            chr(code) for code in range(0x21, 0x7F) if chr(code) not in "|[]"
        )
        padding = "".join(secrets.choice(alphabet) for _ in range(needed))
    padded = f"{padding}{body}"
    if len(padded) % 16 != 0:
        raise Dc09CryptoError("Padded plaintext is not a whole number of AES blocks.")

    encryptor = Cipher(algorithms.AES(key), modes.CBC(ZERO_IV)).encryptor()
    data = padded.encode(WIRE_ENCODING)
    return (encryptor.update(data) + encryptor.finalize()).hex().upper()
