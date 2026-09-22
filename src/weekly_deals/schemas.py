"""Core data contracts.

One schema source for the whole application. Rules encoded here (not just
documented):

* Money is always an integer in the currency's minor unit. Floats never touch a
  price.
* An unknown deadline is ``None`` plus a ``confidence_state`` -- never a
  far-future sentinel that would read as "never expires".
* Claim/activation deadlines are modelled separately from redemption deadlines.
* Eligibility is four-valued. "Not stated" is not "satisfied".
* A provider failure is its own status, never an empty result set.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date as Date
from datetime import datetime as DateTime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

SCHEMA_VERSION = "offer-schema-v1"

Minor = Annotated[int, Field(ge=0, description="Integer amount in the currency's minor unit")]


class Strict(BaseModel):
    """Base model: reject unknown keys so provider drift surfaces as an error."""

    model_config = ConfigDict(extra="forbid", frozen=False, validate_assignment=True)


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class ParseStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NEEDS_VISUAL = "needs_visual"
    FAILED = "failed"


class TimeStatus(StrEnum):
    EXPIRED = "expired"
    UPCOMING = "upcoming"
    WITHIN_STATED_WINDOW = "within_stated_window"
    UNKNOWN = "unknown"
    CONFLICTING = "conflicting"


class EligibilityStatus(StrEnum):
    CONFIRMED = "confirmed"
    CONDITIONAL = "conditional"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


class UserStatus(StrEnum):
    UNUSED_OR_UNKNOWN = "unused_or_unknown"
    USED = "used"
    DISMISSED = "dismissed"
    SAVED = "saved"
    PLANNED = "planned"


class TriState(StrEnum):
    """Four-valued condition flag. ``UNKNOWN`` never counts as satisfied."""

    KNOWN_YES = "known_yes"
    KNOWN_NO = "known_no"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class Channel(StrEnum):
    DINE_IN = "dine_in"
    PICKUP = "pickup"
    DELIVERY = "delivery"
    ONLINE = "online"
    RETAIL = "retail"
    UNKNOWN = "unknown"


class FoodCategory(StrEnum):
    RESTAURANT = "restaurant"
    DRINK = "drink"
    DELIVERY = "delivery"
    GROCERY = "grocery"
    MIXED = "mixed"
    OTHER_OR_UNCLEAR = "other_or_unclear"


class BenefitKind(StrEnum):
    AMOUNT_OFF = "amount_off"
    PERCENT_OFF = "percent_off"
    FIXED_PRICE = "fixed_price"
    BOGO = "bogo"
    FREE_ITEM = "free_item"
    LOYALTY_POINTS = "loyalty_points"
    OTHER = "other"


class Route(StrEnum):
    EXTRACT = "extract"
    EXTRACT_WITH_REVIEW_FLAG = "extract_with_review_flag"
    PROVISIONAL_REJECT = "provisional_reject"
    NEEDS_RICHER_INPUT = "needs_richer_input"
    FALLBACK_PENDING = "fallback_pending"


class ExtractionStatus(StrEnum):
    SUCCESS = "success"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class DateKind(StrEnum):
    DATE_ONLY = "date_only"
    DATETIME = "datetime"
    RELATIVE = "relative"
    RECURRING = "recurring"
    UNKNOWN = "unknown"


class DateConfidence(StrEnum):
    EXPLICIT_DATETIME = "explicit_datetime"
    EXPLICIT_DATE_MISSING_TIME = "explicit_date_missing_time"
    RELATIVE_RESOLVED = "relative_resolved"
    RELATIVE_AMBIGUOUS = "relative_ambiguous"
    ABSENT = "absent"
    CONFLICTING = "conflicting"


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


class TemporalPoint(Strict):
    """A date as the email stated it, with its uncertainty preserved.

    A date-only value is never silently promoted to 23:59:59. Callers that need
    a hard instant must consult ``confidence_state`` first.
    """

    raw_expression: str | None = None
    kind: DateKind = DateKind.UNKNOWN
    date: Date | None = None
    datetime: DateTime | None = None
    timezone: str | None = None
    timezone_source: Literal["explicit", "sender_header", "assumed_display", "unknown"] = "unknown"
    inclusive: bool | None = None
    anchor: Date | None = Field(
        default=None, description="Resolution anchor for relative expressions such as 'tomorrow'"
    )
    confidence_state: DateConfidence = DateConfidence.ABSENT

    @model_validator(mode="after")
    def _coherent(self) -> TemporalPoint:
        if self.datetime is not None and self.date is None:
            object.__setattr__(self, "date", self.datetime.date())
        if self.date is None and self.datetime is None:
            if self.confidence_state not in (
                DateConfidence.ABSENT,
                DateConfidence.RELATIVE_AMBIGUOUS,
                DateConfidence.CONFLICTING,
            ):
                raise ValueError("A resolved date confidence requires an actual date value")
        return self

    @property
    def is_resolved(self) -> bool:
        return self.date is not None


class TimeWindow(Strict):
    """Time-of-day restriction, e.g. happy hour 15:00-18:00."""

    start_minute: int = Field(ge=0, le=1440)
    end_minute: int = Field(ge=0, le=1440)
    label: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> TimeWindow:
        if self.end_minute <= self.start_minute:
            raise ValueError("time window end must be after start")
        return self


class TemporalRules(Strict):
    """Everything that controls *when* an offer may be redeemed or claimed."""

    starts: TemporalPoint = Field(default_factory=TemporalPoint)
    ends: TemporalPoint = Field(default_factory=TemporalPoint)
    claim_deadline: TemporalPoint = Field(default_factory=TemporalPoint)
    claim_required: TriState = TriState.UNKNOWN
    activation_required: TriState = TriState.UNKNOWN
    weekdays: list[int] = Field(default_factory=list, description="0=Monday .. 6=Sunday")
    time_windows: list[TimeWindow] = Field(default_factory=list)
    blackout_dates: list[Date] = Field(default_factory=list)

    @field_validator("weekdays")
    @classmethod
    def _valid_weekdays(cls, value: list[int]) -> list[int]:
        for day in value:
            if not 0 <= day <= 6:
                raise ValueError("weekday must be 0..6")
        return sorted(set(value))


# --------------------------------------------------------------------------
# Money and benefit
# --------------------------------------------------------------------------


class Money(Strict):
    """Integer minor units plus an explicit currency.

    Two ``Money`` values in different currencies are never combined. There is no
    conversion in v0.1 -- mixed-currency arithmetic raises.
    """

    minor: Minor
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    def __add__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(minor=self.minor + other.minor, currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(minor=max(0, self.minor - other.minor), currency=self.currency)

    def _same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(f"cannot combine {self.currency} with {other.currency}")

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(minor=0, currency=currency)

    def format(self, symbol_map: dict[str, str] | None = None) -> str:
        symbols = symbol_map or {"USD": "$", "EUR": "€", "GBP": "£", "CNY": "¥"}
        symbol = symbols.get(self.currency, f"{self.currency} ")
        return f"{symbol}{self.minor / 100:.2f}"


class Benefit(Strict):
    """What the offer gives. Face value only -- not realised savings."""

    kind: BenefitKind
    amount_off: Money | None = None
    percent_off: int | None = Field(default=None, ge=1, le=100)
    fixed_price: Money | None = None
    cap: Money | None = Field(default=None, description="Maximum discount; None means not stated")
    cap_stated_absent: bool = Field(
        default=False, description="True only when the email explicitly says 'no maximum'"
    )
    minimum_spend: Money | None = None
    promo_code: str | None = None
    free_item_description: str | None = None
    description: str = ""

    @model_validator(mode="after")
    def _kind_matches_payload(self) -> Benefit:
        if self.kind is BenefitKind.AMOUNT_OFF and self.amount_off is None:
            raise ValueError("amount_off benefit requires an amount_off value")
        if self.kind is BenefitKind.PERCENT_OFF and self.percent_off is None:
            raise ValueError("percent_off benefit requires a percent_off value")
        if self.kind is BenefitKind.FIXED_PRICE and self.fixed_price is None:
            raise ValueError("fixed_price benefit requires a fixed_price value")
        return self

    @property
    def currency(self) -> str | None:
        for value in (self.amount_off, self.fixed_price, self.minimum_spend, self.cap):
            if value is not None:
                return value.currency
        return None


class Eligibility(Strict):
    """Who may use the offer. Every flag defaults to UNKNOWN, never to satisfied."""

    new_customer_only: TriState = TriState.UNKNOWN
    membership_required: TriState = TriState.UNKNOWN
    membership_name: str | None = None
    targeted_account: TriState = TriState.UNKNOWN
    purchase_required: TriState = TriState.UNKNOWN
    participating_locations_only: TriState = TriState.UNKNOWN
    location_note: str | None = None
    usage_limit: int | None = Field(default=None, ge=1)
    one_per_account: TriState = TriState.UNKNOWN
    stackable: TriState = TriState.UNKNOWN
    exclusive_group: str | None = Field(
        default=None, description="Offers sharing a group are mutually exclusive alternatives"
    )
    channels: list[Channel] = Field(default_factory=lambda: [Channel.UNKNOWN])

    @field_validator("channels")
    @classmethod
    def _dedupe(cls, value: list[Channel]) -> list[Channel]:
        return list(dict.fromkeys(value)) or [Channel.UNKNOWN]


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


class Evidence(Strict):
    """A verbatim excerpt backing one field.

    ``verified`` is set by the validator after locating ``quote`` in the
    normalized text -- a model asserting a span does not make it true.
    """

    field_path: str
    message_id: str
    part_id: str | None = None
    quote: str
    span_start: int | None = None
    span_end: int | None = None
    verified: bool = False

    @field_validator("quote")
    @classmethod
    def _non_trivial(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 3:
            raise ValueError("evidence quote is too short to verify")
        return cleaned


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------


class EmailAttachmentHint(Strict):
    part_id: str
    mime_type: str
    filename: str | None = None
    is_image: bool = False
    alt_text: str | None = None


class NormalizedEmail(Strict):
    """A single message after MIME/HTML normalization.

    ``body_complete=False`` means downstream code must not treat an empty offer
    list as "this email had no offers".
    """

    source_id: str = Field(description="Stable provider message id")
    thread_id: str | None = None
    account_alias: str = "default"
    subject: str = ""
    sender: str = ""
    sender_date: DateTime | None = Field(default=None, description="From the Date header")
    received_date: DateTime | None = Field(default=None, description="Provider receive time")
    date_provenance: Literal["header", "internal", "both", "unknown"] = "unknown"
    normalized_text: str = ""
    structured_markup: list[dict[str, Any]] = Field(
        default_factory=list, description="Parsed JSON-LD / microdata; data only, never executed"
    )
    parts: list[EmailAttachmentHint] = Field(default_factory=list)
    body_complete: bool = True
    has_unparsed_visuals: bool = False
    truncated: bool = False
    parse_status: ParseStatus = ParseStatus.COMPLETE
    parse_notes: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        payload = f"{self.source_id}\x00{self.subject}\x00{self.normalized_text}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def safe_for_negative_conclusion(self) -> bool:
        """Whether 'no offers found' can be trusted for this email."""
        return self.body_complete and not self.has_unparsed_visuals and not self.truncated


class PromotionEvent(Strict):
    """A lightweight calendar entry for any promotion email.

    This is intentionally separate from ``ValidatedOffer``: a promotion can
    be worth a reminder even when it is not a food offer or lacks enough terms
    for coupon planning.
    """

    promotion_id: str
    message_id: str
    merchant: str
    title: str
    category: Literal["food", "travel", "events", "retail", "services", "other"]
    start_date: Date | None = None
    end_date: Date | None = None
    date_expression: str | None = None
    benefit_hint: str | None = None
    status: Literal["expired", "ending_soon", "active", "upcoming", "unknown"] = "unknown"
    evidence: str | None = None
    source_date: DateTime | None = None
    needs_review: bool = False
    body_complete: bool = True
    has_unparsed_visuals: bool = False
    source_message_ids: list[str] = Field(default_factory=list)
    source_accounts: list[str] = Field(default_factory=list)
    duplicate_count: int = 1
    deadline_conflict: bool = False
    dedup_note: str | None = None


class MessageRef(Strict):
    source_id: str
    thread_id: str | None = None
    received_date: DateTime | None = None


class MessagePage(Strict):
    """One page of search results.

    ``is_complete`` is only true once the provider returned a terminal page.
    Nothing may claim full coverage without it.
    """

    items: list[MessageRef] = Field(default_factory=list)
    next_cursor: str | None = None
    is_complete: bool = False

    @model_validator(mode="after")
    def _cursor_consistency(self) -> MessagePage:
        if self.is_complete and self.next_cursor is not None:
            raise ValueError("a complete page cannot carry a next_cursor")
        return self


class MailCapabilities(Strict):
    supports_incremental_history: bool = False
    supports_full_body: bool = True
    max_page_size: int = 100
    provider: str = "unknown"
    supports_concurrent_fetch: bool = Field(
        default=False,
        description="Whether fetch() may be called from several threads at once. "
        "Defaults to False: a source must opt in, because a client library that "
        "shares one HTTP connection will corrupt responses rather than fail loudly.",
    )


# --------------------------------------------------------------------------
# Model call bookkeeping
# --------------------------------------------------------------------------


class Usage(Strict):
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    cost_known: bool = Field(
        default=False, description="False when the provider returned no usage; never record 0"
    )


class ProviderMeta(Strict):
    """Attached to every model result. Never contains credentials."""

    provider: str
    model: str
    prompt_version: str | None = None
    schema_version: str = SCHEMA_VERSION
    latency_ms: int | None = None
    usage: Usage = Field(default_factory=Usage)
    cached: bool = False
    retry_count: int = 0


class ClassificationResult(Strict):
    message_id: str
    body_hash: str
    contains_food_offer: float | None = Field(default=None, ge=0.0, le=1.0)
    contains_promotion: float | None = Field(default=None, ge=0.0, le=1.0)
    food_category: FoodCategory | None = None
    promotion_category: str | None = None
    category_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    route: Route = Route.EXTRACT
    error_code: str | None = None
    meta: ProviderMeta
    reasons: list[str] = Field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.error_code is not None


# --------------------------------------------------------------------------
# Offers
# --------------------------------------------------------------------------


class OfferDraft(Strict):
    """Raw model output for one benefit, before deterministic validation."""

    merchant: str
    merchant_confidence: Literal["explicit", "inferred", "unknown"] = "unknown"
    food_category: FoodCategory = FoodCategory.OTHER_OR_UNCLEAR
    title: str = ""
    benefit: Benefit
    temporal: TemporalRules = Field(default_factory=TemporalRules)
    eligibility: Eligibility = Field(default_factory=Eligibility)
    alternative_group: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    needs_visual_parse: bool = False


class ExtractionResult(Strict):
    """Extractor output.

    ``offers == []`` only means "no offers" when ``status is SUCCESS``. A refusal,
    a timeout or invalid JSON is FAILED and must not be read as a negative label.
    """

    message_id: str
    status: ExtractionStatus
    offers: list[OfferDraft] = Field(default_factory=list)
    error_code: str | None = None
    meta: ProviderMeta

    @model_validator(mode="after")
    def _failure_has_no_silent_offers(self) -> ExtractionResult:
        if self.status is ExtractionStatus.FAILED and self.error_code is None:
            raise ValueError("a failed extraction must carry an error_code")
        return self


class ValidatedOffer(Strict):
    """A draft after deterministic validation, with derived state attached."""

    offer_id: str
    version: int = 1
    merchant: str
    food_category: FoodCategory
    title: str
    benefit: Benefit
    temporal: TemporalRules
    eligibility: Eligibility
    alternative_group: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    source_message_ids: list[str] = Field(default_factory=list)
    time_status: TimeStatus = TimeStatus.UNKNOWN
    eligibility_status: EligibilityStatus = EligibilityStatus.UNKNOWN
    parse_status: ParseStatus = ParseStatus.COMPLETE
    validation_notes: list[str] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)

    @property
    def evidence_verified(self) -> bool:
        return bool(self.evidence) and all(item.verified for item in self.evidence)

    @property
    def actionable(self) -> bool:
        """Only offers that pass every hard gate may enter a committed plan."""
        return (
            self.time_status is TimeStatus.WITHIN_STATED_WINDOW
            and self.eligibility_status is EligibilityStatus.CONFIRMED
            and self.parse_status is ParseStatus.COMPLETE
            and not self.conflicts
        )


class OfferUserState(Strict):
    """User-owned state. A re-sync must never overwrite these fields."""

    offer_id: str
    status: UserStatus = UserStatus.UNUSED_OR_UNKNOWN
    used_at: DateTime | None = None
    dismissed_at: DateTime | None = None
    saved: bool = False
    planned_date: Date | None = None
    eligibility_overrides: dict[str, TriState] = Field(default_factory=dict)
    note: str | None = None
    #: Bumped on every write. Exposed on every read so a caller that wants the
    #: optimistic-concurrency guard can actually supply a value for it -- before,
    #: the guard existed but no read path returned the number to pass.
    revision: int = 0
    updated_at: DateTime | None = None


# --------------------------------------------------------------------------
# Planning and reporting
# --------------------------------------------------------------------------


class CostEstimate(Strict):
    """What the user would actually pay. Absent inputs stay absent."""

    basket: Money | None = None
    discount: Money | None = None
    out_of_pocket: Money | None = None
    relative_savings: Money | None = None
    #: True when the offer would cost MORE than the comparable alternative.
    #: `relative_savings` stays None in that case -- Money is unsigned, and a
    #: clamped 0 reads as "saved nothing" rather than "spend more".
    exceeds_baseline: bool = False
    unknown_components: list[str] = Field(default_factory=list)
    computable: bool = False
    note: str | None = None


class PlanItem(Strict):
    offer_id: str
    merchant: str
    title: str
    slot_date: Date | None = None
    bucket: Literal["this_week", "next_week", "this_month", "needs_confirmation"]
    reason_codes: list[str] = Field(default_factory=list)
    cost: CostEstimate = Field(default_factory=CostEstimate)
    deadline_note: str | None = None
    unknowns: list[str] = Field(default_factory=list)
    offer_version: int = 1
    locked: bool = False


class Coverage(Strict):
    """Scan accounting. Distinguishes 'nothing found' from 'did not finish'."""

    query: str = ""
    lookback_days: int = 0
    range_start: Date | None = None
    range_end: Date | None = None
    search_exhaustive: bool = False
    messages_matched: int = 0
    messages_fetched: int = 0
    fetch_failures: int = 0
    unparsed_visuals: int = 0
    classified: int = 0
    classification_failures: int = 0
    extraction_attempts: int = 0
    extraction_cached: int = 0
    extraction_success: int = 0
    extraction_failures: int = 0
    offers_validated: int = 0
    offers_after_dedup: int = 0
    promotions_indexed: int = 0
    last_sync_at: DateTime | None = None

    #: Messages left unprocessed because the run stopped early (budget cap,
    #: provider outage). They are neither successes nor failures, and a report
    #: that omits them overstates what was covered.
    parked: int = 0

    @property
    def is_partial(self) -> bool:
        return (
            not self.search_exhaustive
            or self.fetch_failures > 0
            or self.classification_failures > 0
            or self.extraction_failures > 0
            or self.parked > 0
        )


class Preferences(Strict):
    timezone: str = "America/Chicago"
    display_language: str = "zh-CN"
    currency: str = "USD"
    per_meal_budget_minor: int | None = Field(default=None, ge=0)
    weekly_dining_budget_minor: int | None = Field(default=None, ge=0)
    max_dining_out_per_week: int | None = Field(default=None, ge=0)
    party_size: int | None = Field(default=None, ge=1)
    preferred_merchants: list[str] = Field(default_factory=list)
    excluded_merchants: list[str] = Field(default_factory=list)
    allowed_channels: list[Channel] = Field(default_factory=list)
    confirmed_memberships: list[str] = Field(default_factory=list)
    planned_meal_slots: list[Date] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def preferences_hash(self) -> str:
        # Built from declared fields only: calling model_dump here would recurse
        # through this computed field.
        data = {name: getattr(self, name) for name in type(self).model_fields}
        payload = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class PlanResult(Strict):
    plan_id: str
    as_of: DateTime
    timezone: str
    this_week: list[PlanItem] = Field(default_factory=list)
    next_week: list[PlanItem] = Field(default_factory=list)
    this_month: list[PlanItem] = Field(default_factory=list)
    needs_confirmation: list[PlanItem] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)
    preferences_hash: str = ""
    notes: list[str] = Field(default_factory=list)

    @property
    def total_items(self) -> int:
        return (
            len(self.this_week)
            + len(self.next_week)
            + len(self.this_month)
            + len(self.needs_confirmation)
        )


class RunRecord(Strict):
    run_id: str
    scope: str
    mode: str
    started_at: DateTime
    finished_at: DateTime | None = None
    status: Literal["running", "completed", "partial", "failed"] = "running"
    coverage: Coverage = Field(default_factory=Coverage)
    error: str | None = None
    estimated_cost_usd: float = 0.0
    cost_known: bool = True
