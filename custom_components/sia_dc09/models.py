"""Data model shared between the receiver, the hub and the entities.

:class:`SiaDc09Event` is the rich object handed to entities over the
dispatcher. :meth:`SiaDc09Event.as_bus_payload` flattens it into the
JSON-serialisable dictionary fired on the event bus, which is a superset of the
payload Home Assistant's built-in ``sia`` integration produces.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .dc09 import (
    AlarmEvent,
    EnrichedEvent,
    EnrichedMessage,
    ReceivedMessage,
    summarise_with_user_names,
    to_hex,
    user_number_of,
)
from .state_machine import SiaStatus, StatusTransition


@dataclass(frozen=True, slots=True)
class SiaDc09Event:
    """One decoded alarm event, ready for entities and the event bus."""

    account: str
    received_at: datetime
    transport: str
    port: int
    remote_ip: str

    #: ``SIA-DCS``, ``ADM-CID``, ``NULL`` or ``OTHER``.
    protocol: str = "OTHER"
    sequence: str | None = None
    receiver: str | None = None
    line_prefix: str | None = None

    code: str | None = None
    #: Human readable name for the code, from the SIA or Contact ID tables.
    code_title: str | None = None
    message: str | None = None
    summary: str = ""
    severity: str = "info"
    category: str = "other"

    zone: str | None = None
    area: str | None = None
    user: str | None = None
    #: The user this event is about, from either the ``id`` modifier or an
    #: address field that is defined to carry a user number.
    user_number: str | None = None
    #: The configured name for :attr:`user_number`, if the account has one.
    user_name: str | None = None
    partition: str | None = None
    event_qualifier: str | None = None

    timestamp: datetime | None = None
    extended_data: dict[str, str] = field(default_factory=dict)

    is_test: bool = False
    is_link_test: bool = False
    encrypted: bool = False
    crc_valid: bool = True
    decoded: bool = True
    errors: tuple[str, ...] = ()

    status_before: str | None = None
    status_after: str | None = None
    status_changed: bool = False

    response: str | None = None
    raw_hex: str = ""

    def as_bus_payload(self) -> dict[str, Any]:
        """Return a JSON-serialisable payload for ``hass.bus.async_fire``.

        Keys named after core's ``get_event_data_from_sia_event`` are kept so
        existing automations written against the built-in integration keep
        working; the rest are additions this integration can provide because it
        decodes and enriches the whole message.
        """
        return {
            # --- shape shared with Home Assistant core -----------------------
            "account": self.account,
            "code": self.code,
            "message": self.message,
            "message_type": self.protocol,
            "receiver": self.receiver,
            "line": self.line_prefix,
            "sequence": self.sequence,
            "ri": self.area,
            "id": self.user,
            "zone": self.zone,
            "partition": self.partition,
            "event_qualifier": self.event_qualifier,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "extended_data": dict(self.extended_data),
            # --- additions ---------------------------------------------------
            "code_title": self.code_title,
            "summary": self.summary,
            "user_number": self.user_number,
            "user_name": self.user_name,
            "severity": self.severity,
            "category": self.category,
            "is_test": self.is_test,
            "is_link_test": self.is_link_test,
            "encrypted": self.encrypted,
            "crc_valid": self.crc_valid,
            "decoded": self.decoded,
            "errors": list(self.errors),
            "status_before": self.status_before,
            "status_after": self.status_after,
            "status_changed": self.status_changed,
            "response": self.response,
            "transport": self.transport,
            "port": self.port,
            "remote_ip": self.remote_ip,
            "received_at": self.received_at.isoformat(),
        }


def _first_enriched(enriched: EnrichedMessage | None) -> EnrichedEvent | None:
    """Return the first enriched event of a message, if there is one."""
    if enriched is None or not enriched.events:
        return None
    return enriched.events[0]


def _first_event(message: ReceivedMessage) -> AlarmEvent | None:
    """Return the first decoded event of a message, if there is one."""
    payload = message.decode.payload
    if payload is None or not payload.events:
        return None
    return payload.events[0]


def build_event(
    message: ReceivedMessage,
    account: str,
    transition: StatusTransition | None = None,
    user_names: Mapping[str, str] | None = None,
) -> SiaDc09Event:
    """Turn a received message into the event entities and automations see.

    A message can technically carry several events. The first one drives the
    scalar fields, which matches how panels actually behave in practice; the
    status machine still folds in every event.

    ``user_names`` maps user numbers to the names configured for the account,
    so "User number 501" reads as "User Mikkel" wherever the summary is shown.
    """
    frame = message.decode.frame
    payload = message.decode.payload
    event = _first_event(message)
    enriched_event = _first_enriched(message.enriched)

    before: str | None = None
    after: str | None = None
    changed = False
    if transition is not None:
        before = (
            None if transition.before is SiaStatus.UNKNOWN else transition.before.value
        )
        after = (
            None if transition.after is SiaStatus.UNKNOWN else transition.after.value
        )
        changed = transition.changed

    user_number = user_number_of(enriched_event) if enriched_event is not None else None
    user_name = user_names.get(user_number) if user_names and user_number else None

    return SiaDc09Event(
        account=account,
        received_at=message.received_at,
        transport=str(message.transport).lower(),
        port=message.local_port,
        remote_ip=message.remote_ip,
        protocol=payload.protocol if payload is not None else "OTHER",
        sequence=frame.sequence if frame is not None else None,
        receiver=frame.receiver if frame is not None else None,
        line_prefix=frame.line_prefix if frame is not None else None,
        code=event.code if event is not None else None,
        code_title=enriched_event.title if enriched_event is not None else None,
        message=event.text if event is not None else None,
        summary=summarise_with_user_names(message.enriched, user_names)
        if message.enriched is not None
        else "",
        severity=enriched_event.severity if enriched_event is not None else "info",
        category=enriched_event.category if enriched_event is not None else "other",
        zone=event.address if event is not None else None,
        area=event.area if event is not None else None,
        user=event.user if event is not None else None,
        user_number=user_number,
        user_name=user_name,
        partition=event.partition if event is not None else None,
        event_qualifier=event.qualifier if event is not None else None,
        timestamp=frame.timestamp_utc if frame is not None else None,
        extended_data={
            item.id: item.value for item in (frame.extended_data if frame else ())
        },
        is_test=transition.is_test if transition is not None else False,
        is_link_test=payload.link_test if payload is not None else False,
        encrypted=frame.encrypted if frame is not None else False,
        crc_valid=frame.crc.valid if frame is not None else False,
        decoded=message.decode.ok,
        errors=message.decode.errors,
        status_before=before,
        status_after=after,
        status_changed=changed,
        response=message.response.kind if message.response is not None else None,
        raw_hex=to_hex(message.raw),
    )
