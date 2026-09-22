"""Temporal state.

Two rules drive this module:

* An absent end date produces ``UNKNOWN``, never ``WITHIN_STATED_WINDOW``. The
  application must not act as though a silent offer never expires.
* The claim deadline is evaluated separately from the redemption window, so an
  offer that is usable next week but has to be claimed today is not silently
  filed as "fine, deal with it later".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from ..clock import Clock
from ..schemas import DateConfidence, TemporalPoint, TemporalRules, TimeStatus, TriState


@dataclass
class TemporalAssessment:
    status: TimeStatus
    reasons: list[str] = field(default_factory=list)
    effective_start: date | None = None
    effective_end: date | None = None
    claim_deadline: date | None = None
    claim_overdue: bool = False
    claim_unknown: bool = False
    days_until_end: int | None = None

    @property
    def expires_soon(self) -> bool:
        return self.days_until_end is not None and 0 <= self.days_until_end <= 3


def resolve_relative(point: TemporalPoint, anchor: date | None) -> TemporalPoint:
    """Resolve 'tomorrow'/'today' style expressions against a trusted anchor.

    Without an anchor the expression stays ambiguous. The plan is explicit that
    the scan date is *not* a valid substitute for the sender's own timezone.
    """
    if point.kind is not None and point.date is not None:
        return point
    raw = (point.raw_expression or "").strip().lower()
    if not raw or anchor is None:
        return point

    offsets = {
        "today": 0,
        "tonight": 0,
        "tomorrow": 1,
        "tomorrow only": 1,
        "今天": 0,
        "今晚": 0,
        "明天": 1,
    }
    if raw in offsets:
        return point.model_copy(
            update={
                "date": anchor + timedelta(days=offsets[raw]),
                "anchor": anchor,
                "confidence_state": DateConfidence.RELATIVE_RESOLVED,
            }
        )
    return point.model_copy(update={"confidence_state": DateConfidence.RELATIVE_AMBIGUOUS})


def assess(rules: TemporalRules, clock: Clock, *, anchor: date | None = None) -> TemporalAssessment:
    """Derive the time status of one offer at the clock's instant."""
    today = clock.now().date()
    reasons: list[str] = []

    starts = resolve_relative(rules.starts, anchor)
    ends = resolve_relative(rules.ends, anchor)
    claim = resolve_relative(rules.claim_deadline, anchor)

    start_date = starts.date
    end_date = ends.date
    claim_date = claim.date

    # Contradictions win over everything else: never silently pick the nicer read.
    if start_date and end_date and start_date > end_date:
        reasons.append("start date is after end date")
        return TemporalAssessment(
            status=TimeStatus.CONFLICTING,
            reasons=reasons,
            effective_start=start_date,
            effective_end=end_date,
            claim_deadline=claim_date,
        )

    if (
        starts.confidence_state is DateConfidence.CONFLICTING
        or ends.confidence_state is DateConfidence.CONFLICTING
    ):
        reasons.append("the email states conflicting dates")
        return TemporalAssessment(status=TimeStatus.CONFLICTING, reasons=reasons)

    claim_overdue = claim_date is not None and claim_date < today
    claim_unknown = rules.claim_required is TriState.KNOWN_YES and claim_date is None
    if claim_unknown:
        reasons.append("a claim step is required but its deadline is not stated")

    days_until_end = (end_date - today).days if end_date else None

    if end_date is not None and end_date < today:
        reasons.append(f"redemption window ended {end_date.isoformat()}")
        status = TimeStatus.EXPIRED
    elif start_date is not None and start_date > today:
        reasons.append(f"window opens {start_date.isoformat()}")
        status = TimeStatus.UPCOMING
    elif end_date is None:
        # No stated end. Unknown, not open-ended.
        reasons.append("no end date stated in the email")
        status = TimeStatus.UNKNOWN
    else:
        status = TimeStatus.WITHIN_STATED_WINDOW

    if claim_overdue and status is TimeStatus.WITHIN_STATED_WINDOW:
        reasons.append(f"claim deadline passed on {claim_date.isoformat()}")

    return TemporalAssessment(
        status=status,
        reasons=reasons,
        effective_start=start_date,
        effective_end=end_date,
        claim_deadline=claim_date,
        claim_overdue=claim_overdue,
        claim_unknown=claim_unknown,
        days_until_end=days_until_end,
    )


def usable_on(rules: TemporalRules, day: date, assessment: TemporalAssessment) -> bool:
    """Whether the email's own terms cover a specific calendar day.

    Deliberately strict: an offer with no stated end date is not projected onto
    a future day, because there is no evidence it is still live then.
    """
    if assessment.status in (TimeStatus.EXPIRED, TimeStatus.CONFLICTING):
        return False
    if assessment.effective_end is None:
        return False
    if day > assessment.effective_end:
        return False
    if assessment.effective_start is not None and day < assessment.effective_start:
        return False
    if rules.weekdays and day.weekday() not in rules.weekdays:
        return False
    return day not in rules.blackout_dates


def usable_days(rules: TemporalRules, window: tuple[date, date], assessment: TemporalAssessment) -> list[date]:
    """Every day inside ``window`` on which the offer's stated terms hold."""
    start, end = window
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if usable_on(rules, cursor, assessment):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def refresh(offer, clock: Clock):
    """Re-derive an offer's time status at *now*, without re-reading the email.

    Stored offers carry the state that was true when they were extracted. Read
    back a week later, an expired coupon still claims ``within_stated_window``
    and still reports ``actionable``. Recomputing is pure local arithmetic over
    dates that are already in the payload, so it costs nothing -- the plan is
    explicit that expiry must be recalculated daily rather than re-extracted.

    Returns the offer unchanged when the status still holds.
    """
    status = assess(offer.temporal, clock).status
    if status is offer.time_status:
        return offer
    return offer.model_copy(update={"time_status": status})
