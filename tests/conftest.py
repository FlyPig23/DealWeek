"""Shared fixtures.

Every test runs against a frozen clock. Nothing here touches the network, a real
mailbox, or a paid API -- CI needs no secrets.
"""

from __future__ import annotations

import datetime as dt

import pytest

from weekly_deals.clock import FrozenClock
from weekly_deals.mail.fixtures import FixtureMailSource
from weekly_deals.models.mock import MockClassifier, MockExtractor
from weekly_deals.schemas import (
    Benefit,
    BenefitKind,
    DateConfidence,
    DateKind,
    Eligibility,
    Evidence,
    Money,
    NormalizedEmail,
    OfferDraft,
    Preferences,
    TemporalPoint,
    TemporalRules,
)
from weekly_deals.service import WeeklyDealsService

# A Wednesday, so "this week" has days on both sides of it.
REFERENCE = dt.datetime(2026, 9, 16, 9, 0)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(REFERENCE, "America/Chicago")


@pytest.fixture
def today() -> dt.date:
    return REFERENCE.date()


@pytest.fixture
def mail(clock: FrozenClock) -> FixtureMailSource:
    return FixtureMailSource(clock)


@pytest.fixture
def extractor() -> MockExtractor:
    return MockExtractor()


@pytest.fixture
def classifier() -> MockClassifier:
    return MockClassifier()


@pytest.fixture
def preferences() -> Preferences:
    return Preferences(currency="USD")


@pytest.fixture
def service(clock: FrozenClock) -> WeeklyDealsService:
    return WeeklyDealsService.offline(clock=clock)


def make_email(text: str, *, source_id: str = "m1", **kwargs) -> NormalizedEmail:
    defaults = {
        "source_id": source_id,
        "subject": "Test",
        "sender": "Test Merchant <a@b.example>",
        "normalized_text": text,
        "sender_date": dt.datetime(2026, 9, 15, 10, 0, tzinfo=dt.UTC),
    }
    defaults.update(kwargs)
    return NormalizedEmail(**defaults)


def point(day: dt.date | None, raw: str = "stated") -> TemporalPoint:
    if day is None:
        return TemporalPoint(confidence_state=DateConfidence.ABSENT)
    return TemporalPoint(
        raw_expression=raw,
        kind=DateKind.DATE_ONLY,
        date=day,
        confidence_state=DateConfidence.EXPLICIT_DATE_MISSING_TIME,
    )


def make_draft(
    *,
    merchant: str = "Test Merchant",
    amount_minor: int = 400,
    minimum_minor: int | None = None,
    ends: dt.date | None = None,
    claim: dt.date | None = None,
    claim_required: bool = False,
    weekdays: list[int] | None = None,
    quote: str = "$4 off",
    eligibility: Eligibility | None = None,
    message_id: str = "m1",
) -> OfferDraft:
    from weekly_deals.schemas import TriState

    return OfferDraft(
        merchant=merchant,
        benefit=Benefit(
            kind=BenefitKind.AMOUNT_OFF,
            amount_off=Money(minor=amount_minor, currency="USD"),
            minimum_spend=(
                Money(minor=minimum_minor, currency="USD") if minimum_minor is not None else None
            ),
            description=quote,
        ),
        temporal=TemporalRules(
            ends=point(ends),
            claim_deadline=point(claim),
            claim_required=TriState.KNOWN_YES if claim_required else TriState.UNKNOWN,
            weekdays=weekdays or [],
        ),
        eligibility=eligibility or Eligibility(),
        evidence=[
            Evidence(field_path="benefit.amount_off", message_id=message_id, quote=quote),
            *(
                [
                    Evidence(
                        field_path="temporal.ends",
                        message_id=message_id,
                        # Must match the sample body verbatim: the validator
                        # rejects a quote it cannot find, which is the point.
                        quote=f"Offer ends {ends.strftime('%B %d, %Y')}",
                    )
                ]
                if ends
                else []
            ),
        ],
    )
