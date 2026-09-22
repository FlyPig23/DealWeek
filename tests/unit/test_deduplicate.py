"""Deduplication.

Merging too eagerly loses a real offer and can detach the user's "already used"
flag. Merging too little only costs screen space. The tests encode that bias.
"""

from __future__ import annotations

import datetime as dt

from mealdeals.offers.deduplicate import deduplicate, merge_pair
from mealdeals.offers.validate import validate
from mealdeals.schemas import Channel, Eligibility

from ..conftest import make_draft, make_email

BODY = (
    "Take $4 off any lunch bowl when you spend $12 or more.\n"
    "Offer ends September 30, 2026. Pickup only."
)
END = dt.date(2026, 9, 30)


def offer(clock, preferences, *, message_id="m1", **kwargs):
    draft = make_draft(ends=END, quote="$4 off", message_id=message_id, **kwargs)
    return validate(draft, make_email(BODY, source_id=message_id), clock, preferences)


class TestMerging:
    def test_reminder_merges_into_one_offer(self, clock, preferences):
        original = offer(clock, preferences, message_id="m1")
        reminder = offer(clock, preferences, message_id="m2")
        result = deduplicate([original, reminder])
        assert len(result.offers) == 1
        assert result.merged_count == 1

    def test_merged_offer_keeps_both_sources(self, clock, preferences):
        result = deduplicate(
            [offer(clock, preferences, message_id="m1"), offer(clock, preferences, message_id="m2")]
        )
        assert set(result.offers[0].source_message_ids) == {"m1", "m2"}

    def test_merge_keeps_the_verified_copy_of_a_quote(self, clock, preferences):
        good = offer(clock, preferences, message_id="m1")
        bad = offer(clock, preferences, message_id="m1")
        bad.evidence[0] = bad.evidence[0].model_copy(update={"verified": False})
        merged = merge_pair(bad, good)
        amount = [e for e in merged.evidence if e.field_path == "benefit.amount_off"]
        assert any(e.verified for e in amount)


class TestNonMerging:
    def test_same_code_different_period_stays_separate(self, clock, preferences):
        september = offer(clock, preferences, message_id="m1")
        october_draft = make_draft(
            ends=dt.date(2026, 10, 31), quote="$4 off", message_id="m2"
        )
        october = validate(
            october_draft,
            make_email(
                "Take $4 off any lunch bowl when you spend $12 or more.\n"
                "Offer ends October 31, 2026. Pickup only.",
                source_id="m2",
            ),
            clock,
            preferences,
        )
        result = deduplicate([september, october])
        assert len(result.offers) == 2

    def test_same_offer_different_channel_stays_separate(self, clock, preferences):
        pickup = offer(
            clock, preferences, message_id="m1", eligibility=Eligibility(channels=[Channel.PICKUP])
        )
        delivery = offer(
            clock,
            preferences,
            message_id="m2",
            eligibility=Eligibility(channels=[Channel.DELIVERY]),
        )
        assert len(deduplicate([pickup, delivery]).offers) == 2

    def test_near_matches_are_flagged_rather_than_merged(self, clock, preferences):
        """Same merchant and value, different end date: report, do not guess."""
        september = offer(clock, preferences, message_id="m1")
        october_draft = make_draft(ends=dt.date(2026, 10, 31), quote="$4 off", message_id="m2")
        october = validate(
            october_draft,
            make_email(
                "Take $4 off any lunch bowl when you spend $12 or more.\n"
                "Offer ends October 31, 2026. Pickup only.",
                source_id="m2",
            ),
            clock,
            preferences,
        )
        result = deduplicate([september, october])
        assert len(result.suspected_duplicates) == 1

    def test_exclusive_alternatives_are_not_duplicates(self, clock, preferences):
        """Two 'pick one' options from the same email are distinct offers."""
        option_a = offer(clock, preferences, message_id="m1")
        option_b = offer(clock, preferences, message_id="m1")
        option_a = option_a.model_copy(update={"alternative_group": "g", "offer_id": "a"})
        option_b = option_b.model_copy(update={"alternative_group": "g", "offer_id": "b"})
        result = deduplicate([option_a, option_b])
        assert len(result.offers) == 2
        assert result.suspected_duplicates == []


class TestOrdering:
    def test_soonest_deadline_first_unknown_last(self, clock, preferences):
        soon = offer(clock, preferences, message_id="m1")
        undated_draft = make_draft(quote="$9 off", amount_minor=900, message_id="m3")
        undated = validate(
            undated_draft, make_email("Take $9 off today.", source_id="m3"), clock, preferences
        )
        result = deduplicate([undated, soon])
        assert result.offers[0].temporal.ends.date is not None
        assert result.offers[-1].temporal.ends.date is None
