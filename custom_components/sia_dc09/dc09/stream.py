"""TCP frame reassembly.

TCP delivers a byte stream, not discrete messages: a single read can carry half
a message, several messages, or a message plus the start of the next one. DC-09
frames are self-delimiting (``<LF> ... <CR>``), so this splits a running buffer
back into whole frames.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import CR_BYTE, LF_BYTE, MAX_FRAME_BYTES


@dataclass(frozen=True, slots=True)
class FrameExtraction:
    """Result of pulling frames out of a reassembly buffer."""

    #: Complete frames, each still including its leading LF and trailing CR.
    frames: tuple[bytes, ...]
    #: Bytes belonging to a frame that has not finished arriving yet.
    rest: bytes
    #: Bytes thrown away because they could not begin a valid frame.
    discarded: int


def extract_frames(
    buffer: bytes, max_frame_bytes: int = MAX_FRAME_BYTES
) -> FrameExtraction:
    """Pull every complete frame out of ``buffer``.

    Anything before the first LF is discarded - a panel that opens a connection
    mid-message, or a port scanner, should not desynchronise the parser forever.
    A partial frame longer than ``max_frame_bytes`` is dropped for the same
    reason, otherwise a peer that never sends CR would grow the buffer without
    bound.
    """
    frames: list[bytes] = []
    rest = buffer
    discarded = 0

    while True:
        start = rest.find(LF_BYTE)

        if start == -1:
            return FrameExtraction(tuple(frames), b"", discarded + len(rest))

        discarded += start

        end = rest.find(CR_BYTE, start + 1)

        if end == -1:
            pending = rest[start:]
            if len(pending) > max_frame_bytes:
                return FrameExtraction(
                    tuple(frames), b"", discarded + len(pending)
                )
            return FrameExtraction(tuple(frames), pending, discarded)

        frames.append(rest[start : end + 1])
        rest = rest[end + 1 :]
