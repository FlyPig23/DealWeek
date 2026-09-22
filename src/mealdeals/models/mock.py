"""Deterministic offline model.

This is not a small language model. It is a rule engine that produces the same
shapes a real extractor produces, so the entire pipeline -- validation, dedup,
planning, rendering -- can be exercised and regression-tested with no API key,
no network and no cost.

Because it is deterministic, it is also the reference for the contract tests:
any real adapter must produce structurally identical output for these inputs.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from ..schemas import (
    Benefit,
    BenefitKind,
    Channel,
    ClassificationResult,
    DateConfidence,
    DateKind,
    Eligibility,
    Evidence,
    ExtractionResult,
    ExtractionStatus,
    FoodCategory,
    Money,
    NormalizedEmail,
    OfferDraft,
    ProviderMeta,
    Route,
    TemporalPoint,
    TemporalRules,
    TriState,
    Usage,
)
from .base import FoodClassifier, OfferExtractor

_AMOUNT_OFF = re.compile(r"\$\s?(\d+(?:\.\d{2})?)\s*off", re.IGNORECASE)
_AMOUNT_OFF_ALT = re.compile(r"save\s+\$\s?(\d+(?:\.\d{2})?)", re.IGNORECASE)
_CNY_OFF = re.compile(r"减\s?(\d+(?:\.\d+)?)\s*元")
_PERCENT = re.compile(r"(\d{1,2})\s?%\s*off", re.IGNORECASE)
_MIN_SPEND = re.compile(
    # "spend $12 or more", "orders over $60", and the reminder-email phrasing
    # "with a $12 minimum" -- which a campaign restates differently from the
    # original send, and which must still resolve to the same offer identity.
    r"(?:spend|of|orders?\s+(?:over|of))\s+\$\s?(\d+(?:\.\d{2})?)\s*(?:or more)?"
    r"|\$\s?(\d+(?:\.\d{2})?)\s*minimum",
    re.IGNORECASE,
)
_CNY_MIN = re.compile(r"满\s?(\d+(?:\.\d+)?)\s*元")
_CAP = re.compile(r"maximum discount\s+\$\s?(\d+(?:\.\d{2})?)", re.IGNORECASE)
# The keyword is case-insensitive, the code itself is not: a reminder email
# writing "Code LUNCH4" must resolve to the same campaign as "code LUNCH4",
# otherwise the user's "already used" flag does not follow the offer.
_CODE = re.compile(r"\b(?i:code|coupon)\s+([A-Z0-9]{4,12})\b")
# Allows an item name between the halves: "buy one large pizza, get one free".
_BOGO = re.compile(r"buy one\b[\w ]{0,24}?[, ]+get one\b|\bbogo\b", re.IGNORECASE)
_FREE_ITEM = re.compile(r"free\s+([a-z ]{3,24}?)\s+with\s+any\s+([a-z ]{3,24})", re.IGNORECASE)
_ENDS = re.compile(
    r"(?:offer\s+)?(?:ends?|valid through|through|expires?)\s+"
    r"([A-Z][a-z]+ \d{1,2}, \d{4})",
    re.IGNORECASE,
)
_ENDED = re.compile(r"offer ended\s+([A-Z][a-z]+ \d{1,2}, \d{4})", re.IGNORECASE)
_CLAIM = re.compile(
    r"claim (?:this reward )?by\s+([A-Z][a-z]+ \d{1,2}, \d{4})", re.IGNORECASE
)
_REDEEM_THROUGH = re.compile(
    r"redeem it any time through\s+([A-Z][a-z]+ \d{1,2}, \d{4})", re.IGNORECASE
)
_CN_END = re.compile(r"活动截止\s*([A-Z][a-z]+ \d{1,2}, \d{4})")
_WEEKDAY = re.compile(
    r"\b(mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?)\s+only",
    re.IGNORECASE,
)
_EVERY_WEEKDAY = re.compile(
    r"every\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", re.IGNORECASE
)
_WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_FOOD_TERMS = (
    "lunch", "dinner", "breakfast", "meal", "menu", "entree", "pizza", "bowl",
    "coffee", "drink", "pastry", "dessert", "grocery", "groceries", "delivery",
    "restaurant", "cafe", "bakery", "scoop", "餐", "套餐", "咖啡", "外卖",
)
_OFFER_TERMS = (
    "off", "free", "bogo", "save", "discount", "coupon", "reward", "deal",
    "promo", "减", "优惠", "券", "立减",
)
_NON_OFFER_HINTS = ("introducing", "new menu", "we look forward", "now open")


def _money(value: str, currency: str = "USD") -> Money:
    return Money(minor=round(float(value) * 100), currency=currency)


def _parse_long_date(text: str) -> date | None:
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _point(raw: str, parsed: date | None) -> TemporalPoint:
    if parsed is None:
        return TemporalPoint(raw_expression=raw, confidence_state=DateConfidence.RELATIVE_AMBIGUOUS)
    return TemporalPoint(
        raw_expression=raw,
        kind=DateKind.DATE_ONLY,
        date=parsed,
        timezone_source="unknown",
        confidence_state=DateConfidence.EXPLICIT_DATE_MISSING_TIME,
    )


def _evidence(field_path: str, message_id: str, quote: str) -> Evidence:
    return Evidence(field_path=field_path, message_id=message_id, quote=quote.strip())


class MockClassifier(FoodClassifier):
    """Keyword triage with the same output shape as the JEV adapter."""

    prompt_version = "mock-classifier-v1"

    @property
    def model_id(self) -> str:
        return "mock-classifier"

    def classify(self, email: NormalizedEmail) -> ClassificationResult:
        text = email.normalized_text.lower()
        food_hits = sum(1 for term in _FOOD_TERMS if term in text)
        offer_hits = sum(1 for term in _OFFER_TERMS if term in text)

        score = min(0.99, 0.15 * food_hits + 0.2 * offer_hits)
        # Judged not to be an offer at all. Both answers then have to agree:
        # returning a low probability *and* a confident food category is a
        # contradiction, and the router is required to treat a contradiction as
        # uncertainty rather than discard it. An inconsistent mock made gate
        # mode look broken offline, because nothing was ever safe to reject.
        not_an_offer = any(hint in text for hint in _NON_OFFER_HINTS) and offer_hits <= 1
        if not_an_offer:
            score = min(score, 0.04)

        if not_an_offer:
            category = FoodCategory.OTHER_OR_UNCLEAR
        elif "coffee" in text or "drink" in text or "咖啡" in text:
            category = FoodCategory.DRINK
        elif "grocer" in text:
            category = FoodCategory.GROCERY
        elif "delivery" in text or "外卖" in text:
            category = FoodCategory.DELIVERY
        elif food_hits:
            category = FoodCategory.RESTAURANT
        else:
            category = FoodCategory.OTHER_OR_UNCLEAR

        return ClassificationResult(
            message_id=email.source_id,
            body_hash=email.content_hash,
            contains_food_offer=round(score, 4),
            food_category=category,
            category_confidence=round(min(0.95, 0.3 + 0.1 * food_hits), 4),
            route=Route.EXTRACT,
            meta=ProviderMeta(
                provider="mock",
                model="mock-classifier",
                prompt_version=self.prompt_version,
                latency_ms=0,
                usage=Usage(cost_known=True, estimated_cost_usd=0.0),
            ),
        )


class MockExtractor(OfferExtractor):
    """Rule-based extraction covering the shapes the fixtures exercise."""

    prompt_version = "mock-extractor-v1"

    @property
    def model_id(self) -> str:
        return "mock-extractor"

    def capabilities(self) -> dict[str, bool]:
        return {"structured_output": True, "vision": False, "usage_accounting": True}

    def extract(self, email: NormalizedEmail) -> ExtractionResult:
        meta = ProviderMeta(
            provider="mock",
            model="mock-extractor",
            prompt_version=self.prompt_version,
            latency_ms=0,
            usage=Usage(cost_known=True, estimated_cost_usd=0.0),
        )
        text = email.normalized_text

        # An image-only email yields a review flag, never a "free meal" claim.
        if email.has_unparsed_visuals:
            return ExtractionResult(
                message_id=email.source_id,
                status=ExtractionStatus.NEEDS_REVIEW,
                offers=[],
                error_code=None,
                meta=meta,
            )

        drafts = self._drafts(email, text)
        status = ExtractionStatus.SUCCESS
        if not email.safe_for_negative_conclusion and not drafts:
            status = ExtractionStatus.NEEDS_REVIEW

        return ExtractionResult(
            message_id=email.source_id, status=status, offers=drafts, meta=meta
        )

    # -- helpers -----------------------------------------------------------

    def _merchant(self, email: NormalizedEmail) -> str:
        sender = email.sender
        if "<" in sender:
            return sender.split("<", 1)[0].strip().strip('"') or "Unknown merchant"
        return sender.split("@")[0] or "Unknown merchant"

    def _temporal(self, text: str, message_id: str) -> tuple[TemporalRules, list[Evidence]]:
        evidence: list[Evidence] = []
        ends = TemporalPoint()
        claim = TemporalPoint()
        claim_required = TriState.UNKNOWN
        weekdays: list[int] = []

        ended = _ENDED.search(text)
        end_match = _REDEEM_THROUGH.search(text) or _ENDS.search(text) or _CN_END.search(text)
        if ended:
            ends = _point(ended.group(0), _parse_long_date(ended.group(1)))
            evidence.append(_evidence("temporal.ends", message_id, ended.group(0)))
        elif end_match:
            ends = _point(end_match.group(0), _parse_long_date(end_match.group(1)))
            evidence.append(_evidence("temporal.ends", message_id, end_match.group(0)))

        claim_match = _CLAIM.search(text)
        if claim_match:
            claim = _point(claim_match.group(0), _parse_long_date(claim_match.group(1)))
            claim_required = TriState.KNOWN_YES
            evidence.append(_evidence("temporal.claim_deadline", message_id, claim_match.group(0)))

        weekday_match = _WEEKDAY.search(text) or _EVERY_WEEKDAY.search(text)
        if weekday_match:
            name = weekday_match.group(1).lower().rstrip("s")
            if name in _WEEKDAY_INDEX:
                weekdays = [_WEEKDAY_INDEX[name]]
                evidence.append(_evidence("temporal.weekdays", message_id, weekday_match.group(0)))

        return (
            TemporalRules(
                ends=ends,
                claim_deadline=claim,
                claim_required=claim_required,
                weekdays=weekdays,
            ),
            evidence,
        )

    def _eligibility(self, text: str) -> Eligibility:
        lowered = text.lower()
        channels: list[Channel] = []
        if "pickup only" in lowered:
            channels.append(Channel.PICKUP)
        if "dine-in only" in lowered or "仅限堂食" in text:
            channels.append(Channel.DINE_IN)
        if "dine-in or pickup" in lowered:
            channels.extend([Channel.DINE_IN, Channel.PICKUP])
        if "delivery only" in lowered:
            channels.append(Channel.DELIVERY)
        if "online orders only" in lowered:
            channels.append(Channel.ONLINE)
        if not channels:
            channels.append(Channel.UNKNOWN)

        membership = TriState.UNKNOWN
        membership_name = None
        if "members only" in lowered or "rewards member" in lowered:
            membership = TriState.KNOWN_YES
            membership_name = "rewards"

        participating = (
            TriState.KNOWN_YES if "participating" in lowered else TriState.UNKNOWN
        )

        usage_limit = None
        if "one per customer" in lowered or "每人限用一次" in text:
            usage_limit = 1

        return Eligibility(
            membership_required=membership,
            membership_name=membership_name,
            participating_locations_only=participating,
            location_note="participating locations" if participating is TriState.KNOWN_YES else None,
            usage_limit=usage_limit,
            one_per_account=TriState.KNOWN_YES if usage_limit == 1 else TriState.UNKNOWN,
            stackable=TriState.KNOWN_NO if "cannot be combined" in lowered else TriState.UNKNOWN,
            channels=channels,
        )

    def _category(self, text: str) -> FoodCategory:
        lowered = text.lower()
        if "grocer" in lowered:
            return FoodCategory.GROCERY
        if "delivery" in lowered:
            return FoodCategory.DELIVERY
        if any(term in lowered for term in ("coffee", "drink", "pastry", "咖啡")):
            return FoodCategory.DRINK
        return FoodCategory.RESTAURANT

    def _drafts(self, email: NormalizedEmail, text: str) -> list[OfferDraft]:
        merchant = self._merchant(email)
        message_id = email.source_id
        rules, temporal_evidence = self._temporal(text, message_id)
        eligibility = self._eligibility(text)
        category = self._category(text)
        drafts: list[OfferDraft] = []

        min_spend = None
        min_match = _MIN_SPEND.search(text)
        if min_match:
            min_spend = _money(min_match.group(1) or min_match.group(2))
        cn_min = _CNY_MIN.search(text)
        if cn_min:
            min_spend = _money(cn_min.group(1), "CNY")

        code_match = _CODE.search(text)
        promo_code = code_match.group(1) if code_match else None

        # Alternatives presented as "pick one" are modelled as an exclusive group.
        exclusive = "cannot be combined" in text.lower() and "option a" in text.lower()
        group = f"{merchant}-choice" if exclusive else None

        for match in _AMOUNT_OFF.finditer(text):
            benefit = Benefit(
                kind=BenefitKind.AMOUNT_OFF,
                amount_off=_money(match.group(1)),
                minimum_spend=min_spend if min_spend and min_spend.currency == "USD" else None,
                promo_code=promo_code,
                description=match.group(0),
            )
            drafts.append(
                self._draft(
                    merchant, category, benefit, rules, eligibility, group,
                    [*temporal_evidence, _evidence("benefit.amount_off", message_id, match.group(0))],
                )
            )

        for match in _AMOUNT_OFF_ALT.finditer(text):
            benefit = Benefit(
                kind=BenefitKind.AMOUNT_OFF,
                amount_off=_money(match.group(1)),
                minimum_spend=min_spend if min_spend and min_spend.currency == "USD" else None,
                promo_code=promo_code,
                description=match.group(0),
            )
            drafts.append(
                self._draft(
                    merchant, category, benefit, rules, eligibility, group,
                    [*temporal_evidence, _evidence("benefit.amount_off", message_id, match.group(0))],
                )
            )

        for match in _CNY_OFF.finditer(text):
            benefit = Benefit(
                kind=BenefitKind.AMOUNT_OFF,
                amount_off=_money(match.group(1), "CNY"),
                minimum_spend=min_spend if min_spend and min_spend.currency == "CNY" else None,
                description=match.group(0),
            )
            drafts.append(
                self._draft(
                    merchant, category, benefit, rules, eligibility, group,
                    [*temporal_evidence, _evidence("benefit.amount_off", message_id, match.group(0))],
                )
            )

        for match in _PERCENT.finditer(text):
            cap_match = _CAP.search(text)
            benefit = Benefit(
                kind=BenefitKind.PERCENT_OFF,
                percent_off=int(match.group(1)),
                cap=_money(cap_match.group(1)) if cap_match else None,
                minimum_spend=min_spend if min_spend and min_spend.currency == "USD" else None,
                description=match.group(0),
            )
            extra = [_evidence("benefit.percent_off", message_id, match.group(0))]
            if cap_match:
                extra.append(_evidence("benefit.cap", message_id, cap_match.group(0)))
            drafts.append(
                self._draft(
                    merchant, category, benefit, rules, eligibility, group,
                    [*temporal_evidence, *extra],
                )
            )

        if _BOGO.search(text):
            match = _BOGO.search(text)
            assert match is not None
            benefit = Benefit(kind=BenefitKind.BOGO, description=match.group(0))
            drafts.append(
                self._draft(
                    merchant, category, benefit, rules, eligibility, group,
                    [*temporal_evidence, _evidence("benefit.kind", message_id, match.group(0))],
                )
            )

        for match in _FREE_ITEM.finditer(text):
            benefit = Benefit(
                kind=BenefitKind.FREE_ITEM,
                free_item_description=match.group(1).strip(),
                description=match.group(0),
            )
            drafts.append(
                self._draft(
                    merchant, category, benefit, rules, eligibility, group,
                    [*temporal_evidence, _evidence("benefit.kind", message_id, match.group(0))],
                )
            )

        return drafts

    def _draft(
        self,
        merchant: str,
        category: FoodCategory,
        benefit: Benefit,
        rules: TemporalRules,
        eligibility: Eligibility,
        group: str | None,
        evidence: list[Evidence],
    ) -> OfferDraft:
        unresolved: list[str] = []
        if not rules.ends.is_resolved:
            unresolved.append("temporal.ends")
        if benefit.kind is BenefitKind.PERCENT_OFF and benefit.cap is None:
            unresolved.append("benefit.cap")
        return OfferDraft(
            merchant=merchant,
            merchant_confidence="explicit",
            food_category=category,
            title=f"{merchant}: {benefit.description or str(benefit.kind)}",
            benefit=benefit,
            temporal=rules,
            eligibility=eligibility,
            alternative_group=group,
            evidence=evidence,
            unresolved_fields=unresolved,
        )
