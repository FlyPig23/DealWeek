"""Extractor contract.

Every adapter must satisfy the same contract, which is why these tests are
written against stub clients rather than a live provider:

* an empty offer list is only meaningful when status is SUCCESS;
* a refusal, truncation or schema violation is FAILED with an error code;
* usage that the provider did not report is 'unknown', never zero.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from weekly_deals.models.compatible_extractor import CompatibleExtractor, extract_json
from weekly_deals.models.mock import MockExtractor
from weekly_deals.models.wire import WireOffer, to_draft
from weekly_deals.schemas import BenefitKind, ExtractionStatus, TriState

from ..conftest import make_email

BODY = (
    "Take $4 off any lunch bowl when you spend $12 or more.\n"
    "Offer ends September 30, 2026. Pickup only. Use code LUNCH4."
)

VALID_PAYLOAD = {
    "status": "success",
    "offers": [
        {
            "merchant": "Noodle Lantern",
            "title": "$4 off lunch",
            "food_category": "restaurant",
            "benefit_kind": "amount_off",
            "amount_off_minor": 400,
            "minimum_spend_minor": 1200,
            "currency": "USD",
            "promo_code": "LUNCH4",
            "valid_until": {"raw_expression": "Offer ends September 30, 2026",
                            "iso_date": "2026-09-30", "has_explicit_time": False},
            "channels": ["pickup"],
            "evidence": [{"field_path": "benefit.amount_off", "quote": "$4 off"}],
        }
    ],
}


class StubResponse:
    def __init__(self, content: str, finish_reason: str = "stop", usage=None, refusal=None):
        message = SimpleNamespace(content=content, refusal=refusal, parsed=None)
        self.choices = [SimpleNamespace(message=message, finish_reason=finish_reason)]
        self.usage = usage


class StubClient:
    """Minimal stand-in for the OpenAI client surface these adapters use."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def compatible(*responses) -> tuple[CompatibleExtractor, StubClient]:
    client = StubClient(*responses)
    return (
        CompatibleExtractor("k", "test-model", "https://example.invalid/v1", client=client),
        client,
    )


class TestWireMapping:
    def test_maps_a_full_offer(self):
        wire = WireOffer.model_validate(VALID_PAYLOAD["offers"][0])
        draft = to_draft(wire, "m1")
        assert draft.benefit.kind is BenefitKind.AMOUNT_OFF
        assert draft.benefit.amount_off is not None
        assert draft.benefit.amount_off.minor == 400
        assert draft.benefit.minimum_spend is not None
        assert draft.temporal.ends.date is not None
        assert draft.evidence[0].quote == "$4 off"

    def test_kind_without_its_payload_degrades_instead_of_raising(self):
        """A model claiming amount_off with no amount must not crash the run."""
        wire = WireOffer(merchant="X", benefit_kind="amount_off", amount_off_minor=None)
        assert to_draft(wire, "m1").benefit.kind is BenefitKind.OTHER

    def test_unknown_enum_values_fall_back(self):
        wire = WireOffer(merchant="X", food_category="interdimensional", channels=["teleport"])
        draft = to_draft(wire, "m1")
        assert str(draft.food_category) == "other_or_unclear"
        assert str(draft.eligibility.channels[0]) == "unknown"

    def test_unresolvable_date_keeps_the_raw_expression(self):
        wire = WireOffer(
            merchant="X",
            valid_until={"raw_expression": "next Tuesday", "iso_date": None},
        )
        point = to_draft(wire, "m1").temporal.ends
        assert point.date is None
        assert point.raw_expression == "next Tuesday"

    def test_out_of_range_percentage_is_dropped(self):
        wire = WireOffer(merchant="X", benefit_kind="percent_off", percent_off=150)
        assert to_draft(wire, "m1").benefit.percent_off is None

    def test_short_evidence_quotes_are_discarded(self):
        wire = WireOffer(merchant="X", evidence=[{"field_path": "a", "quote": "x"}])
        assert to_draft(wire, "m1").evidence == []

    def test_unknown_tristate_defaults_to_unknown(self):
        wire = WireOffer(merchant="X", membership_required="maybe")
        assert to_draft(wire, "m1").eligibility.membership_required is TriState.UNKNOWN


class TestJsonRecovery:
    def test_plain_object(self):
        assert extract_json('{"a": 1}') == '{"a": 1}'

    def test_fenced_block(self):
        assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_object_surrounded_by_prose(self):
        assert extract_json('Sure!\n{"a": 1}\nHope that helps.') == '{"a": 1}'

    def test_no_object(self):
        assert extract_json("I cannot help with that.") is None


