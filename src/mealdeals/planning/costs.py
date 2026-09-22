"""Money maths.

All arithmetic is on integer minor units. The rounding rule for percentage
discounts is half-up on the minor unit, applied once, and it is covered by
tests -- floating point never touches a price.

The hard rule of this module: an unknown input produces an *absent* result, not
an optimistic guess. If we do not know the basket, we do not know the saving,
and the report says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ..schemas import Benefit, BenefitKind, CostEstimate, Money


class CurrencyMismatch(ValueError):
    """Two amounts in different currencies. v0.1 does not convert, it declares."""


@dataclass
class BasketInput:
    """What the user would spend, and what we know about the extras."""

    subtotal: Money | None = None
    item_prices: list[Money] | None = None
    party_size: int | None = None
    known_tax: Money | None = None
    known_fees: Money | None = None
    baseline_alternative: Money | None = None
    """What the user would otherwise have spent. Without it there is no
    defensible 'you saved X' figure, only a face value."""


def _percent_of(amount: Money, percent: int) -> Money:
    """Half-up rounding on the minor unit, computed once with Decimal."""
    value = (Decimal(amount.minor) * Decimal(percent) / Decimal(100)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return Money(minor=int(value), currency=amount.currency)


def discount_for(benefit: Benefit, basket: Money) -> tuple[Money, list[str]]:
    """Discount that applies to a known basket, plus any caveats.

    Implements the plan's rules verbatim: below the minimum spend the discount
    is zero (not pro-rated), and a percentage discount is capped only when a cap
    was actually stated.
    """
    notes: list[str] = []
    zero = Money.zero(basket.currency)

    minimum = benefit.minimum_spend
    if minimum is not None:
        if minimum.currency != basket.currency:
            # Not "no discount" -- we simply cannot tell without a rate. The
            # caller must surface it as unknown rather than print $0.00 off.
            raise CurrencyMismatch(
                f"minimum spend is in {minimum.currency}, basket in {basket.currency}"
            )
        if basket.minor < minimum.minor:
            return zero, [f"basket is below the {minimum.format()} minimum spend"]

    if benefit.kind is BenefitKind.AMOUNT_OFF and benefit.amount_off is not None:
        if benefit.amount_off.currency != basket.currency:
            return zero, ["discount currency differs from basket currency"]
        return Money(
            minor=min(basket.minor, benefit.amount_off.minor), currency=basket.currency
        ), notes

    if benefit.kind is BenefitKind.PERCENT_OFF and benefit.percent_off is not None:
        raw = _percent_of(basket, benefit.percent_off)
        if benefit.cap is not None:
            if benefit.cap.currency != basket.currency:
                return zero, ["cap currency differs from basket currency"]
            if raw.minor > benefit.cap.minor:
                notes.append(f"discount capped at {benefit.cap.format()}")
                return benefit.cap, notes
        elif not benefit.cap_stated_absent:
            # Unknown is not unlimited. The plan only permits treating a cap as
            # infinite when the email says there is no maximum; guessing that
            # here produced the most optimistic possible number and presented it
            # as a firm saving. Callers must surface this as an unknown.
            notes.append(
                "no maximum discount was stated, so the true discount may be lower than this"
            )
        return raw, notes

    if benefit.kind is BenefitKind.FIXED_PRICE and benefit.fixed_price is not None:
        if benefit.fixed_price.currency != basket.currency:
            raise CurrencyMismatch("fixed price currency differs from basket currency")
        if basket.minor < benefit.fixed_price.minor:
            # The deal sets a floor, not a ceiling: a $20 prix fixe against a $15
            # basket means paying $20. Reporting a zero discount left
            # out_of_pocket at $15, understating what the user would actually pay.
            notes.append(
                f"the fixed price {benefit.fixed_price.format()} is above this basket; "
                "taking the offer means spending more"
            )
            return Money(minor=0, currency=basket.currency), notes
        return Money(minor=basket.minor - benefit.fixed_price.minor, currency=basket.currency), notes

    if benefit.kind is BenefitKind.BOGO:
        # Needs two item prices and someone to eat the second portion.
        return zero, ["buy-one-get-one needs both item prices to be priced"]

    return zero, ["benefit type cannot be priced automatically"]


def bogo_discount(
    item_prices: list[Money], party_size: int | None
) -> tuple[Money | None, list[str]]:
    """Value of a buy-one-get-one, which is never simply 'half price'.

    Requires two known item prices. A solo diner gets a caveat rather than a
    silent 50% claim, because the second portion is only worth something if it
    is actually eaten.
    """
    if len(item_prices) < 2:
        return None, ["buy-one-get-one needs the price of both items"]
    currencies = {price.currency for price in item_prices}
    if len(currencies) > 1:
        return None, ["mixed currencies in the basket"]

    cheapest = min(item_prices, key=lambda price: price.minor)
    notes: list[str] = []
    if party_size is not None and party_size < 2:
        notes.append(
            "party size is 1: the free item only counts as a saving if you would "
            "have bought or eaten it anyway"
        )
    return cheapest, notes


def estimate(
    benefit: Benefit,
    basket: BasketInput,
    *,
    currency: str = "USD",
) -> CostEstimate:
    """Full cost estimate. Returns ``computable=False`` when inputs are missing."""
    unknown: list[str] = []
    notes: list[str] = []

    if benefit.kind is BenefitKind.BOGO:
        prices = basket.item_prices or []
        discount, bogo_notes = bogo_discount(prices, basket.party_size)
        notes.extend(bogo_notes)
        if discount is None:
            unknown.append("item prices")
            return CostEstimate(
                unknown_components=unknown,
                computable=False,
                note="; ".join(notes) or None,
            )
        subtotal = Money(minor=sum(p.minor for p in prices), currency=prices[0].currency)
        # A buy-one-get-one still has to clear its own minimum spend.
        floor = benefit.minimum_spend
        if floor is not None:
            if floor.currency != subtotal.currency:
                return CostEstimate(
                    basket=subtotal,
                    unknown_components=["minimum spend (currency mismatch)"],
                    computable=False,
                    note=f"minimum spend is in {floor.currency}, basket in {subtotal.currency}",
                )
            if subtotal.minor < floor.minor:
                discount = Money.zero(subtotal.currency)
                notes.append(f"basket is below the {floor.format()} minimum spend")
    else:
        if basket.subtotal is None:
            unknown.append("menu price / basket subtotal")
            return CostEstimate(
                unknown_components=unknown,
                computable=False,
                note="face value only; no basket total was supplied",
            )
        subtotal = basket.subtotal
        try:
            discount, discount_notes = discount_for(benefit, subtotal)
        except CurrencyMismatch as exc:
            # Declaring the gap beats printing a confident "$0.00 off".
            return CostEstimate(
                basket=subtotal,
                unknown_components=["discount (currency mismatch)"],
                computable=False,
                note=str(exc),
            )
        notes.extend(discount_notes)

    if (
        benefit.kind is BenefitKind.FIXED_PRICE
        and benefit.fixed_price is not None
        and benefit.fixed_price.currency == subtotal.currency
    ):
        # A fixed price IS the amount payable, above or below the basket.
        after_discount = benefit.fixed_price
    else:
        after_discount = subtotal - discount

    # A percentage discount whose ceiling the email never stated is computed
    # uncapped above. That is a best case, not a figure to plan against, so it
    # is declared alongside tax and fees rather than passed off as known.
    if (
        benefit.kind is BenefitKind.PERCENT_OFF
        and benefit.cap is None
        and not benefit.cap_stated_absent
    ):
        unknown.append("maximum discount")
    if basket.known_tax is None:
        unknown.append("tax")
    if basket.known_fees is None:
        unknown.append("delivery / service fees")

    out_of_pocket = after_discount
    if basket.known_tax is not None:
        out_of_pocket = out_of_pocket + basket.known_tax
    if basket.known_fees is not None:
        out_of_pocket = out_of_pocket + basket.known_fees

    relative: Money | None = None
    costs_more = False
    if basket.baseline_alternative is not None:
        if basket.baseline_alternative.currency != out_of_pocket.currency:
            notes.append("baseline alternative is in a different currency")
        else:
            # Money is unsigned by design, so subtraction clamps at zero. Taking
            # that as the answer reported "$0.00 saved" for an offer that costs
            # more than what the user would otherwise have bought -- hiding the
            # exact case this tool exists to catch. Report the excess instead.
            difference = basket.baseline_alternative.minor - out_of_pocket.minor
            if difference < 0:
                costs_more = True
                excess = Money(minor=-difference, currency=out_of_pocket.currency)
                notes.append(
                    f"this costs {excess.format()} MORE than the alternative you would "
                    "otherwise have bought"
                )
            else:
                relative = Money(minor=difference, currency=out_of_pocket.currency)
    else:
        notes.append(
            "no comparable alternative was supplied, so real savings are not shown"
        )

    return CostEstimate(
        basket=subtotal,
        discount=discount,
        out_of_pocket=out_of_pocket,
        relative_savings=relative,
        exceeds_baseline=costs_more,
        unknown_components=unknown,
        computable=True,
        note="; ".join(notes) or None,
    )


def face_value(benefit: Benefit) -> str:
    """Human-readable face value. Never presented as realised savings."""
    if benefit.kind is BenefitKind.AMOUNT_OFF and benefit.amount_off:
        text = f"{benefit.amount_off.format()} off"
        if benefit.minimum_spend:
            text += f" (min. spend {benefit.minimum_spend.format()})"
        return text
    if benefit.kind is BenefitKind.PERCENT_OFF and benefit.percent_off:
        text = f"{benefit.percent_off}% off"
        if benefit.cap:
            text += f" (max {benefit.cap.format()})"
        return text
    if benefit.kind is BenefitKind.FIXED_PRICE and benefit.fixed_price:
        return f"fixed price {benefit.fixed_price.format()}"
    if benefit.kind is BenefitKind.BOGO:
        return "buy one, get one"
    if benefit.kind is BenefitKind.FREE_ITEM:
        return f"free: {benefit.free_item_description or 'item'}"
    return benefit.description or str(benefit.kind)
