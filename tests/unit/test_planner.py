"""Planner.

These are the scenarios from the development plan's section 10.5, written as
executable checks. The one that matters most is
:meth:`TestBudget.test_never_recommends_spending_more_to_use_a_coupon` -- a tool
that pushes a $30 basket at someone whose usual lunch is $12 has made them
poorer while claiming to save them money.
"""

from __future__ import annotations

import datetime as dt

from mealdeals.offers.validate import validate
from mealdeals.planning.explanations import ReasonCode
from mealdeals.planning.planner import Planner
from mealdeals.schemas import (
    Eligibility,
    OfferUserState,
    Preferences,
    TriState,
    UserStatus,
)

from ..conftest import make_draft, make_email


def build(clock, preferences, *, message_id="m1", body=None, **kwargs):
    ends = kwargs.get("ends")
    text = body or (
        "Take $4 off any lunch bowl when you spend $12 or more.\n"
        + (f"Offer ends {ends.strftime('%B %d, %Y')}.\n" if ends else "")
        + "Pickup only."
    )
    draft = make_draft(quote="$4 off", message_id=message_id, **kwargs)
    return validate(draft, make_email(text, source_id=message_id), clock, preferences)


def plan_for(clock, offers, preferences=None, states=None):
    return Planner(clock, preferences or Preferences(currency="USD")).build(
        offers, states or {}
    )


def codes(items) -> set[str]:
    return {code for item in items for code in item.reason_codes}


class TestScarcityOrdering:
    def test_sooner_deadline_is_scheduled_before_a_longer_one(self, clock, today, preferences):
        """A expires Sunday, B at month end, similar value: A goes first."""
        sunday = today + dt.timedelta(days=(6 - today.weekday()))
        month_end = today + dt.timedelta(days=30)
        soon = build(clock, preferences, message_id="m1", ends=sunday)
        later = build(clock, preferences, message_id="m2", ends=month_end, amount_minor=450)

        prefs = Preferences(currency="USD", max_dining_out_per_week=1)
        result = plan_for(clock, [later, soon], prefs)

        assert len(result.this_week) == 1
        assert result.this_week[0].offer_id == soon.offer_id
        assert ReasonCode.WEEKLY_LIMIT_REACHED in codes(result.this_month + result.next_week)


class TestBudget:
    def test_never_recommends_spending_more_to_use_a_coupon(self, clock, today):
        """Coupon needs a $30 basket; the user normally spends $12."""
        offer = build(
            clock,
            Preferences(currency="USD"),
            ends=today + dt.timedelta(days=1),
            minimum_minor=3000,
            body=(
                "Take $4 off any lunch bowl when you spend $30 or more.\n"
                f"Offer ends {(today + dt.timedelta(days=1)).strftime('%B %d, %Y')}.\n"
                "Pickup only."
            ),
        )
        prefs = Preferences(currency="USD", per_meal_budget_minor=1200)
        result = plan_for(clock, [offer], prefs)

        assert result.this_week == []
        assert ReasonCode.MIN_SPEND_ABOVE_HABIT in codes(result.needs_confirmation)

    def test_minimum_within_budget_is_planned(self, clock, today):
        offer = build(clock, Preferences(currency="USD"), ends=today + dt.timedelta(days=2))
        prefs = Preferences(currency="USD", per_meal_budget_minor=2000)
        result = plan_for(clock, [offer], prefs)
        assert len(result.this_week) == 1

    def test_no_budget_means_no_optimality_claim(self, clock, today, preferences):
        offer = build(clock, preferences, ends=today + dt.timedelta(days=2))
        result = plan_for(clock, [offer], preferences)
        assert any("预算" in note for note in result.notes)


class TestWeekBoundaries:
    def test_tuesday_only_coupon_lands_on_tuesday_only(self, clock, today, preferences):
        offer = build(
            clock, preferences, ends=today + dt.timedelta(days=30), weekdays=[1]
        )
        result = plan_for(clock, [offer], preferences)
        scheduled = result.this_week + result.next_week
        assert scheduled
        for item in scheduled:
            assert item.slot_date is None or item.slot_date.weekday() == 1

    def test_offer_without_end_date_is_never_promised_for_next_week(
        self, clock, preferences
    ):
        offer = build(clock, preferences, body="Take $4 off any lunch bowl. Pickup only.")
        result = plan_for(clock, [offer], preferences)
        assert result.next_week == []
        assert ReasonCode.UNKNOWN_DEADLINE in codes(result.needs_confirmation)


