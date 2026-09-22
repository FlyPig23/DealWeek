"""The schema the language model actually sees.

Deliberately flatter than :mod:`mealdeals.schemas`. Structured-output modes
behave far better on shallow objects with explicit nulls than on deep trees with
defaults, and keeping the two apart means the internal contract can evolve
without rewriting the prompt.

Nothing here is trusted. Everything is mapped through
:func:`to_draft` and then re-validated deterministically.
"""

from __future__ import annotations

from datetime import date as Date

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import (
    Benefit,
    BenefitKind,
    Channel,
    DateConfidence,
    DateKind,
    Eligibility,
    Evidence,
    FoodCategory,
    Money,
    OfferDraft,
    TemporalPoint,
    TemporalRules,
    TriState,
)


class WireEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field_path: str
    quote: str


class WireDate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    raw_expression: str | None = None
    iso_date: str | None = Field(default=None, description="YYYY-MM-DD, or null if not stated")
    has_explicit_time: bool = False

    def to_point(self) -> TemporalPoint:
        if not self.iso_date:
            return TemporalPoint(
                raw_expression=self.raw_expression,
                kind=DateKind.RELATIVE if self.raw_expression else DateKind.UNKNOWN,
                confidence_state=(
                    DateConfidence.RELATIVE_AMBIGUOUS
                    if self.raw_expression
                    else DateConfidence.ABSENT
                ),
            )
        try:
            parsed = Date.fromisoformat(self.iso_date)
        except ValueError:
            return TemporalPoint(
                raw_expression=self.raw_expression,
                confidence_state=DateConfidence.RELATIVE_AMBIGUOUS,
            )
        return TemporalPoint(
            raw_expression=self.raw_expression,
            kind=DateKind.DATETIME if self.has_explicit_time else DateKind.DATE_ONLY,
            date=parsed,
            confidence_state=(
                DateConfidence.EXPLICIT_DATETIME
                if self.has_explicit_time
                else DateConfidence.EXPLICIT_DATE_MISSING_TIME
            ),
        )


class WireOffer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    merchant: str
    title: str = ""
    food_category: str = "other_or_unclear"
    benefit_kind: str = "other"
    amount_off_minor: int | None = None
    percent_off: int | None = None
    fixed_price_minor: int | None = None
    cap_minor: int | None = None
    cap_explicitly_absent: bool = False
    minimum_spend_minor: int | None = None
    currency: str = "USD"
    promo_code: str | None = None
    free_item_description: str | None = None
    description: str = ""

    valid_from: WireDate | None = None
    valid_until: WireDate | None = None
    claim_deadline: WireDate | None = None
    claim_required: str = "unknown"
    weekdays: list[int] = Field(default_factory=list)

    new_customer_only: str = "unknown"
    membership_required: str = "unknown"
    membership_name: str | None = None
    participating_locations_only: str = "unknown"
    location_note: str | None = None
    usage_limit: int | None = None
    stackable: str = "unknown"
    channels: list[str] = Field(default_factory=list)

    alternative_group: str | None = None
    evidence: list[WireEvidence] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    needs_visual_parse: bool = False


class WireExtraction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str = "success"
    offers: list[WireOffer] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _tri(value: str | None) -> TriState:
    try:
        return TriState(value or "unknown")
    except ValueError:
        return TriState.UNKNOWN


def _money(minor: int | None, currency: str) -> Money | None:
    if minor is None or minor < 0:
        return None
    return Money(minor=minor, currency=currency)


