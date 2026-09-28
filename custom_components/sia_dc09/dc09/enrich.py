"""Joins decoded messages to the SIA and Contact ID code tables."""

from __future__ import annotations

from collections.abc import Mapping

from .cid_codes import CID_CODES, cid_address_meaning, cid_category
from .models import (
    AlarmEvent,
    DecodeResult,
    EnrichedEvent,
    EnrichedMessage,
)
from .sia_codes import category_for, lookup_sia_code, severity_for

#: The address meaning that identifies a field as carrying a user number.
#: ``Zone or user number`` is deliberately excluded: when the protocol itself
#: cannot say which one it is, neither can we, and naming the wrong thing is
#: worse than naming nothing.
USER_ADDRESS_MEANING = "user number"


def normalise_user_number(number: str) -> str:
    """Return a user number in the form used to look names up.

    Panels pad user numbers differently - ``5``, ``05`` and ``005`` are all the
    same person - so leading zeros are dropped on both sides of the lookup.
    """
    return number.strip().lstrip("0") or "0"


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


def user_number_of(enriched: EnrichedEvent) -> str | None:
    """Return the user number an event refers to, or ``None``.

    A user number reaches us one of two ways: the SIA ``id`` modifier, or the
    address field of a code whose address is defined to be a user, which is how
    panels report openings and closings.
    """
    event = enriched.event
    if event.user:
        return normalise_user_number(event.user)
    meaning = (enriched.address_meaning or "").lower()
    if event.address and meaning == USER_ADDRESS_MEANING:
        return normalise_user_number(event.address)
    return None


def summarise_with_user_names(
    message: EnrichedMessage, user_names: Mapping[str, str] | None
) -> str:
    """Re-render a summary with configured user names substituted in.

    Naming is a Home Assistant concern, not a protocol one, so the summary is
    built without names when the message is decoded and rewritten here once the
    account it belongs to is known.
    """
    if not user_names or not message.events:
        return message.summary
    return "; ".join(_event_phrase(event, user_names) for event in message.events)


def _address_label(
    meaning: str | None,
    address: str | None,
    user_names: Mapping[str, str] | None = None,
) -> str | None:
    if not address:
        return None
    if not meaning or "unused" in meaning.lower():
        return None
    number = normalise_user_number(address)
    if meaning.lower() == USER_ADDRESS_MEANING and (
        name := (user_names or {}).get(number)
    ):
        return f"User {name}"
    return f"{meaning} {number}"


def _event_phrase(
    enriched: EnrichedEvent, user_names: Mapping[str, str] | None = None
) -> str:
    """Render one event as the clause that appears in a summary."""
    name = enriched.title or f"Unknown code {enriched.code}"
    label = _address_label(enriched.address_meaning, enriched.event.address, user_names)
    where = f" - {label}" if label else ""
    area = f" (area {enriched.event.area})" if enriched.event.area else ""
    text = f' "{enriched.event.text}"' if enriched.event.text else ""
    return f"{name}{where}{area}{text}"


def _summarise(token: str, events: tuple[EnrichedEvent, ...], link_test: bool) -> str:
    if link_test:
        return "Link test (NULL) - supervision heartbeat, no event reported"
    if not events:
        return f"{token} message with no event data"
    return "; ".join(_event_phrase(event) for event in events)
