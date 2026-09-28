"""Parser for the ``SIA-DCS`` data block (SIA DC-03 content)."""

from __future__ import annotations

import re

from .bytes_util import to_display_text
from .constants import SIA_MODIFIERS, SIA_QUALIFIERS
from .models import AlarmEvent, DecodedPayload, SiaBlock
from .sia_codes import lookup_sia_code

TEXT_DESCRIPTOR = re.compile(r"\^([^^]*)\^")


def parse_sia_payload(body: str) -> DecodedPayload:
    """Parse a SIA data block of the shape ``#account|Nri01/BA0003^Front door^``."""
    account, content = _split_account(body)
    blocks: list[SiaBlock] = []
    events: list[AlarmEvent] = []

    qualifier: str | None = None
    area: str | None = None
    user: str | None = None
    partition: str | None = None

    raw_blocks = content.split("/") if content else []
    for index, raw in enumerate(raw_blocks):
        remainder = raw
        text: str | None = None

        descriptor = TEXT_DESCRIPTOR.search(remainder)
        if descriptor:
            text = to_display_text(descriptor.group(1))
            remainder = TEXT_DESCRIPTOR.sub("", remainder, count=1)

        # The N/R new-vs-old event modifier only ever leads the first block, and
        # is only a modifier when what follows is itself a recognised code.
        leader = remainder[:1]
        after_leader = remainder[1:3]
        if (
            index == 0
            and len(remainder) > 2
            and leader in ("N", "R")
            and (
                after_leader in SIA_MODIFIERS
                or lookup_sia_code(after_leader) is not None
            )
        ):
            qualifier = leader
            blocks.append(
                SiaBlock(
                    raw=leader,
                    kind="modifier",
                    code=leader,
                    value="",
                    meaning=SIA_QUALIFIERS.get(leader),
                )
            )
            remainder = remainder[1:]

        if not remainder:
            continue

        code = remainder[:2]
        value = remainder[2:]
        modifier = SIA_MODIFIERS.get(code)

        if modifier:
            if code == "ri":
                area = value
            elif code == "id":
                user = value
            elif code == "pm":
                partition = value
            blocks.append(
                SiaBlock(
                    raw=raw, kind="modifier", code=code, value=value, meaning=modifier
                )
            )
            continue

        meta = lookup_sia_code(code)
        if meta:
            blocks.append(
                SiaBlock(
                    raw=raw,
                    kind="event",
                    code=code.upper(),
                    value=value,
                    meaning=meta.title,
                )
            )
            events.append(
                AlarmEvent(
                    code=code.upper(),
                    source=raw,
                    qualifier=qualifier,
                    qualifier_meaning=(
                        SIA_QUALIFIERS.get(qualifier) if qualifier else None
                    ),
                    address=value or None,
                    area=area,
                    user=user,
                    partition=partition,
                    text=text,
                )
            )
            continue

        blocks.append(SiaBlock(raw=raw, kind="unknown", code=code, value=value))

    # Modifiers may follow the event they qualify, so fill in anything learned
    # after an event was emitted.
    if any(value is not None for value in (area, user, partition)):
        events = [
            AlarmEvent(
                code=event.code,
                source=event.source,
                qualifier=event.qualifier,
                qualifier_meaning=event.qualifier_meaning,
                address=event.address,
                area=event.area or area,
                user=event.user or user,
                partition=event.partition or partition,
                text=event.text,
            )
            for event in events
        ]

    return DecodedPayload(
        protocol="SIA-DCS",
        account=account,
        events=tuple(events),
        blocks=tuple(blocks),
    )


def _split_account(body: str) -> tuple[str | None, str]:
    pipe = body.find("|")
    if pipe < 0:
        return None, body
    head = body[:pipe]
    account = head[1:] if head.startswith("#") else (head or None)
    return account, body[pipe + 1 :]
