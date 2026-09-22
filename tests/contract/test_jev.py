"""JEV adapter contract.

Uses httpx's built-in MockTransport, so the real endpoint is never called and CI
needs no key. What is asserted is the contract the plan states: a Noul answer is
a probability under ``noul``, there is no Noul ``confidence`` field, and a
failure is an error rather than a negative verdict.
"""

from __future__ import annotations

import httpx
import pytest

from mealdeals.models.jev import JevClassifier, route_for
from mealdeals.schemas import FoodCategory, Route

from ..conftest import make_email

QUESTIONS = {"contains_food_offer": {"type": "noul", "instructions": "..."}}
BODY = "Take $4 off a lunch purchase of $12 or more. Pickup only."


def client_returning(payload: dict, status: int = 200, headers: dict | None = None) -> httpx.Client:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=payload, headers=headers or {})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.recorded = calls  # type: ignore[attr-defined]
    return client


def classifier(client: httpx.Client, **kwargs) -> JevClassifier:
    return JevClassifier("test-key", questions=QUESTIONS, client=client, **kwargs)


class TestSuccessfulAnswer:
    def test_generic_promotion_answer_is_supported(self):
        client = client_returning(
            {
                "answers": {
                    "contains_promotion": {"noul": 0.91},
                    "promotion_category": {"choice": "shopping"},
                }
            }
        )
        result = classifier(client).classify(make_email("Save 20% on supplies."))
        assert result.contains_promotion == pytest.approx(0.91)
        assert result.contains_food_offer == pytest.approx(0.91)
        assert result.promotion_category == "shopping"

    def test_noul_probability_is_read(self):
        client = client_returning(
            {
                "model": "jev-1.13.0",
                "answers": {"contains_food_offer": {"noul": 0.93}},
                "usage": {"input_tokens": 120},
            }
        )
        result = classifier(client).classify(make_email(BODY))
        assert result.contains_food_offer == pytest.approx(0.93)
        assert result.error_code is None

    def test_reported_model_version_is_recorded(self):
        client = client_returning(
            {"model": "jev-1.13.1", "answers": {"contains_food_offer": {"noul": 0.5}}}
        )
        result = classifier(client, model="jev-latest").classify(make_email(BODY))
        assert result.meta.model == "jev-1.13.1"

    def test_choice_answer_maps_to_a_category(self):
        client = client_returning(
            {
                "answers": {
                    "contains_food_offer": {"noul": 0.9},
                    "food_category": {"choice": "restaurant", "confidence": 0.8},
                }
            }
        )
        result = classifier(client).classify(make_email(BODY))
        assert result.food_category is FoodCategory.RESTAURANT
        assert result.category_confidence == pytest.approx(0.8)

    def test_unknown_choice_degrades_rather_than_raising(self):
        client = client_returning(
            {
                "answers": {
                    "contains_food_offer": {"noul": 0.9},
                    "food_category": {"choice": "martian_cuisine"},
                }
            }
        )
        result = classifier(client).classify(make_email(BODY))
        assert result.food_category is FoodCategory.OTHER_OR_UNCLEAR

    def test_email_is_sent_as_state_not_as_instructions(self):
        client = client_returning({"answers": {"contains_food_offer": {"noul": 0.5}}})
        classifier(client).classify(make_email(BODY))
        request = client.recorded[0]  # type: ignore[attr-defined]
        import json

        sent = json.loads(request.content)
        assert sent["state"]["body"] == BODY
        assert "instructions" not in sent["state"]

    def test_the_api_key_is_sent_as_a_bearer_header_only(self):
        client = client_returning({"answers": {"contains_food_offer": {"noul": 0.5}}})
        classifier(client).classify(make_email(BODY))
        request = client.recorded[0]  # type: ignore[attr-defined]
        assert request.headers["authorization"] == "Bearer test-key"
        assert "test-key" not in request.content.decode()


class TestCostAccounting:
    def test_usage_produces_a_cost(self):
        client = client_returning(
            {
                "answers": {"contains_food_offer": {"noul": 0.9}},
                "usage": {"input_tokens": 1_000_000},
            }
        )
        result = classifier(client).classify(make_email(BODY))
        assert result.meta.usage.cost_known
        assert result.meta.usage.estimated_cost_usd == pytest.approx(0.042)

    def test_absent_usage_is_unknown_not_zero(self):
        client = client_returning({"answers": {"contains_food_offer": {"noul": 0.9}}})
        result = classifier(client).classify(make_email(BODY))
        assert result.meta.usage.cost_known is False
        assert result.meta.usage.estimated_cost_usd is None


