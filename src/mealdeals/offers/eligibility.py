"""Eligibility resolution.

The asymmetry here is deliberate: a condition the email never mentions is
``UNKNOWN``, and ``UNKNOWN`` never upgrades an offer to ``CONFIRMED``.

But there is a second, equally important asymmetry, and getting it wrong makes
the product useless. Not every unknown is material:

* **Material** conditions can make the offer unusable *for this person* --
  new-customer-only, a membership they may not hold, an offer targeted at
  another account. These downgrade the offer to CONDITIONAL, because acting on
  them could waste a trip.
* **Caveats** are near-universal boilerplate -- "at participating locations",
  an unstated redemption channel, a one-per-customer limit. These are surfaced
  for the user to check but do not downgrade the offer, because if they did,
  essentially every real promotion would land in the review queue and the
  planner would recommend nothing at all.

Silence about a condition that most emails never mention (membership, for
instance) produces no unknown at all. Treating it as one was an early bug here:
it pushed every offer into "needs confirmation" and the plan came back empty.
"""

from __future__ import annotations

from ..schemas import (
    Channel,
    Eligibility,
    EligibilityStatus,
    OfferUserState,
    Preferences,
    TriState,
)


def resolve(
    eligibility: Eligibility,
    preferences: Preferences,
    user_state: OfferUserState | None = None,
) -> tuple[EligibilityStatus, list[str]]:
    """Return the eligibility status plus everything the user should check.

    The returned list mixes material unknowns and caveats; the status reflects
    only the material ones.
    """
    overrides = dict(user_state.eligibility_overrides) if user_state else {}
    material: list[str] = []
    caveats: list[str] = []
    blocked: list[str] = []

    def effective(field_name: str) -> TriState:
        if field_name in overrides:
            return TriState(overrides[field_name])
        return getattr(eligibility, field_name)

    # -- material conditions ------------------------------------------------

    membership = effective("membership_required")
    if membership is TriState.KNOWN_YES:
        name = (eligibility.membership_name or "").strip().lower()
        confirmed = {m.strip().lower() for m in preferences.confirmed_memberships}
        if name and name in confirmed:
            pass  # the user told us they hold it
        else:
            label = eligibility.membership_name or "programme not named"
            material.append(f"membership required: {label}")

    if effective("new_customer_only") is TriState.KNOWN_YES:
        material.append("new customers only - confirm this applies to you")

    if effective("targeted_account") is TriState.KNOWN_YES:
        material.append("targeted at a specific account - confirm it is yours")

    # -- hard blocks --------------------------------------------------------

    if effective("targeted_account") is TriState.KNOWN_NO:
        blocked.append("offer is targeted at a different account")

    allowed = set(preferences.allowed_channels)
    offer_channels = set(eligibility.channels)
    if allowed:
        stated = offer_channels - {Channel.UNKNOWN}
        if not stated:
            caveats.append("redemption channel not stated")
        elif not (stated & allowed):
            # A stated channel the user cannot use is a block even when the
            # email also lists "unknown" alongside it. Short-circuiting on the
            # presence of UNKNOWN let a delivery-only offer through for someone
            # who only collects in person.
            blocked.append(
                "redemption channel ("
                + ", ".join(sorted(str(c) for c in stated))
                + ") is outside your allowed channels"
            )
            if Channel.UNKNOWN in offer_channels:
                caveats.append("the email also leaves one redemption channel unstated")

    # -- caveats ------------------------------------------------------------

    if effective("participating_locations_only") is TriState.KNOWN_YES:
        caveats.append(eligibility.location_note or "participating locations only")

    if eligibility.usage_limit == 1:
        caveats.append("one use per customer")

    if eligibility.stackable is TriState.KNOWN_NO:
        caveats.append("cannot be combined with other offers")

    if blocked:
        return EligibilityStatus.INELIGIBLE, blocked + material + caveats
    if material:
        return EligibilityStatus.CONDITIONAL, material + caveats
    return EligibilityStatus.CONFIRMED, caveats
