"""Rule planner.

An explainable heuristic, not an optimiser, and it never claims to be optimal.
The ordering below follows the plan's priority list exactly.

The rule that overrides all the others: never recommend spending more than the
user would otherwise have spent just to use a coupon. A coupon that requires a
$30 basket is not a saving for someone whose usual lunch is $12.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date

from ..clock import Clock, month_bounds, rolling_bounds, today, week_bounds
from ..offers import eligibility as eligibility_rules
from ..offers import temporal
from ..schemas import (
    Coverage,
    EligibilityStatus,
    OfferUserState,
    ParseStatus,
    PlanItem,
    PlanResult,
    Preferences,
    TimeStatus,
    UserStatus,
    ValidatedOffer,
)
from . import costs
from .explanations import ReasonCode


@dataclass
class _Candidate:
    offer: ValidatedOffer
    assessment: temporal.TemporalAssessment
    reasons: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    this_week_days: list[date] = field(default_factory=list)
    next_week_days: list[date] = field(default_factory=list)
    month_days: list[date] = field(default_factory=list)
    blocked: bool = False
    #: Set only when the user's eligibility overrides change the stored verdict.
    eligibility_status: EligibilityStatus | None = None
    extra_unknowns: list[str] = field(default_factory=list)
    #: The offer needs claiming/activating and the user has not said they did.
    claim_pending: bool = False

    @property
    def effective_eligibility(self) -> EligibilityStatus:
        return self.eligibility_status or self.offer.eligibility_status

    @property
    def window_width(self) -> int:
        """Fewer usable days = scarcer = schedule it earlier."""
        return len(self.this_week_days) + len(self.next_week_days) + len(self.month_days)


class Planner:
    def __init__(self, clock: Clock, preferences: Preferences) -> None:
        self.clock = clock
        self.preferences = preferences

    # -- step 1: hard exclusions -----------------------------------------

    def _screen(
        self, offer: ValidatedOffer, state: OfferUserState
    ) -> tuple[_Candidate, bool]:
        assessment = temporal.assess(offer.temporal, self.clock)
        candidate = _Candidate(offer=offer, assessment=assessment)

        # The user's own eligibility answers are applied here, not at extraction
        # time: they are not facts about the email, and a re-scan must not erase
        # them. Without this the overrides were written to the database and then
        # read by nobody, so confirming a membership changed nothing.
        if state.eligibility_overrides:
            status, unknowns = eligibility_rules.resolve(
                offer.eligibility, self.preferences, state
            )
            candidate.eligibility_status = status
            candidate.extra_unknowns = unknowns

        if state.status is UserStatus.USED:
            candidate.reasons.append(ReasonCode.USER_USED)
            candidate.blocked = True
        elif state.status is UserStatus.DISMISSED:
            candidate.reasons.append(ReasonCode.USER_DISMISSED)
            candidate.blocked = True

        if offer.merchant.strip().lower() in {
            m.strip().lower() for m in self.preferences.excluded_merchants
        }:
            candidate.reasons.append(ReasonCode.EXCLUDED_MERCHANT)
            candidate.blocked = True

        if assessment.status is TimeStatus.EXPIRED:
            candidate.reasons.append(ReasonCode.EXPIRED)
            candidate.blocked = True
        elif assessment.status is TimeStatus.CONFLICTING:
            candidate.reasons.append(ReasonCode.CONFLICTING_TERMS)

        if candidate.effective_eligibility is EligibilityStatus.INELIGIBLE:
            candidate.reasons.append(ReasonCode.INELIGIBLE)
            candidate.blocked = True

        return candidate, candidate.blocked

    # -- step 2: windows --------------------------------------------------

    def _windows(self, candidate: _Candidate) -> None:
        rules = candidate.offer.temporal
        this_week = week_bounds(self.clock, 0)
        next_week = week_bounds(self.clock, 1)
        month = month_bounds(self.clock)
        current = today(self.clock)

        # Only days from today onward can be planned.
        candidate.this_week_days = [
            day
            for day in temporal.usable_days(rules, this_week, candidate.assessment)
            if day >= current
        ]
        candidate.next_week_days = temporal.usable_days(rules, next_week, candidate.assessment)
        candidate.month_days = [
            day
            for day in temporal.usable_days(rules, month, candidate.assessment)
            if day > next_week[1] and day >= current
        ]

        if rules.weekdays:
            candidate.reasons.append(ReasonCode.WEEKDAY_RESTRICTED)

    # -- step 3: prerequisites --------------------------------------------

    def _prerequisites(self, candidate: _Candidate, state: OfferUserState) -> None:
        """Steps the user must take before the offer is usable at all.

        An offer that still has to be claimed or activated is a *conditional*
        candidate, not a committed one: the plan is explicit that something
        requiring the user to act first must not be presented as ready to go, or
        occupy one of the week's confirmed slots. Marking it saved or planned is
        how the user says they have done it.
        """
        assessment = candidate.assessment
        if assessment.claim_overdue:
            candidate.reasons.append(ReasonCode.CLAIM_DEADLINE_PASSED)
            candidate.blocked = True
            return

        already_acted = state.status in (UserStatus.SAVED, UserStatus.PLANNED)
        if assessment.claim_unknown:
            candidate.reasons.append(ReasonCode.CLAIM_DEADLINE_UNKNOWN)
            candidate.unknowns.append("claim deadline not stated")
            candidate.claim_pending = not already_acted
        elif assessment.claim_deadline is not None:
            candidate.reasons.append(ReasonCode.CLAIM_FIRST)
            candidate.unknowns.append(
                f"claim or activate by {assessment.claim_deadline.isoformat()}"
            )
            candidate.claim_pending = not already_acted

    # -- step 4: budget ----------------------------------------------------

    def _budget(self, candidate: _Candidate) -> None:
        """Reject offers that only pay off by spending more than usual."""
        benefit = candidate.offer.benefit
        per_meal = self.preferences.per_meal_budget_minor
        if per_meal is None:
            # With no budget configured there is nothing to compare against. The
            # minimum spend is already shown in the offer's face value, so
            # repeating it as an open question would be noise; the plan-level
            # note explains that no budget was set.
            return

        for label, amount in (
            ("minimum spend", benefit.minimum_spend),
            # A fixed-price deal commits the user to that price. A $40 tasting
            # menu is over a $12 lunch budget just as surely as a $40 minimum
            # spend is, and checking only the latter let it straight through.
            ("fixed price", benefit.fixed_price),
        ):
            if amount is None:
                continue
            if amount.currency != self.preferences.currency:
                candidate.unknowns.append(
                    f"{label} is in {amount.currency}, your budget is in "
                    f"{self.preferences.currency}"
                )
                continue
            if amount.minor > per_meal:
                candidate.reasons.append(ReasonCode.MIN_SPEND_ABOVE_HABIT)
                candidate.blocked = True
                return

    def _weekly_spend_minor(self, candidate: _Candidate) -> int:
        """Lower bound on what committing to this offer costs for one meal."""
        benefit = candidate.offer.benefit
        currency = self.preferences.currency
        for amount in (benefit.fixed_price, benefit.minimum_spend):
            if amount is not None and amount.currency == currency:
                return amount.minor
        return 0

    # -- assembly ----------------------------------------------------------

    def _to_item(
        self, candidate: _Candidate, bucket: str, slot: date | None, locked: bool = False
    ) -> PlanItem:
        offer = candidate.offer
        estimate = costs.estimate(
            offer.benefit,
            costs.BasketInput(
                subtotal=None,
                party_size=self.preferences.party_size,
            ),
            currency=self.preferences.currency,
        )
        deadline = None
        if candidate.assessment.effective_end is not None:
            deadline = candidate.assessment.effective_end.isoformat()
        elif candidate.assessment.status is TimeStatus.UNKNOWN:
            deadline = None

        unknowns = list(
            dict.fromkeys([*candidate.unknowns, *candidate.extra_unknowns, *offer.unresolved_fields])
        )
        return PlanItem(
            offer_id=offer.offer_id,
            merchant=offer.merchant,
            title=offer.title,
            slot_date=slot,
            bucket=bucket,  # type: ignore[arg-type]
            reason_codes=[str(code) for code in dict.fromkeys(candidate.reasons)],
            cost=estimate,
            deadline_note=deadline,
            unknowns=unknowns,
            offer_version=offer.version,
            locked=locked,
        )

    def build(
        self,
        offers: list[ValidatedOffer],
        user_states: dict[str, OfferUserState],
        coverage: Coverage | None = None,
    ) -> PlanResult:
        coverage = coverage or Coverage()
        now = self.clock.now()
        plan_id = hashlib.sha256(
            f"{now.isoformat()}|{self.preferences.preferences_hash}".encode()
        ).hexdigest()[:16]

        candidates: list[_Candidate] = []
        needs_confirmation: list[PlanItem] = []

        for offer in offers:
            state = user_states.get(offer.offer_id, OfferUserState(offer_id=offer.offer_id))
            candidate, blocked = self._screen(offer, state)

            if blocked:
                # Only surface things the user can still act on.
                if ReasonCode.EXPIRED not in candidate.reasons and (
                    ReasonCode.USER_USED not in candidate.reasons
                    and ReasonCode.USER_DISMISSED not in candidate.reasons
                ):
                    needs_confirmation.append(self._to_item(candidate, "needs_confirmation", None))
                continue

            self._windows(candidate)
            self._prerequisites(candidate, state)
            self._budget(candidate)

            if candidate.blocked:
                needs_confirmation.append(self._to_item(candidate, "needs_confirmation", None))
                continue

            # Anything not fully verified goes to the confirmation queue rather
            # than into a committed plan.
            if offer.parse_status is ParseStatus.NEEDS_VISUAL:
                candidate.reasons.append(ReasonCode.NEEDS_VISUAL_PARSE)
                needs_confirmation.append(self._to_item(candidate, "needs_confirmation", None))
                continue
            if offer.time_status is TimeStatus.UNKNOWN:
                candidate.reasons.append(ReasonCode.UNKNOWN_DEADLINE)
                needs_confirmation.append(self._to_item(candidate, "needs_confirmation", None))
                continue
            if candidate.effective_eligibility in (
                EligibilityStatus.UNKNOWN,
                EligibilityStatus.CONDITIONAL,
            ):
                candidate.reasons.append(ReasonCode.UNKNOWN_ELIGIBILITY)
                needs_confirmation.append(self._to_item(candidate, "needs_confirmation", None))
                continue
            if not offer.evidence_verified:
                candidate.reasons.append(ReasonCode.EVIDENCE_UNVERIFIED)
                needs_confirmation.append(self._to_item(candidate, "needs_confirmation", None))
                continue
            if candidate.claim_pending:
                needs_confirmation.append(self._to_item(candidate, "needs_confirmation", None))
                continue
            if offer.time_status is TimeStatus.UPCOMING and not (
                candidate.this_week_days or candidate.next_week_days or candidate.month_days
            ):
                candidate.reasons.append(ReasonCode.NOT_YET_ACTIVE)
                needs_confirmation.append(self._to_item(candidate, "needs_confirmation", None))
                continue

            candidates.append(candidate)

        this_week, next_week, this_month = self._schedule(candidates, user_states)

        notes: list[str] = []
        if self.preferences.per_meal_budget_minor is None:
            notes.append(
                "没有设置单餐预算，因此只整理优惠条件，不声称已做预算内最优安排。"
            )
        if not this_week:
            notes.append("本周没有可执行的优惠；留空优于用较差优惠凑满餐位。")

        return PlanResult(
            plan_id=plan_id,
            as_of=now,
            timezone=str(self.clock.tz),
            this_week=this_week,
            next_week=next_week,
            this_month=this_month,
            needs_confirmation=needs_confirmation,
            coverage=coverage,
            preferences_hash=self.preferences.preferences_hash,
            notes=notes,
        )

    # -- steps 5-7: slot filling ------------------------------------------

    def _schedule(
        self, candidates: list[_Candidate], user_states: dict[str, OfferUserState]
    ) -> tuple[list[PlanItem], list[PlanItem], list[PlanItem]]:
        current = today(self.clock)
        this_week_range = week_bounds(self.clock, 0)
        next_week_range = week_bounds(self.clock, 1)

        # Scarcity first: an offer usable on fewer days should be placed first.
        ordered = sorted(
            candidates,
            key=lambda c: (
                c.window_width,
                c.assessment.effective_end or date.max,
                c.offer.merchant,
            ),
        )

        # Honour slots the user locked before considering anything else.
        locked: dict[str, date] = {}
        for offer_id, state in user_states.items():
            if state.status is UserStatus.PLANNED and state.planned_date is not None:
                locked[offer_id] = state.planned_date

        weekly_cap = self.preferences.max_dining_out_per_week
        user_slots = [d for d in self.preferences.planned_meal_slots if d >= current]
        this_week_slots = [d for d in user_slots if this_week_range[0] <= d <= this_week_range[1]]
        next_week_slots = [d for d in user_slots if next_week_range[0] <= d <= next_week_range[1]]

        # The plan requires the weekly spend limit to be checked alongside the
        # weekly outing count. It was declared in config and read by nothing, so
        # a user with a $40 weekly budget could be handed four $30-minimum deals.
        weekly_budget = self.preferences.weekly_dining_budget_minor

        this_week: list[PlanItem] = []
        next_week: list[PlanItem] = []
        this_month: list[PlanItem] = []
        used_groups: set[str] = set()
        used_this_week = 0
        committed_this_week = 0

        for candidate in ordered:
            offer = candidate.offer

            # Mutually exclusive alternatives: take one, explain the other.
            group = offer.alternative_group
            if group and group in used_groups:
                candidate.reasons.append(ReasonCode.ALTERNATIVE_CHOSEN)
                this_month.append(self._to_item(candidate, "this_month", None))
                continue

            if offer.merchant.strip().lower() in {
                m.strip().lower() for m in self.preferences.preferred_merchants
            }:
                candidate.reasons.append(ReasonCode.PREFERRED_MERCHANT)

            locked_date = locked.get(offer.offer_id)
            if locked_date is not None:
                candidate.reasons.append(ReasonCode.USER_LOCKED)
                bucket = (
                    "this_week"
                    if this_week_range[0] <= locked_date <= this_week_range[1]
                    else "next_week"
                    if next_week_range[0] <= locked_date <= next_week_range[1]
                    else "this_month"
                )
                item = self._to_item(candidate, bucket, locked_date, locked=True)
                {"this_week": this_week, "next_week": next_week, "this_month": this_month}[
                    bucket
                ].append(item)
                if group:
                    used_groups.add(group)
                if bucket == "this_week":
                    used_this_week += 1
                    committed_this_week += self._weekly_spend_minor(candidate)
                continue

            spend = self._weekly_spend_minor(candidate)
            if candidate.this_week_days:
                if weekly_cap is not None and used_this_week >= weekly_cap:
                    candidate.reasons.append(ReasonCode.WEEKLY_LIMIT_REACHED)
                elif weekly_budget is not None and committed_this_week + spend > weekly_budget:
                    candidate.reasons.append(ReasonCode.BUDGET_EXCEEDED)
                elif this_week_slots or weekly_cap is not None or not user_slots:
                    slot = next(
                        (d for d in this_week_slots if d in candidate.this_week_days),
                        candidate.this_week_days[0],
                    )
                    if slot in this_week_slots:
                        candidate.reasons.append(ReasonCode.FITS_PLANNED_SLOT)
                        this_week_slots.remove(slot)
                    if candidate.assessment.expires_soon:
                        candidate.reasons.append(ReasonCode.EXPIRES_SOON)
                    this_week.append(self._to_item(candidate, "this_week", slot))
                    used_this_week += 1
                    committed_this_week += spend
                    if group:
                        used_groups.add(group)
                    continue
                else:
                    candidate.reasons.append(ReasonCode.NO_SLOT_AVAILABLE)

            if candidate.next_week_days:
                candidate.reasons.append(ReasonCode.VALID_NEXT_WEEK)
                slot = next(
                    (d for d in next_week_slots if d in candidate.next_week_days),
                    candidate.next_week_days[0],
                )
                if slot in next_week_slots:
                    candidate.reasons.append(ReasonCode.FITS_PLANNED_SLOT)
                    next_week_slots.remove(slot)
                next_week.append(self._to_item(candidate, "next_week", slot))
                if group:
                    used_groups.add(group)
                continue

            if candidate.month_days:
                candidate.reasons.append(ReasonCode.VALID_THIS_MONTH)
                if candidate.window_width > 7:
                    candidate.reasons.append(ReasonCode.LONGER_WINDOW_DEFERRED)
                this_month.append(self._to_item(candidate, "this_month", None))
                if group:
                    used_groups.add(group)

        return this_week, next_week, this_month


def horizon_days(clock: Clock, days: int) -> tuple[date, date]:
    """Rolling view, kept separate from the week/month buckets on purpose."""
    return rolling_bounds(clock, days)
