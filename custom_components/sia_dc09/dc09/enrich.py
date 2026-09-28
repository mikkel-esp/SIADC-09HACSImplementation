"""Joins decoded messages to the SIA and Contact ID code tables."""

from __future__ import annotations

from .cid_codes import CID_CODES, cid_address_meaning, cid_category
from .models import (
    AlarmEvent,
    DecodeResult,
    EnrichedEvent,
    EnrichedMessage,
)
from .sia_codes import category_for, lookup_sia_code, severity_for


def enrich(result: DecodeResult) -> EnrichedMessage | None:
    """Produce the view of a message that an operator actually wants to read."""
    frame = result.frame
    payload = result.payload
    if frame is None or payload is None:
        return None

    events = tuple(
        _enrich_cid_event(event)
        if payload.protocol == "ADM-CID"
        else _enrich_sia_event(event)
        for event in payload.events
    )

    return EnrichedMessage(
        protocol=payload.protocol,
        account=payload.account or frame.account,
        receiver=frame.receiver,
        line_prefix=frame.line_prefix,
        sequence=frame.sequence,
        timestamp_utc=frame.timestamp_utc,
        link_test=payload.link_test,
        summary=_summarise(frame.protocol_token, events, payload.link_test),
        events=events,
    )


def _enrich_sia_event(event: AlarmEvent) -> EnrichedEvent:
    meta = lookup_sia_code(event.code)
    if meta is None:
        return EnrichedEvent(
            event=event,
            known=False,
            severity="unknown",
            category=category_for(event.code),
        )
    return EnrichedEvent(
        event=event,
        known=True,
        title=meta.title,
        description=meta.description,
        address_meaning=meta.address_meaning,
        address_label=_address_label(meta.address_meaning, event.address),
        severity=severity_for(meta.title, meta.description),
        category=category_for(event.code),
    )


def _enrich_cid_event(event: AlarmEvent) -> EnrichedEvent:
    title = CID_CODES.get(event.code)
    address_meaning = cid_address_meaning(event.code)
    return EnrichedEvent(
        event=event,
        known=title is not None,
        title=title,
        description=f"Contact ID {event.code}: {title}" if title else None,
        address_meaning=address_meaning,
        address_label=_address_label(address_meaning, event.address),
        severity=severity_for(title or "", event.qualifier_meaning or ""),
        category=cid_category(event.code),
    )


def _address_label(meaning: str | None, address: str | None) -> str | None:
    if not address:
        return None
    if not meaning or "unused" in meaning.lower():
        return None
    return f"{meaning} {address.lstrip('0') or '0'}"


def _summarise(
    token: str, events: tuple[EnrichedEvent, ...], link_test: bool
) -> str:
    if link_test:
        return "Link test (NULL) - supervision heartbeat, no event reported"
    if not events:
        return f"{token} message with no event data"

    parts: list[str] = []
    for enriched in events:
        name = enriched.title or f"Unknown code {enriched.code}"
        where = f" - {enriched.address_label}" if enriched.address_label else ""
        area = f" (area {enriched.event.area})" if enriched.event.area else ""
        text = f' "{enriched.event.text}"' if enriched.event.text else ""
        parts.append(f"{name}{where}{area}{text}")
    return "; ".join(parts)
