"""Validation: evidence, identity and parse completeness."""

from __future__ import annotations

import datetime as dt

from weekly_deals.offers import validate
from weekly_deals.schemas import (
    Benefit,
    BenefitKind,
    Eligibility,
    EligibilityStatus,
    Evidence,
    Money,
    OfferDraft,
    ParseStatus,
    TriState,
)

from ..conftest import make_draft, make_email

BODY = (
    "Take $4 off any lunch bowl when you spend $12 or more.\n"
    "Offer ends September 30, 2026. Pickup only."
)


class TestEvidence:
    def test_quote_present_in_body_is_verified(self, clock, preferences):
        email = make_email(BODY)
        draft = make_draft(ends=dt.date(2026, 9, 30), quote="$4 off")
        offer = validate.validate(draft, email, clock, preferences)
        amount_evidence = next(
            e for e in offer.evidence if e.field_path == "benefit.amount_off"
        )
        assert amount_evidence.verified
        assert amount_evidence.span_start is not None

    def test_fabricated_quote_is_not_verified(self, clock, preferences):
        """A quote the model invented must fail, whatever span it claims."""
        email = make_email(BODY)
        draft = make_draft(ends=dt.date(2026, 9, 30), quote="$40 off everything")
        offer = validate.validate(draft, email, clock, preferences)
        assert not offer.evidence_verified
        assert offer.parse_status is ParseStatus.PARTIAL
        assert any("could not be located" in note for note in offer.validation_notes)

    def test_matching_ignores_case_and_whitespace(self, clock, preferences):
        email = make_email("Take  $4   OFF any lunch bowl.")
        draft = make_draft(quote="$4 off")
        offer = validate.validate(draft, email, clock, preferences)
        assert offer.evidence[0].verified

    def test_model_supplied_spans_are_recomputed(self, clock, preferences):
        email = make_email(BODY)
        draft = make_draft(quote="$4 off")
        draft.evidence[0] = Evidence(
            field_path="benefit.amount_off",
            message_id="m1",
            quote="$4 off",
            span_start=9999,
            span_end=99999,
        )
        offer = validate.validate(draft, email, clock, preferences)
        assert offer.evidence[0].span_start != 9999


class TestOfferIdentity:
    def test_same_campaign_from_two_emails_gets_one_id(self):
        """A reminder must land on the same id so user state survives."""
        original = make_draft(ends=dt.date(2026, 9, 30), message_id="m1")
        reminder = make_draft(ends=dt.date(2026, 9, 30), message_id="m2")
        assert validate.stable_offer_id(original) == validate.stable_offer_id(reminder)

    def test_different_end_date_gets_a_different_id(self):
        """Same code, different period: a renewal, not the same campaign."""
        september = make_draft(ends=dt.date(2026, 9, 30))
        october = make_draft(ends=dt.date(2026, 10, 31))
        assert validate.stable_offer_id(september) != validate.stable_offer_id(october)

    def test_different_amount_gets_a_different_id(self):
        assert validate.stable_offer_id(make_draft(amount_minor=400)) != validate.stable_offer_id(
            make_draft(amount_minor=600)
        )


class TestNumericChecks:
    def test_cap_below_discount_is_flagged(self, clock, preferences):
        draft = OfferDraft(
            merchant="X",
            benefit=Benefit(
                kind=BenefitKind.AMOUNT_OFF,
                amount_off=Money(minor=1000, currency="USD"),
                cap=Money(minor=500, currency="USD"),
            ),
        )
        offer = validate.validate(draft, make_email("ten dollars off"), clock, preferences)
        assert any("cap is lower" in note for note in offer.validation_notes)

    def test_mixed_currency_benefit_is_flagged(self, clock, preferences):
        draft = OfferDraft(
            merchant="X",
            benefit=Benefit(
                kind=BenefitKind.AMOUNT_OFF,
                amount_off=Money(minor=400, currency="USD"),
                minimum_spend=Money(minor=1200, currency="EUR"),
            ),
        )
        offer = validate.validate(draft, make_email("body"), clock, preferences)
        assert any("mixed currencies" in note for note in offer.validation_notes)


class TestParseCompleteness:
    def test_image_only_email_can_never_be_complete(self, clock, preferences):
        email = make_email("Free scoop", has_unparsed_visuals=True)
        offer = validate.validate(make_draft(quote="Free scoop"), email, clock, preferences)
        assert offer.parse_status is ParseStatus.NEEDS_VISUAL
        assert not offer.actionable

    def test_truncated_email_downgrades_confirmed_eligibility(self, clock, preferences):
        email = make_email(BODY, truncated=True)
        offer = validate.validate(
            make_draft(ends=dt.date(2026, 9, 30), quote="$4 off"), email, clock, preferences
        )
        assert offer.parse_status is ParseStatus.PARTIAL
        assert offer.eligibility_status is not EligibilityStatus.CONFIRMED

    def test_clean_email_is_actionable(self, clock, preferences):
        email = make_email(BODY)
        offer = validate.validate(
            make_draft(ends=dt.date(2026, 9, 30), quote="$4 off"), email, clock, preferences
        )
        assert offer.parse_status is ParseStatus.COMPLETE
        assert offer.actionable


class TestEligibilityIntegration:
    def test_membership_requirement_makes_it_conditional(self, clock, preferences):
        draft = make_draft(
            ends=dt.date(2026, 9, 30),
            quote="$4 off",
            eligibility=Eligibility(
                membership_required=TriState.KNOWN_YES, membership_name="rewards"
            ),
        )
        offer = validate.validate(draft, make_email(BODY), clock, preferences)
        assert offer.eligibility_status is EligibilityStatus.CONDITIONAL
        assert not offer.actionable

    def test_confirmed_membership_clears_it(self, clock):
        from weekly_deals.schemas import Preferences

        draft = make_draft(
            ends=dt.date(2026, 9, 30),
            quote="$4 off",
            eligibility=Eligibility(
                membership_required=TriState.KNOWN_YES, membership_name="rewards"
            ),
        )
        prefs = Preferences(currency="USD", confirmed_memberships=["Rewards"])
        offer = validate.validate(draft, make_email(BODY), clock, prefs)
        assert offer.eligibility_status is EligibilityStatus.CONFIRMED

    def test_boilerplate_location_note_does_not_block(self, clock, preferences):
        """'Participating locations' is on nearly every real offer.

        If it downgraded eligibility, the planner would recommend nothing.
        """
        draft = make_draft(
            ends=dt.date(2026, 9, 30),
            quote="$4 off",
            eligibility=Eligibility(participating_locations_only=TriState.KNOWN_YES),
        )
        offer = validate.validate(draft, make_email(BODY), clock, preferences)
        assert offer.eligibility_status is EligibilityStatus.CONFIRMED
        assert any("participating" in item for item in offer.unresolved_fields)

    def test_silence_about_membership_is_not_an_unknown(self, clock, preferences):
        draft = make_draft(ends=dt.date(2026, 9, 30), quote="$4 off")
        offer = validate.validate(draft, make_email(BODY), clock, preferences)
        assert not any("membership" in item for item in offer.unresolved_fields)