class TestPrerequisites:
    def test_claim_deadline_surfaces_before_the_redemption_window(
        self, clock, today, preferences
    ):
        """Redeemable until month end, but must be claimed this Friday."""
        claim_by = today + dt.timedelta(days=2)
        month_end = today + dt.timedelta(days=25)
        offer = build(
            clock,
            preferences,
            ends=month_end,
            claim=claim_by,
            claim_required=True,
            body=(
                f"Take $4 off any lunch bowl when you spend $12 or more.\n"
                f"Claim by {claim_by.strftime('%B %d, %Y')}; "
                f"Offer ends {month_end.strftime('%B %d, %Y')}.\nPickup only."
            ),
        )
        result = plan_for(clock, [offer], preferences)
        # A step the user still has to take makes this a *conditional* candidate:
        # it is surfaced with its claim deadline, but it does not take a
        # committed slot, because nobody has claimed it yet.
        assert result.this_week == []
        assert ReasonCode.CLAIM_FIRST in codes(result.needs_confirmation)
        assert any(
            claim_by.isoformat() in u for item in result.needs_confirmation for u in item.unknowns
        )

    def test_claimed_offer_may_take_a_slot(self, clock, today, preferences):
        """Once the user says they have claimed it, it can be planned."""
        claim_by = today + dt.timedelta(days=2)
        month_end = today + dt.timedelta(days=25)
        offer = build(
            clock,
            preferences,
            ends=month_end,
            claim=claim_by,
            claim_required=True,
            body=(
                f"Take $4 off any lunch bowl when you spend $12 or more.\n"
                f"Claim by {claim_by.strftime('%B %d, %Y')}; "
                f"Offer ends {month_end.strftime('%B %d, %Y')}.\nPickup only."
            ),
        )
        states = {
            offer.offer_id: OfferUserState(offer_id=offer.offer_id, status=UserStatus.SAVED)
        }
        result = Planner(clock, preferences).build([offer], states)
        assert [item.offer_id for item in result.this_week] == [offer.offer_id]

    def test_expired_claim_deadline_removes_it_from_the_plan(self, clock, today, preferences):
        offer = build(
            clock,
            preferences,
            ends=today + dt.timedelta(days=25),
            claim=today - dt.timedelta(days=1),
            claim_required=True,
        )
        result = plan_for(clock, [offer], preferences)
        assert result.this_week == []
        assert ReasonCode.CLAIM_DEADLINE_PASSED in codes(result.needs_confirmation)


class TestUncertainty:
    def test_unknown_eligibility_never_enters_a_committed_plan(
        self, clock, today, preferences
    ):
        offer = build(
            clock,
            preferences,
            ends=today + dt.timedelta(days=3),
            eligibility=Eligibility(new_customer_only=TriState.KNOWN_YES),
        )
        result = plan_for(clock, [offer], preferences)
        assert result.this_week == []
        assert ReasonCode.UNKNOWN_ELIGIBILITY in codes(result.needs_confirmation)

    def test_image_only_offer_never_becomes_a_free_meal_promise(
        self, clock, today, preferences
    ):
        draft = make_draft(ends=today + dt.timedelta(days=3), quote="Free meal")
        offer = validate(
            draft,
            make_email("Free meal", has_unparsed_visuals=True),
            clock,
            preferences,
        )
        result = plan_for(clock, [offer], preferences)
        assert result.this_week == []
        assert ReasonCode.NEEDS_VISUAL_PARSE in codes(result.needs_confirmation)

    def test_unverified_evidence_blocks_scheduling(self, clock, today, preferences):
        draft = make_draft(ends=today + dt.timedelta(days=3), quote="$999 off everything")
        offer = validate(draft, make_email("Take $4 off."), clock, preferences)
        result = plan_for(clock, [offer], preferences)
        assert result.this_week == []


class TestUserState:
    def test_used_offer_is_not_recommended_again(self, clock, today, preferences):
        offer = build(clock, preferences, ends=today + dt.timedelta(days=3))
        states = {offer.offer_id: OfferUserState(offer_id=offer.offer_id, status=UserStatus.USED)}
        result = plan_for(clock, [offer], preferences, states)
        assert result.total_items == 0

    def test_dismissed_offer_disappears_quietly(self, clock, today, preferences):
        offer = build(clock, preferences, ends=today + dt.timedelta(days=3))
        states = {
            offer.offer_id: OfferUserState(offer_id=offer.offer_id, status=UserStatus.DISMISSED)
        }
        result = plan_for(clock, [offer], preferences, states)
        assert result.total_items == 0

    def test_locked_plan_keeps_its_date(self, clock, today, preferences):
        offer = build(clock, preferences, ends=today + dt.timedelta(days=5))
        chosen = today + dt.timedelta(days=4)
        states = {
            offer.offer_id: OfferUserState(
                offer_id=offer.offer_id, status=UserStatus.PLANNED, planned_date=chosen
            )
        }
        result = plan_for(clock, [offer], preferences, states)
        scheduled = result.this_week + result.next_week + result.this_month
        assert scheduled[0].slot_date == chosen
        assert scheduled[0].locked
        assert ReasonCode.USER_LOCKED in codes(scheduled)


class TestAlternatives:
    def test_only_one_of_two_exclusive_options_is_scheduled(self, clock, today, preferences):
        first = build(clock, preferences, message_id="m1", ends=today + dt.timedelta(days=3))
        second = build(
            clock, preferences, message_id="m2", ends=today + dt.timedelta(days=3), amount_minor=500
        )
        first = first.model_copy(update={"alternative_group": "pick-one"})
        second = second.model_copy(update={"alternative_group": "pick-one"})

        result = plan_for(clock, [first, second], preferences)
        assert len(result.this_week) == 1
        assert ReasonCode.ALTERNATIVE_CHOSEN in codes(result.this_month)


class TestEmptyPlan:
    def test_an_empty_week_is_a_valid_answer(self, clock, today, preferences):
        expired = build(clock, preferences, ends=today - dt.timedelta(days=5))
        result = plan_for(clock, [expired], preferences)
        assert result.this_week == []
        assert any("留空" in note for note in result.notes)

    def test_excluded_merchant_is_dropped(self, clock, today):
        offer = build(clock, Preferences(currency="USD"), ends=today + dt.timedelta(days=3))
        prefs = Preferences(currency="USD", excluded_merchants=["Test Merchant"])
        result = plan_for(clock, [offer], prefs)
        assert result.this_week == []
