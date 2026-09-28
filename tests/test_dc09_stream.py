"""TCP reassembly tests, ported from the SIADC09Debugger vitest suite."""

from __future__ import annotations

from custom_components.sia_dc09.dc09 import extract_frames, wire_to_bytes

A = '\n68AF0025"SIA-DCS"0001L0#1234[#1234|Nri1/BA12]\r'
B = '\n68AF0025"SIA-DCS"0002L0#1234[#1234|Nri1/BA13]\r'


def _texts(frames: tuple[bytes, ...]) -> list[str]:
    return [frame.decode("latin-1") for frame in frames]


def test_returns_a_single_whole_frame() -> None:
    result = extract_frames(wire_to_bytes(A))

    assert _texts(result.frames) == [A]
    assert result.rest == b""


def test_splits_several_frames_delivered_in_one_read() -> None:
    result = extract_frames(wire_to_bytes(A + B))

    assert _texts(result.frames) == [A, B]
    assert result.rest == b""


def test_holds_a_partial_frame_back_until_the_rest_arrives() -> None:
    raw = wire_to_bytes(A)
    split = 20

    first = extract_frames(raw[:split])
    assert first.frames == ()
    assert len(first.rest) == split

    second = extract_frames(first.rest + raw[split:])
    assert _texts(second.frames) == [A]
    assert second.rest == b""


def test_reassembles_a_frame_split_byte_by_byte() -> None:
    pending = b""
    collected: list[str] = []

    for byte in wire_to_bytes(A + B):
        pending += bytes([byte])
        result = extract_frames(pending)
        pending = result.rest
        collected.extend(_texts(result.frames))

    assert collected == [A, B]
    assert pending == b""


def test_keeps_the_trailing_partial_frame() -> None:
    result = extract_frames(wire_to_bytes(A + B[:15]))

    assert _texts(result.frames) == [A]
    assert result.rest.decode("latin-1") == B[:15]


def test_discards_leading_bytes_that_cannot_start_a_frame() -> None:
    result = extract_frames(wire_to_bytes(f"garbage{A}"))

    assert _texts(result.frames) == [A]
    assert result.discarded == 7


def test_drops_an_oversized_partial_frame() -> None:
    flood = wire_to_bytes("\n" + "x" * 9000)
    result = extract_frames(flood)

    assert result.frames == ()
    assert result.rest == b""
    assert result.discarded == len(flood)


def test_returns_nothing_for_a_buffer_with_no_frame_start() -> None:
    result = extract_frames(wire_to_bytes("nothing here"))

    assert result.frames == ()
    assert result.rest == b""
    assert result.discarded == 12