class TestFailures:
    def test_malformed_probability_is_an_error_not_a_negative(self):
        """0.0 and 'invalid' must not be confused."""
        client = client_returning({"answers": {"contains_food_offer": {"noul": "yes"}}})
        result = classifier(client).classify(make_email(BODY))
        assert result.error_code == "missing_noul"
        assert result.contains_food_offer is None
        assert result.route is Route.FALLBACK_PENDING

    def test_boolean_is_rejected_as_a_probability(self):
        client = client_returning({"answers": {"contains_food_offer": {"noul": True}}})
        result = classifier(client).classify(make_email(BODY))
        assert result.error_code == "missing_noul"

    def test_missing_answers_object_is_a_schema_error(self):
        client = client_returning({"model": "jev-1.13.0"})
        result = classifier(client).classify(make_email(BODY))
        assert result.error_code == "bad_schema"

    def test_auth_failure_is_reported_and_not_retried(self):
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(401, json={"error": "nope"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = classifier(client, max_retries=3).classify(make_email(BODY))
        assert result.error_code == "auth"
        assert len(attempts) == 1

    def test_server_error_retries_then_reports(self, monkeypatch):
        monkeypatch.setattr("mealdeals.models.jev.time.sleep", lambda _: None)
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(503)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = classifier(client, max_retries=2).classify(make_email(BODY))
        assert result.error_code == "http_503"
        assert len(attempts) == 3

    def test_rate_limit_respects_retry_after(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("mealdeals.models.jev.time.sleep", lambda s: slept.append(s))
        state = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            state["n"] += 1
            if state["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(200, json={"answers": {"contains_food_offer": {"noul": 0.8}}})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = classifier(client).classify(make_email(BODY))
        assert slept == [2.0]
        assert result.contains_food_offer == pytest.approx(0.8)
        assert result.meta.retry_count == 1

    def test_invalid_json_is_an_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = classifier(client).classify(make_email(BODY))
        assert result.error_code == "bad_json"

    def test_a_missing_key_is_rejected_at_construction(self):
        with pytest.raises(ValueError):
            JevClassifier("", questions=QUESTIONS)


class TestRouting:
    def test_high_score_extracts(self):
        client = client_returning({"answers": {"contains_food_offer": {"noul": 0.95}}})
        result = classifier(client).classify(make_email(BODY))
        assert (
            route_for(result, make_email(BODY), mode="gate", reject_below=0.05, accept_above=0.7)
            is Route.EXTRACT
        )

    def test_observe_mode_never_drops_a_negative(self):
        """This is what makes observe mode safe to enable first."""
        client = client_returning({"answers": {"contains_food_offer": {"noul": 0.01}}})
        result = classifier(client).classify(make_email(BODY))
        assert (
            route_for(result, make_email(BODY), mode="observe", reject_below=0.05, accept_above=0.7)
            is Route.EXTRACT
        )

    def test_gate_mode_drops_a_confident_negative(self):
        client = client_returning({"answers": {"contains_food_offer": {"noul": 0.01}}})
        result = classifier(client).classify(make_email(BODY))
        assert (
            route_for(result, make_email(BODY), mode="gate", reject_below=0.05, accept_above=0.7)
            is Route.PROVISIONAL_REJECT
        )

    def test_contradictory_category_blocks_a_gate_rejection(self):
        """Low Noul but a food category is uncertainty, not a licence to drop."""
        client = client_returning(
            {
                "answers": {
                    "contains_food_offer": {"noul": 0.01},
                    "food_category": {"choice": "restaurant"},
                }
            }
        )
        result = classifier(client).classify(make_email(BODY))
        assert (
            route_for(result, make_email(BODY), mode="gate", reject_below=0.05, accept_above=0.7)
            is Route.EXTRACT_WITH_REVIEW_FLAG
        )

    def test_incomplete_input_is_never_gated_out(self):
        """Completeness is checked before the score, deliberately."""
        client = client_returning({"answers": {"contains_food_offer": {"noul": 0.001}}})
        email = make_email(BODY, has_unparsed_visuals=True)
        result = classifier(client).classify(email)
        assert (
            route_for(result, email, mode="gate", reject_below=0.05, accept_above=0.7)
            is Route.NEEDS_RICHER_INPUT
        )

    def test_middle_score_is_extracted_with_a_review_flag(self):
        client = client_returning({"answers": {"contains_food_offer": {"noul": 0.4}}})
        result = classifier(client).classify(make_email(BODY))
        assert (
            route_for(result, make_email(BODY), mode="gate", reject_below=0.05, accept_above=0.7)
            is Route.EXTRACT_WITH_REVIEW_FLAG
        )

    def test_provider_failure_never_becomes_a_negative_label(self):
        client = client_returning({}, status=500)
        result = classifier(client, max_retries=0).classify(make_email(BODY))
        assert (
            route_for(result, make_email(BODY), mode="gate", reject_below=0.05, accept_above=0.7)
            is Route.FALLBACK_PENDING
        )
