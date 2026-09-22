"""Deterministic validation of model output.

A draft that satisfies the JSON schema is still just a claim. This module is
what turns a claim into a fact:

* every evidence quote must be locatable in the email's normalized text;
* numeric relationships must be coherent (a cap below the face value, a minimum
  spend of zero, a percentage over 100 are all rejected);
* an email that was not fully parsed can never produce a ``COMPLETE`` offer.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from ..clock import Clock
from ..schemas import (
    BenefitKind,
    EligibilityStatus,
    Evidence,
    Money,
    NormalizedEmail,
    OfferDraft,
    ParseStatus,
    Preferences,
    TemporalPoint,
    TimeStatus,
    ValidatedOffer,
)
from . import eligibility as eligibility_rules
from . import temporal

_WHITESPACE = re.compile(r"\s+")
# Fields whose values must be backed by a verbatim quote before we trust them.
CRITICAL_FIELDS = ("benefit.amount_off", "benefit.percent_off", "temporal.ends")


def normalize_for_match(text: str) -> str:
    """Fold whitespace, case and unicode width so quoting survives HTML cleanup."""
    folded = unicodedata.normalize("NFKC", text)
    folded = folded.replace("’", "'").replace("“", '"').replace("”", '"')
    folded = folded.replace("–", "-").replace("—", "-")
    return _WHITESPACE.sub(" ", folded).strip().lower()


def verify_evidence(item: Evidence, haystack: str) -> Evidence:
    """Locate a quote in the normalized body. Model-supplied spans are ignored."""
    needle = normalize_for_match(item.quote)
    if not needle:
        return item.model_copy(update={"verified": False})
    index = haystack.find(needle)
    if index < 0:
        return item.model_copy(update={"verified": False, "span_start": None, "span_end": None})
    return item.model_copy(
        update={"verified": True, "span_start": index, "span_end": index + len(needle)}
    )


def stable_offer_id(draft: OfferDraft) -> str:
    """Identity of a *logical* promotion, stable across reminder emails.

    Built only from campaign-identifying attributes. The message id is
    deliberately excluded: the same campaign re-sent must land on the same id so
    the user's "already used" flag survives.
    """
    benefit = draft.benefit
    eligibility = draft.eligibility

    def money(value: Money | None) -> str:
        # Currency belongs in the key: 400 EUR and 400 USD are not one campaign.
        return f"{value.minor}{value.currency}" if value else ""

    parts = [
        draft.merchant.strip().lower(),
        str(benefit.kind),
        (benefit.promo_code or "").strip().lower(),
        money(benefit.amount_off),
        str(benefit.percent_off or ""),
        money(benefit.fixed_price),
        money(benefit.minimum_spend),
        # A 20% discount capped at $5 is a different promotion from the same
        # 20% capped at $50; without the cap they shared an id and merged.
        money(benefit.cap),
        # The whole benefit for FREE_ITEM and BOGO lives in prose. Leaving it out
        # collapsed "free croissant" and "free cookie" from one merchant into a
        # single card, losing a real offer.
        (benefit.free_item_description or "").strip().lower(),
        draft.temporal.ends.date.isoformat() if draft.temporal.ends.date else "",
        ",".join(sorted(str(c) for c in eligibility.channels)),
        # Who may use it is part of what the offer *is*. A new-customers-only
        # variant must not merge with the general version of the same discount.
        str(eligibility.new_customer_only),
        str(eligibility.membership_required),
        (eligibility.membership_name or "").strip().lower(),
        str(eligibility.targeted_account),
        draft.alternative_group or "",
    ]
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:20]


def _check_numbers(draft: OfferDraft) -> list[str]:
    problems: list[str] = []
    benefit = draft.benefit

    if benefit.percent_off is not None and not 1 <= benefit.percent_off <= 100:
        problems.append("percentage discount outside 1-100")

    if benefit.amount_off is not None and benefit.amount_off.minor <= 0:
        problems.append("amount_off is zero or negative")

    if benefit.cap is not None and benefit.amount_off is not None:
        if benefit.cap.currency == benefit.amount_off.currency and (
            benefit.cap.minor < benefit.amount_off.minor
        ):
            problems.append("stated cap is lower than the stated discount")

    if benefit.minimum_spend is not None and benefit.amount_off is not None:
        if benefit.minimum_spend.currency == benefit.amount_off.currency and (
            benefit.minimum_spend.minor < benefit.amount_off.minor
        ):
            problems.append("minimum spend is lower than the discount - verify the terms")

    currencies = {
        value.currency
        for value in (benefit.amount_off, benefit.fixed_price, benefit.minimum_spend, benefit.cap)
        if value is not None
    }
    if len(currencies) > 1:
        problems.append(f"mixed currencies in one benefit: {sorted(currencies)}")

    if benefit.kind is BenefitKind.PERCENT_OFF and benefit.cap is None and not benefit.cap_stated_absent:
        # Not an error, but it must reach the user as an unknown.
        problems.append("percentage discount with no stated maximum")

    return problems


def validate(
    draft: OfferDraft,
    email: NormalizedEmail,
    clock: Clock,
    preferences: Preferences | None = None,
) -> ValidatedOffer:
    """Turn one draft into a validated offer with derived state attached."""
    preferences = preferences or Preferences()
    haystack = normalize_for_match(email.normalized_text)

    checked_evidence = [verify_evidence(item, haystack) for item in draft.evidence]
    unverified = [item.field_path for item in checked_evidence if not item.verified]

    notes: list[str] = []
    unresolved = list(draft.unresolved_fields)
    conflicts = list(draft.conflicts)

    if unverified:
        notes.append("evidence could not be located for: " + ", ".join(sorted(set(unverified))))

    covered = {item.field_path for item in checked_evidence if item.verified}
    for required in CRITICAL_FIELDS:
        has_value = _field_present(draft, required)
        if has_value and required not in covered:
            notes.append(f"{required} has no verified quote backing it")
            unresolved.append(required)

    notes.extend(_check_numbers(draft))

    # Temporal. The anchor for relative dates is the sender's own date, never
    # the day we happened to run the scan.
    anchor = None
    if email.sender_date is not None:
        anchor = email.sender_date.date()
    assessment = temporal.assess(draft.temporal, clock, anchor=anchor)
    if assessment.claim_unknown:
        unresolved.append("temporal.claim_deadline")
    if assessment.status is TimeStatus.CONFLICTING:
        conflicts.extend(assessment.reasons)

    eligibility_status, eligibility_unknowns = eligibility_rules.resolve(
        draft.eligibility, preferences
    )
    unresolved.extend(eligibility_unknowns)

    # Parse completeness gates everything downstream.
    if draft.needs_visual_parse or email.has_unparsed_visuals:
        parse_status = ParseStatus.NEEDS_VISUAL
        notes.append("key terms appear to be in an image that was not parsed")
    elif not email.body_complete or email.truncated:
        parse_status = ParseStatus.PARTIAL
        notes.append("the email body was incomplete when it was read")
    elif unverified:
        parse_status = ParseStatus.PARTIAL
    else:
        parse_status = ParseStatus.COMPLETE

    # An incompletely parsed email can never yield a confirmed offer.
    if parse_status is not ParseStatus.COMPLETE and eligibility_status is EligibilityStatus.CONFIRMED:
        eligibility_status = EligibilityStatus.CONDITIONAL
        unresolved.append("terms not fully parsed")

    return ValidatedOffer(
        offer_id=stable_offer_id(draft),
        merchant=draft.merchant,
        food_category=draft.food_category,
        title=draft.title or draft.benefit.description or draft.merchant,
        benefit=draft.benefit,
        temporal=draft.temporal,
        eligibility=draft.eligibility,
        alternative_group=draft.alternative_group,
        evidence=checked_evidence,
        source_message_ids=[email.source_id],
        time_status=assessment.status,
        eligibility_status=eligibility_status,
        parse_status=parse_status,
        validation_notes=notes,
        unresolved_fields=sorted(set(unresolved)),
        conflicts=sorted(set(conflicts)),
    )


def _field_present(draft: OfferDraft, path: str) -> bool:
    """Whether the draft actually asserts a value at ``path``.

    A ``TemporalPoint`` is always *there* even when the email stated no date, so
    a plain attribute walk reported ``temporal.ends`` as present for every offer
    and then demanded a quote for it -- filing "no deadline stated" offers with a
    bogus unresolved field on top of their real one.
    """
    node: object = draft
    for part in path.split("."):
        node = getattr(node, part, None)
        if node is None:
            return False
    if isinstance(node, TemporalPoint):
        return node.date is not None or bool(node.raw_expression)
    return True
