"""SIA data code table and the classifiers built on top of it."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from .models import EventSeverity

_CODES_PATH: Final = Path(__file__).parent / "sia_codes.json"


@dataclass(frozen=True, slots=True)
class SiaCodeMeta:
    """Metadata for one SIA data code, from the SIA code spreadsheet."""

    code: str
    title: str
    description: str
    address_meaning: str


@lru_cache(maxsize=1)
def _load_codes() -> dict[str, SiaCodeMeta]:
    raw = json.loads(_CODES_PATH.read_text(encoding="utf-8"))
    return {
        code: SiaCodeMeta(
            code=entry["code"],
            title=entry["title"],
            description=entry["description"],
            address_meaning=entry["addressMeaning"],
        )
        for code, entry in raw.items()
    }


def sia_codes() -> dict[str, SiaCodeMeta]:
    """Return the whole SIA data code table, keyed by uppercase code."""
    return _load_codes()


def lookup_sia_code(code: str) -> SiaCodeMeta | None:
    """Look up a SIA data code, case insensitively."""
    return _load_codes().get(code.upper())


#: Category of a SIA data code, derived from its first letter as defined by the
#: SIA data code conventions.
SIA_CATEGORY_BY_LETTER: Final[dict[str, str]] = {
    "A": "Analog / AC power",
    "B": "Burglary",
    "C": "Closing (arming)",
    "D": "Access control",
    "E": "Exit / expansion device",
    "F": "Fire",
    "G": "Gas",
    "H": "Holdup",
    "I": "Equipment",
    "J": "User / journal",
    "K": "Heat",
    "L": "Programming / listen-in",
    "M": "Medical",
    "N": "Perimeter / network",
    "O": "Opening (disarming)",
    "P": "Panic",
    "Q": "Emergency",
    "R": "Remote programming / relay",
    "S": "Sprinkler",
    "T": "Tamper / test",
    "U": "Untyped zone",
    "V": "Printer",
    "W": "Water",
    "X": "Transmitter / RF",
    "Y": "System trouble",
    "Z": "Freeze",
}

_SEVERITY_RULES: Final[tuple[tuple[EventSeverity, re.Pattern[str]], ...]] = (
    ("status", re.compile(r"restor|unbypass|cancel|abort|reset", re.I)),
    # `Bypass` must beat the alarm keywords: `Holdup Bypass` is not a holdup alarm.
    ("trouble", re.compile(r"bypass|shunt", re.I)),
    ("test", re.compile(r"test", re.I)),
    (
        "supervisory",
        re.compile(r"supervisor|missing|no activity|delinquent", re.I),
    ),
    (
        "trouble",
        re.compile(r"trouble|fault|fail|denied|error|low\b|unable", re.I),
    ),
    (
        "alarm",
        re.compile(
            r"alarm|panic|holdup|duress|medical|emergency|tamper|verified", re.I
        ),
    ),
)


def _match_severity(text: str) -> EventSeverity | None:
    for severity, pattern in _SEVERITY_RULES:
        if pattern.search(text):
            return severity
    return None


def severity_for(title: str, description: str) -> EventSeverity:
    """Classify an event.

    The short title is the strongest signal; the long description is only
    consulted when the title is not conclusive.
    """
    return _match_severity(title) or _match_severity(description) or "status"


def category_for(code: str) -> str:
    """Return the SIA category implied by a code's leading letter."""
    if not code:
        return "Unclassified"
    return SIA_CATEGORY_BY_LETTER.get(code[0].upper(), "Unclassified")
