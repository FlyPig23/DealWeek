"""Time boundaries.

The regression that matters most: an offer with no stated end date must never
be usable. "Not stated" is not "forever".
"""

from __future__ import annotations

import datetime as dt

import pytest

from weekly_deals.clock import FrozenClock, month_bounds, week_bounds
from weekly_deals.offers import temporal
from weekly_deals.schemas import (
    DateConfidence,
    TemporalPoint,
    TemporalRules,
    TimeStatus,
    TriState,
)

from ..conftest import point


def rules(**kwargs) -> TemporalRules:
    return TemporalRules(**kwargs)


class TestStatus:
    def test_within_window(self, clock, today):
        assessment = temporal.assess(rules(ends=point(today + dt.timedelta(days=3))), clock)
        assert assessment.status is TimeStatus.WITHIN_STATED_WINDOW

    def test_expired_yesterday(self, clock, today):
        assessment = temporal.assess(rules(ends=point(today - dt.timedelta(days=1))), clock)
        assert assessment.status is TimeStatus.EXPIRED

    def test_last_day_is_still_valid(self, clock, today):
        """An offer ending today has not expired yet."""
        assessment = temporal.assess(rules(ends=point(today)), clock)
        assert assessment.status is TimeStatus.WITHIN_STATED_WINDOW
        assert assessment.days_until_end == 0

    def test_not_started(self, clock, today):
        assessment = temporal.assess(
            rules(starts=point(today + dt.timedelta(days=2)), ends=point(today + dt.timedelta(days=9))),
            clock,
        )
        assert assessment.status is TimeStatus.UPCOMING

    def test_missing_end_date_is_unknown_not_forever(self, clock):
        """The central rule. An unstated deadline must not become open-ended."""
        assessment = temporal.assess(rules(), clock)
        assert assessment.status is TimeStatus.UNKNOWN
        assert assessment.status is not TimeStatus.WITHIN_STATED_WINDOW

    def test_reversed_dates_conflict(self, clock, today):
        assessment = temporal.assess(
            rules(starts=point(today + dt.timedelta(days=5)), ends=point(today + dt.timedelta(days=1))),
            clock,
        )
        assert assessment.status is TimeStatus.CONFLICTING


class TestClaimDeadline:
    def test_claim_is_separate_from_redemption(self, clock, today):
        """Claim by Friday, redeem until month end: both must survive."""
        assessment = temporal.assess(
            rules(
                ends=point(today + dt.timedelta(days=20)),
                claim_deadline=point(today + dt.timedelta(days=2)),
                claim_required=TriState.KNOWN_YES,
            ),
            clock,
        )
        assert assessment.status is TimeStatus.WITHIN_STATED_WINDOW
        assert assessment.claim_deadline == today + dt.timedelta(days=2)
        assert not assessment.claim_overdue

    def test_overdue_claim_flagged_while_window_open(self, clock, today):
        assessment = temporal.assess(
            rules(
                ends=point(today + dt.timedelta(days=20)),
                claim_deadline=point(today - dt.timedelta(days=1)),
                claim_required=TriState.KNOWN_YES,
            ),
            clock,
        )
        assert assessment.claim_overdue
        assert assessment.status is TimeStatus.WITHIN_STATED_WINDOW

    def test_claim_required_without_deadline_is_unknown(self, clock, today):
        assessment = temporal.assess(
            rules(ends=point(today + dt.timedelta(days=5)), claim_required=TriState.KNOWN_YES),
            clock,
        )
        assert assessment.claim_unknown


class TestUsableDays:
    def test_unknown_end_date_is_never_projected_forward(self, clock):
        """No stated end means no evidence it is live next week."""
        assessment = temporal.assess(rules(), clock)
        days = temporal.usable_days(rules(), week_bounds(clock, 1), assessment)
        assert days == []

    def test_weekday_restriction_limits_to_that_day(self, clock, today):
        offer = rules(ends=point(today + dt.timedelta(days=30)), weekdays=[1])  # Tuesdays
        assessment = temporal.assess(offer, clock)
        days = temporal.usable_days(offer, week_bounds(clock, 1), assessment)
        assert len(days) == 1
        assert days[0].weekday() == 1

    def test_blackout_date_removed(self, clock, today):
        blackout = today + dt.timedelta(days=1)
        offer = rules(ends=point(today + dt.timedelta(days=5)), blackout_dates=[blackout])
        assessment = temporal.assess(offer, clock)
        days = temporal.usable_days(offer, (today, today + dt.timedelta(days=5)), assessment)
        assert blackout not in days

    def test_expired_offer_has_no_usable_days(self, clock, today):
        offer = rules(ends=point(today - dt.timedelta(days=2)))
        assessment = temporal.assess(offer, clock)
        assert temporal.usable_days(offer, (today, today + dt.timedelta(days=7)), assessment) == []


class TestRelativeDates:
    def test_tomorrow_resolves_against_sender_date_not_scan_date(self, clock, today):
        """'Tomorrow' means the day after the email was sent, not after the scan."""
        sent = today - dt.timedelta(days=3)
        resolved = temporal.resolve_relative(
            TemporalPoint(raw_expression="tomorrow", confidence_state=DateConfidence.ABSENT),
            sent,
        )
        assert resolved.date == sent + dt.timedelta(days=1)
        assert resolved.confidence_state is DateConfidence.RELATIVE_RESOLVED

    def test_relative_without_anchor_stays_ambiguous(self):
        resolved = temporal.resolve_relative(
            TemporalPoint(raw_expression="tomorrow", confidence_state=DateConfidence.ABSENT), None
        )
        assert resolved.date is None


class TestCalendarBoundaries:
    @pytest.mark.parametrize(
        "instant,expected_monday",
        [
            (dt.datetime(2026, 9, 14, 0, 1), dt.date(2026, 9, 14)),  # Monday
            (dt.datetime(2026, 9, 20, 23, 59), dt.date(2026, 9, 14)),  # Sunday
        ],
    )
    def test_week_runs_monday_to_sunday(self, instant, expected_monday):
        clock = FrozenClock(instant, "America/Chicago")
        monday, sunday = week_bounds(clock)
        assert monday == expected_monday
        assert sunday == expected_monday + dt.timedelta(days=6)
        assert sunday.weekday() == 6

    def test_next_week_is_the_next_calendar_week(self, clock):
        this_monday, _ = week_bounds(clock, 0)
        next_monday, _ = week_bounds(clock, 1)
        assert next_monday == this_monday + dt.timedelta(days=7)

    def test_month_bounds_handle_december(self):
        clock = FrozenClock(dt.datetime(2026, 12, 15, 12, 0), "America/Chicago")
        first, last = month_bounds(clock)
        assert first == dt.date(2026, 12, 1)
        assert last == dt.date(2026, 12, 31)

    def test_month_bounds_handle_february(self):
        clock = FrozenClock(dt.datetime(2028, 2, 10, 12, 0), "America/Chicago")
        _, last = month_bounds(clock)
        assert last == dt.date(2028, 2, 29)  # leap year

    def test_dst_transition_does_not_shift_the_week(self):
        """US DST ends 2026-11-01. The week boundary must not move."""
        before = FrozenClock(dt.datetime(2026, 10, 31, 12, 0), "America/Chicago")
        after = FrozenClock(dt.datetime(2026, 11, 1, 12, 0), "America/Chicago")
        assert week_bounds(before)[0] == dt.date(2026, 10, 26)
        assert week_bounds(after)[0] == dt.date(2026, 10, 26)