def to_draft(wire: WireOffer, message_id: str) -> OfferDraft:
    """Map model output into the internal draft.

    Unparseable enum values degrade to their unknown variant rather than raising,
    because a single odd field should not discard a whole extraction. The
    resulting unknowns are what the validator then reports to the user.
    """
    currency = (wire.currency or "USD").upper()[:3]

    try:
        kind = BenefitKind(wire.benefit_kind)
    except ValueError:
        kind = BenefitKind.OTHER

    amount = _money(wire.amount_off_minor, currency)
    fixed = _money(wire.fixed_price_minor, currency)
    # Sanitise the percentage *before* deciding the kind: a model returning
    # "150% off" must degrade to an unpriceable benefit, not raise and take the
    # whole email's extraction down with it.
    percent = wire.percent_off if wire.percent_off and 1 <= wire.percent_off <= 100 else None

    # A kind whose payload did not survive validation cannot stand.
    if kind is BenefitKind.AMOUNT_OFF and amount is None:
        kind = BenefitKind.OTHER
    if kind is BenefitKind.PERCENT_OFF and percent is None:
        kind = BenefitKind.OTHER
    if kind is BenefitKind.FIXED_PRICE and fixed is None:
        kind = BenefitKind.OTHER

    benefit = Benefit(
        kind=kind,
        amount_off=amount,
        percent_off=percent,
        fixed_price=fixed,
        cap=_money(wire.cap_minor, currency),
        cap_stated_absent=wire.cap_explicitly_absent,
        minimum_spend=_money(wire.minimum_spend_minor, currency),
        promo_code=wire.promo_code,
        free_item_description=wire.free_item_description,
        description=wire.description,
    )

    channels: list[Channel] = []
    for raw in wire.channels:
        try:
            channels.append(Channel(raw))
        except ValueError:
            continue
    if not channels:
        channels = [Channel.UNKNOWN]

    try:
        category = FoodCategory(wire.food_category)
    except ValueError:
        category = FoodCategory.OTHER_OR_UNCLEAR

    temporal = TemporalRules(
        starts=wire.valid_from.to_point() if wire.valid_from else TemporalPoint(),
        ends=wire.valid_until.to_point() if wire.valid_until else TemporalPoint(),
        claim_deadline=wire.claim_deadline.to_point() if wire.claim_deadline else TemporalPoint(),
        claim_required=_tri(wire.claim_required),
        weekdays=[d for d in wire.weekdays if 0 <= d <= 6],
    )

    eligibility = Eligibility(
        new_customer_only=_tri(wire.new_customer_only),
        membership_required=_tri(wire.membership_required),
        membership_name=wire.membership_name,
        participating_locations_only=_tri(wire.participating_locations_only),
        location_note=wire.location_note,
        usage_limit=wire.usage_limit if (wire.usage_limit or 0) >= 1 else None,
        stackable=_tri(wire.stackable),
        channels=channels,
    )

    evidence = [
        Evidence(field_path=item.field_path, message_id=message_id, quote=item.quote)
        for item in wire.evidence
        if len(item.quote.strip()) >= 3
    ]

    return OfferDraft(
        merchant=wire.merchant or "Unknown merchant",
        merchant_confidence="explicit" if wire.merchant else "unknown",
        food_category=category,
        title=wire.title,
        benefit=benefit,
        temporal=temporal,
        eligibility=eligibility,
        alternative_group=wire.alternative_group,
        evidence=evidence,
        unresolved_fields=wire.unresolved_fields,
        conflicts=wire.conflicts,
        needs_visual_parse=wire.needs_visual_parse,
    )


def safe_drafts(wires: list[WireOffer], message_id: str) -> tuple[list[OfferDraft], list[str]]:
    """Map a batch, skipping individual offers that cannot be mapped.

    One malformed offer in an email of four should cost that one offer, not the
    whole email. The names of the dropped merchants come back so the caller can
    downgrade the extraction to ``needs_review`` rather than report a clean
    success.
    """
    drafts: list[OfferDraft] = []
    dropped: list[str] = []
    for wire in wires:
        try:
            drafts.append(to_draft(wire, message_id))
        except Exception:
            dropped.append(wire.merchant or "unnamed offer")
    return drafts, dropped


def json_schema() -> dict:
    """JSON Schema for providers that accept one."""
    return WireExtraction.model_json_schema()
