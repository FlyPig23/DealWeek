"""Build a date-aware index for every promotion email.

This index is deliberately separate from the food-offer extractor. A promotion
can be useful as a reminder even when it is a retail, travel, event, or software
offer, and missing a structured coupon should not make the email disappear.
Dates are only filled when the message gives an explicit date near an expiry
phrase; otherwise the calendar keeps the date unknown.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from email.utils import parseaddr
from typing import Literal

from ..schemas import NormalizedEmail, PromotionEvent

_MONTHS = {
    name: index + 1
    for index, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        )
    )
    for name in names
}
_MONTH_DATE = re.compile(
    r"\b(?P<month>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<year>20\d{2}))?\b",
    re.IGNORECASE,
)
_NUMERIC_DATE = re.compile(
    r"\b(?P<month>\d{1,2})[/-](?P<day>\d{1,2})(?:[/-](?P<year>\d{2,4}))?\b"
)
_ISO_DATE = re.compile(r"\b(?P<year>20\d{2})-(?P<month>\d{1,2})-(?P<day>\d{1,2})\b")
_EXPIRY = re.compile(
    r"\b(?:expires?|expiration|ends?|ending|valid\s+(?:through|until|on)|"
    r"offer\s+(?:ends?|expires?)|redeem\s+by|last\s+day|good\s+through)\b",
    re.IGNORECASE,
)
_FOOD = re.compile(
    r"\b(?:restaurant|food|meal|dining|lunch|dinner|breakfast|pizza|taco|burger|"
    r"sushi|chicken|rice\s+bowl|bowls?|coffee|cafe|café|bakery|grocer(?:y|ies)|delivery|"
    r"doordash|grubhub|ubereats|starbucks|chipotle|panera)\b",
    re.IGNORECASE,
)
_TRAVEL = re.compile(r"\b(?:hotel|flight|airline|travel|resort|cruise|rental car)\b", re.I)
_EVENTS = re.compile(r"\b(?:concert|ticket|festival|event|workshop|webinar|conference)\b", re.I)
_RETAIL = re.compile(
    r"\b(?:shop(?:ping)?|store|clothing|shoes|electronics|supplies|merch|retail|gift\s+card)\b",
    re.I,
)
_SERVICES = re.compile(r"\b(?:software|cloud|subscription|membership|course|class|service)\b", re.I)
_BENEFIT = re.compile(
    r"(?:\$\s?\d+(?:\.\d{1,2})?\s*(?:off|credit|back|reward)|\d{1,3}\s?%\s?off|"
    r"free\s+[A-Za-z][^.!?\n]{0,50}|bogo|buy\s+\d+\s+get\s+\d+)",
    re.IGNORECASE,
)


def _merchant(sender: str) -> str:
    display, address = parseaddr(sender)
    value = display.strip().strip('"') or address.split("@", 1)[0]
    return value or "Unknown sender"


def _category(email: NormalizedEmail) -> str:
    text = f"{email.subject}\n{email.normalized_text}"
    if _FOOD.search(text):
        return "food"
    if _TRAVEL.search(text):
        return "travel"
    if _EVENTS.search(text):
        return "events"
    if _RETAIL.search(text):
        return "retail"
    if _SERVICES.search(text):
        return "services"
    return "other"


def _candidate_dates(text: str, *, default_year: int) -> list[tuple[date, str, int, int]]:
    found: list[tuple[date, str, int, int]] = []
    patterns = (_ISO_DATE, _MONTH_DATE, _NUMERIC_DATE)
    for pattern in patterns:
        for match in pattern.finditer(text):
            try:
                year_raw = match.group("year")
                year = int(year_raw) if year_raw else None
                if year is not None and year < 100:
                    year += 2000
                if pattern is _ISO_DATE:
                    year = int(match.group("year"))
                month = (
                    _MONTHS[match.group("month").lower().rstrip(".")]
                    if pattern is _MONTH_DATE
                    else int(match.group("month"))
                )
                day = int(match.group("day"))
                if year is None:
                    year = default_year
                found.append((date(year, month, day), match.group(0), match.start(), match.end()))
            except (TypeError, ValueError, KeyError):
                continue
    return sorted(found, key=lambda item: item[2])


def _expiry(text: str, now: date) -> tuple[date | None, str | None, str | None]:
    dates = _candidate_dates(text, default_year=now.year)
    if not dates:
        return None, None, None
    contextual: list[tuple[date, str, str]] = []
    for expiry in _EXPIRY.finditer(text):
        for value, raw, start, end in dates:
            if start >= expiry.end() and start - expiry.end() <= 100:
                resolved = value
                if not re.search(r"\b20\d{2}\b", raw) and value < now - timedelta(days=180):
                    try:
                        resolved = value.replace(year=value.year + 1)
                    except ValueError:
                        resolved = value
                contextual.append((resolved, raw, text[max(0, expiry.start() - 30) : end]))
                break
    if contextual:
        return contextual[0]
    return None, None, None


def _status(
    *,
    start_date: date | None,
    end_date: date | None,
    now: date,
    needs_review: bool,
) -> Literal["expired", "ending_soon", "active", "upcoming", "unknown"]:
    if needs_review and end_date is None:
        return "unknown"
    if end_date is not None and end_date < now:
        return "expired"
    if start_date is not None and start_date > now:
        return "upcoming"
    if end_date is not None and end_date <= now + timedelta(days=7):
        return "ending_soon"
    if end_date is not None:
        return "active"
    return "unknown"


def build_promotion_event(email: NormalizedEmail, *, now: datetime) -> PromotionEvent:
    end_date, raw_date, evidence = _expiry(email.normalized_text, now.date())
    needs_review = not email.body_complete or email.has_unparsed_visuals or email.truncated
    benefit = _BENEFIT.search(f"{email.subject}\n{email.normalized_text}")
    return PromotionEvent(
        promotion_id=email.source_id,
        message_id=email.source_id,
        merchant=_merchant(email.sender),
        title=email.subject.strip() or "Untitled promotion",
        category=_category(email),
        start_date=None,
        end_date=end_date,
        date_expression=raw_date,
        benefit_hint=benefit.group(0).strip() if benefit else None,
        status=_status(
            start_date=None,
            end_date=end_date,
            now=now.date(),
            needs_review=needs_review,
        ),
        evidence=evidence,
        source_date=email.sender_date or email.received_date,
        needs_review=needs_review,
        body_complete=email.body_complete,
        has_unparsed_visuals=email.has_unparsed_visuals,
    )


def refresh_promotion_event(event: PromotionEvent, *, now: datetime) -> PromotionEvent:
    return event.model_copy(
        update={
            "status": _status(
                start_date=event.start_date,
                end_date=event.end_date,
                now=now.date(),
                needs_review=event.needs_review,
            )
        }
    )
