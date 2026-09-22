"""Money maths.

Integer minor units throughout, and the rule the whole product hangs on: if we
do not know the basket, we do not claim a saving.
"""

from __future__ import annotations

import pytest

from weekly_deals.planning import costs
from weekly_deals.schemas import Benefit, BenefitKind, Money


def usd(minor: int) -> Money:
    return Money(minor=minor, currency="USD")


def amount_off(minor: int, minimum: int | None = None) -> Benefit:
    return Benefit(
        kind=BenefitKind.AMOUNT_OFF,
        amount_off=usd(minor),
        minimum_spend=usd(minimum) if minimum is not None else None,
    )


def percent_off(percent: int, cap: int | None = None, stated_no_cap: bool = False) -> Benefit:
    return Benefit(
        kind=BenefitKind.PERCENT_OFF,
        percent_off=percent,
        cap=usd(cap) if cap is not None else None,
        cap_stated_absent=stated_no_cap,
    )


class TestMinimumSpend:
    def test_at_the_threshold_the_discount_applies(self):
        discount, _ = costs.discount_for(amount_off(400, 1200), usd(1200))
        assert discount.minor == 400

    def test_one_cent_below_threshold_gives_nothing(self):
        """Not pro-rated. Below the minimum the discount is zero."""
        discount, notes = costs.discount_for(amount_off(400, 1200), usd(1199))
        assert discount.minor == 0
        assert any("minimum spend" in note for note in notes)

    def test_discount_never_exceeds_the_basket(self):
        discount, _ = costs.discount_for(amount_off(1000), usd(600))
        assert discount.minor == 600


class TestPercentage:
    def test_percent_is_not_the_amount_paid(self):
        """20% off a $50 basket is $10 off, not a $10 bill."""
        discount, _ = costs.discount_for(percent_off(20), usd(5000))
        assert discount.minor == 1000

    def test_cap_applies(self):
        discount, notes = costs.discount_for(percent_off(20, cap=500), usd(5000))
        assert discount.minor == 500
        assert any("capped" in note for note in notes)

    def test_cap_not_reached(self):
        discount, _ = costs.discount_for(percent_off(20, cap=5000), usd(5000))
        assert discount.minor == 1000

    def test_missing_cap_is_reported_as_unknown(self):
        _, notes = costs.discount_for(percent_off(20), usd(5000))
        assert any("no maximum" in note for note in notes)

    def test_explicit_no_cap_is_silent(self):
        _, notes = costs.discount_for(percent_off(20, stated_no_cap=True), usd(5000))
        assert not any("no maximum" in note for note in notes)

    @pytest.mark.parametrize(
        "basket,percent,expected",
        [
            (1050, 15, 158),  # 157.5 -> half-up
            (1000, 33, 330),
            (333, 50, 167),  # 166.5 -> half-up
            (1, 50, 1),  # 0.5 -> half-up
        ],
    )
    def test_rounding_is_half_up_on_the_minor_unit(self, basket, percent, expected):
        discount, _ = costs.discount_for(percent_off(percent), usd(basket))
        assert discount.minor == expected


class TestBogo:
    def test_needs_two_prices(self):
        discount, notes = costs.bogo_discount([usd(1200)], party_size=2)
        assert discount is None
        assert any("both item" in note for note in notes)

    def test_value_is_the_cheaper_item(self):
        discount, _ = costs.bogo_discount([usd(1500), usd(1200)], party_size=2)
        assert discount is not None
        assert discount.minor == 1200

    def test_solo_diner_gets_a_caveat_not_a_half_price_claim(self):
        """A second portion only saves money if it is actually eaten."""
        discount, notes = costs.bogo_discount([usd(1200), usd(1200)], party_size=1)
        assert discount is not None
        assert any("party size is 1" in note for note in notes)


class TestCurrency:
    def test_mixed_currency_yields_no_discount(self):
        benefit = Benefit(
            kind=BenefitKind.AMOUNT_OFF,
            amount_off=Money(minor=400, currency="EUR"),
        )
        discount, notes = costs.discount_for(benefit, usd(2000))
        assert discount.minor == 0
        assert notes

    def test_money_addition_across_currencies_raises(self):
        with pytest.raises(ValueError):
            usd(100) + Money(minor=100, currency="EUR")


class TestEstimate:
    def test_no_basket_means_no_savings_claim(self):
        estimate = costs.estimate(amount_off(400), costs.BasketInput())
        assert not estimate.computable
        assert estimate.out_of_pocket is None
        assert estimate.relative_savings is None

    def test_unknown_fees_are_listed_not_assumed_zero(self):
        estimate = costs.estimate(amount_off(400), costs.BasketInput(subtotal=usd(2000)))
        assert estimate.computable
        assert estimate.out_of_pocket is not None
        assert estimate.out_of_pocket.minor == 1600
        assert "tax" in estimate.unknown_components
        assert "delivery / service fees" in estimate.unknown_components

    def test_no_relative_savings_without_a_comparable_alternative(self):
        estimate = costs.estimate(amount_off(400), costs.BasketInput(subtotal=usd(2000)))
        assert estimate.relative_savings is None
        assert "no comparable alternative" in (estimate.note or "")

    def test_relative_savings_computed_against_a_baseline(self):
        estimate = costs.estimate(
            amount_off(400),
            costs.BasketInput(
                subtotal=usd(2000),
                known_tax=usd(150),
                known_fees=usd(0),
                baseline_alternative=usd(1900),
            ),
        )
        # 2000 - 400 + 150 = 1750 out of pocket; baseline 1900 -> saves 150.
        assert estimate.out_of_pocket is not None and estimate.out_of_pocket.minor == 1750
        assert estimate.relative_savings is not None
        assert estimate.relative_savings.minor == 150
        assert estimate.unknown_components == []


class TestFaceValue:
    def test_includes_minimum_spend(self):
        assert "min. spend" in costs.face_value(amount_off(400, 1200))

    def test_percentage_includes_cap(self):
        assert "max" in costs.face_value(percent_off(20, cap=500))