class TestCompatibleAdapter:
    def test_valid_response_succeeds(self):
        extractor, _ = compatible(StubResponse(json.dumps(VALID_PAYLOAD)))
        result = extractor.extract(make_email(BODY))
        assert result.status is ExtractionStatus.SUCCESS
        assert len(result.offers) == 1

    def test_prose_wrapped_json_is_recovered(self):
        extractor, _ = compatible(StubResponse(f"Here you go:\n{json.dumps(VALID_PAYLOAD)}"))
        assert extractor.extract(make_email(BODY)).status is ExtractionStatus.SUCCESS

    def test_one_repair_attempt_then_success(self):
        extractor, client = compatible(
            StubResponse("I'm not sure what you want."),
            StubResponse(json.dumps(VALID_PAYLOAD)),
        )
        result = extractor.extract(make_email(BODY))
        assert result.status is ExtractionStatus.SUCCESS
        assert result.meta.retry_count == 1
        assert len(client.calls) == 2

    def test_two_failures_give_up_rather_than_loop(self):
        extractor, client = compatible(
            StubResponse("nope"), StubResponse("still nope")
        )
        result = extractor.extract(make_email(BODY))
        assert result.status is ExtractionStatus.FAILED
        assert result.error_code == "schema_violation"
        assert len(client.calls) == 2

    def test_truncation_is_a_failure_not_an_empty_result(self):
        extractor, _ = compatible(StubResponse('{"offers": [', finish_reason="length"))
        result = extractor.extract(make_email(BODY))
        assert result.status is ExtractionStatus.FAILED
        assert result.error_code == "truncated"
        assert result.offers == []

    def test_timeout_is_classified(self):
        class APITimeoutError(Exception):
            pass

        extractor, _ = compatible(APITimeoutError("slow"))
        assert extractor.extract(make_email(BODY)).error_code == "timeout"

    def test_rate_limit_is_classified(self):
        class RateLimitError(Exception):
            pass

        extractor, _ = compatible(RateLimitError("slow down"))
        assert extractor.extract(make_email(BODY)).error_code == "rate_limited"

    def test_cost_is_unknown_for_an_arbitrary_endpoint(self):
        extractor, _ = compatible(
            StubResponse(
                json.dumps(VALID_PAYLOAD),
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
            )
        )
        usage = extractor.extract(make_email(BODY)).meta.usage
        assert usage.input_tokens == 100
        assert usage.cost_known is False

    def test_capabilities_are_declared_honestly(self):
        extractor, _ = compatible(StubResponse("{}"))
        assert extractor.capabilities()["structured_output"] is False

    def test_base_url_is_required(self):
        with pytest.raises(ValueError):
            CompatibleExtractor("k", "m", "", client=StubClient())

    def test_schema_is_embedded_in_the_prompt(self):
        extractor, _ = compatible(StubResponse("{}"))
        assert "JSON Schema" in extractor.prompt


class TestMockExtractorContract:
    def test_empty_result_on_a_complete_email_is_success(self):
        result = MockExtractor().extract(make_email("Our new autumn menu is here."))
        assert result.status is ExtractionStatus.SUCCESS
        assert result.offers == []

    def test_empty_result_on_an_incomplete_email_is_needs_review(self):
        """Absence of evidence is not evidence of absence."""
        result = MockExtractor().extract(make_email("", body_complete=False))
        assert result.status is ExtractionStatus.NEEDS_REVIEW

    def test_image_only_email_returns_no_fabricated_offer(self):
        result = MockExtractor().extract(
            make_email("Free scoop", has_unparsed_visuals=True)
        )
        assert result.status is ExtractionStatus.NEEDS_REVIEW
        assert result.offers == []

    def test_multiple_offers_in_one_email(self):
        result = MockExtractor().extract(
            make_email("Get $5 off any entree, or a free dessert with any entree.")
        )
        assert len(result.offers) >= 2

    def test_receipt_coupon_is_found(self):
        """A coupon at the bottom of a receipt must not be skipped."""
        result = MockExtractor().extract(
            make_email(
                "Thanks for your order. Total charged: $23.40.\n\n"
                "A thank-you for next time: $6 off your next order of $25 or more.\n"
                "Offer ends September 30, 2026."
            )
        )
        assert any(
            o.benefit.amount_off and o.benefit.amount_off.minor == 600 for o in result.offers
        )
