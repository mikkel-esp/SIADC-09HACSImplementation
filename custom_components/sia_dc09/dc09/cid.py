"""Parser for the ``ADM-CID`` data block (Contact ID content)."""

from __future__ import annotations

import re

from .cid_codes import CID_QUALIFIERS
from .models import AlarmEvent, DecodedPayload, SiaBlock

#: ``1628 01 000`` - qualifier, event code, group/partition, zone/user.
CID_PATTERN = re.compile(r"^(\d)(\d{3})(\d{2})(\d{3})$")


def parse_cid_payload(body: str) -> DecodedPayload:
    """Parse a Contact ID data block of the shape ``#account|1628 01 000``.

    Whitespace between the fields is optional.
    """
    account, content = _split_account(body)
    compact = "".join(content.split())
    match = CID_PATTERN.match(compact)

    if not match:
        return DecodedPayload(
            protocol="ADM-CID",
            account=account,
            blocks=(SiaBlock(raw=content, kind="unknown", code="", value=content),),
        )

    qualifier, code, group, zone = match.groups()
    blocks = (
        SiaBlock(
            raw=qualifier,
            kind="modifier",
            code="Q",
            value=qualifier,
            meaning=CID_QUALIFIERS.get(qualifier),
        ),
        SiaBlock(
            raw=code,
            kind="event",
            code=code,
            value=code,
            meaning="Contact ID event code",
        ),
        SiaBlock(
            raw=group,
            kind="modifier",
            code="GG",
            value=group,
            meaning="Group / partition number",
        ),
        SiaBlock(
            raw=zone,
            kind="modifier",
            code="ZZZ",
            value=zone,
            meaning="Zone or user number",
        ),
    )

    event = AlarmEvent(
        code=code,
        source=content.strip(),
        qualifier=qualifier,
        qualifier_meaning=CID_QUALIFIERS.get(qualifier),
        address=zone,
        area=group,
        partition=group,
        user=zone if 400 <= int(code) < 500 else None,
    )

    return DecodedPayload(
        protocol="ADM-CID", account=account, events=(event,), blocks=blocks
    )


def _split_account(body: str) -> tuple[str | None, str]:
    pipe = body.find("|")
    if pipe < 0:
        return None, body
    head = body[:pipe]
    account = head[1:] if head.startswith("#") else (head or None)
    return account, body[pipe + 1 :]
