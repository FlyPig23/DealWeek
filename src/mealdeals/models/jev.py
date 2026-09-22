"""JEV (TypeSafe) classifier adapter.

Implemented against the documented HTTP contract rather than an SDK, so the
retry budget, timeout, usage accounting and cache key are all under our control.

Endpoint: ``POST https://api.typesafe.ai/v1/systemone``
Body:     ``{"model": ..., "state": {...}, "questions": {...}}``
Answers:  ``result["answers"][<question key>]``

Three behaviours the plan calls out and this file enforces:

* The Noul answer is a probability under the ``noul`` key. There is no
  ``confidence`` field on a Noul answer and none is read. The current prompt
  asks for ``contains_promotion``; the older food-only key remains accepted
  for cached results and compatibility tests.
* 401/403 are not retried. A permissions problem is reported, not worked around.
* A call that fails is an error, never a "not a promotion" verdict.
"""

from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path
from typing import Any

import httpx

from ..schemas import (
    ClassificationResult,
    FoodCategory,
    NormalizedEmail,
    ProviderMeta,
    Route,
    Usage,
)
from .base import FoodClassifier, ModelError

# Verified at planning time; re-check before release and keep the version tag.
PRICE_VERSION = "2026-09-19"
INPUT_USD_PER_MTOK = 0.042
OUTPUT_USD_PER_MTOK = 0.0

# Packaged with the code for the same reason as the extraction prompt: the
# question wording *is* the classifier's contract.
DEFAULT_QUESTIONS_PATH = Path(__file__).resolve().parent.parent / "prompts" / "jev_promotion_v1.json"


def load_questions(path: str | Path = DEFAULT_QUESTIONS_PATH) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    questions = payload.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise ValueError(f"{path} does not define a 'questions' object")
    return payload


def _estimate_cost(usage: dict[str, Any] | None) -> Usage:
    """Cost from reported usage only. Missing usage is 'unknown', never zero."""
    if not isinstance(usage, dict):
        return Usage(cost_known=False)
    input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
    output_tokens = usage.get("output_tokens") or usage.get("completion_tokens")
    if input_tokens is None:
        return Usage(input_tokens=None, output_tokens=output_tokens, cost_known=False)
    cost = (int(input_tokens) / 1_000_000) * INPUT_USD_PER_MTOK
    if output_tokens:
        cost += (int(output_tokens) / 1_000_000) * OUTPUT_USD_PER_MTOK
    return Usage(
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens) if output_tokens is not None else None,
        estimated_cost_usd=round(cost, 8),
        cost_known=True,
    )


def _valid_probability(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
    )


class JevClassifier(FoodClassifier):
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "jev-latest",
        endpoint: str = "https://api.typesafe.ai/v1/systemone",
        questions: dict[str, Any] | None = None,
        prompt_version: str = "jev_promotion_v1",
        timeout: float = 60.0,
        max_retries: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("JEV api key is required")
        self._api_key = api_key.strip()
        self.model = model
        self.endpoint = endpoint
        self.prompt_version = prompt_version
        self.timeout = timeout
        self.max_retries = max_retries
        self._questions = questions or load_questions()["questions"]
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    @property
    def model_id(self) -> str:
        # The configured id, not the version the API resolved it to: the cache
        # key has to be knowable before the call is made.
        return self.model

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> JevClassifier:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """POST with a bounded retry budget. Returns (body, retry_count)."""
        attempt = 0
        last_error: Exception | None = None

        while attempt <= self.max_retries:
            try:
                response = self._client.post(
                    self.endpoint,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt == self.max_retries:
                    raise ModelError("JEV request timed out", code="timeout", retryable=True) from exc
                self._sleep(attempt, None)
                attempt += 1
                continue
            except httpx.HTTPError as exc:
                raise ModelError("JEV network error", code="network", retryable=True) from exc

            if response.status_code in (401, 403):
                # Never retried, never escalated to a broader scope.
                raise ModelError(
                    f"JEV rejected the credentials (HTTP {response.status_code}); "
                    "check the API key and its permissions",
                    code="auth",
                    retryable=False,
                )
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == self.max_retries:
                    raise ModelError(
                        f"JEV returned HTTP {response.status_code} after retries",
                        code=f"http_{response.status_code}",
                        retryable=True,
                    )
                self._sleep(attempt, response.headers.get("Retry-After"))
                attempt += 1
                continue
            if response.status_code >= 400:
                # Response bodies are not echoed: they may contain email text.
                raise ModelError(
                    f"JEV returned HTTP {response.status_code}",
                    code=f"http_{response.status_code}",
                    retryable=False,
                )

            try:
                return response.json(), attempt
            except json.JSONDecodeError as exc:
                raise ModelError("JEV returned invalid JSON", code="bad_json") from exc

        raise ModelError("JEV retries exhausted", code="retries") from last_error

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(30.0, float(retry_after)))
                return
            except ValueError:
                pass
        # Exponential backoff with jitter, capped.
        time.sleep(min(8.0, (2**attempt) * 0.5 + random.uniform(0, 0.25)))

    # -- classification ----------------------------------------------------

    def classify(self, email: NormalizedEmail) -> ClassificationResult:
        meta_base = {
            "provider": "typesafe",
            "model": self.model,
            "prompt_version": self.prompt_version,
        }
        payload = {
            "model": self.model,
            # The email is supplied as opaque state, never merged into instructions.
            "state": {"subject": email.subject, "body": email.normalized_text},
            "questions": self._questions,
        }

        started = time.perf_counter()
        try:
            body, retries = self._post(payload)
        except ModelError as exc:
            return ClassificationResult(
                message_id=email.source_id,
                body_hash=email.content_hash,
                route=Route.FALLBACK_PENDING,
                error_code=exc.code,
                meta=ProviderMeta(
                    **meta_base,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    usage=Usage(cost_known=False),
                ),
                reasons=[str(exc)],
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        answers = body.get("answers")
        if not isinstance(answers, dict):
            return ClassificationResult(
                message_id=email.source_id,
                body_hash=email.content_hash,
                route=Route.FALLBACK_PENDING,
                error_code="bad_schema",
                meta=ProviderMeta(**meta_base, latency_ms=latency_ms, usage=Usage(cost_known=False)),
                reasons=["response had no 'answers' object"],
            )

        probability: float | None = None
        promotion_probability: float | None = None
        promotion_answer = answers.get("contains_promotion")
        if isinstance(promotion_answer, dict) and _valid_probability(promotion_answer.get("noul")):
            promotion_probability = float(promotion_answer["noul"])
            probability = promotion_probability
        food_answer = answers.get("contains_food_offer")
        if probability is None and isinstance(food_answer, dict) and _valid_probability(
            food_answer.get("noul")
        ):
            probability = float(food_answer["noul"])

        category: FoodCategory | None = None
        category_confidence: float | None = None
        choice_answer = answers.get("food_category")
        if isinstance(choice_answer, dict):
            raw_choice = choice_answer.get("choice")
            if isinstance(raw_choice, str):
                try:
                    category = FoodCategory(raw_choice)
                except ValueError:
                    category = FoodCategory.OTHER_OR_UNCLEAR
            # Choice confidence is a statistic over the distribution -- it is not
            # an accuracy estimate and is only stored, never thresholded alone.
            if _valid_probability(choice_answer.get("confidence")):
                category_confidence = float(choice_answer["confidence"])

        promotion_category = None
        promotion_choice = answers.get("promotion_category")
        if isinstance(promotion_choice, dict) and isinstance(promotion_choice.get("choice"), str):
            promotion_category = promotion_choice["choice"]

        if probability is None:
            return ClassificationResult(
                message_id=email.source_id,
                body_hash=email.content_hash,
                food_category=category,
                contains_promotion=promotion_probability,
                promotion_category=promotion_category,
                route=Route.FALLBACK_PENDING,
                error_code="missing_noul",
                meta=ProviderMeta(
                    **{**meta_base, "model": body.get("model") or self.model},
                    latency_ms=latency_ms,
                    usage=_estimate_cost(body.get("usage")),
                    retry_count=retries,
                ),
                reasons=["no valid Noul probability in the response"],
            )

        return ClassificationResult(
            message_id=email.source_id,
            body_hash=email.content_hash,
            contains_food_offer=probability,
            contains_promotion=promotion_probability,
            food_category=category,
            promotion_category=promotion_category,
            category_confidence=category_confidence,
            route=Route.EXTRACT,  # routing is decided by the router, not here
            meta=ProviderMeta(
                **{**meta_base, "model": body.get("model") or self.model},
                latency_ms=latency_ms,
                usage=_estimate_cost(body.get("usage")),
                retry_count=retries,
            ),
        )


def route_for(
    result: ClassificationResult,
    email: NormalizedEmail,
    *,
    mode: str,
    reject_below: float,
    accept_above: float,
) -> Route:
    """Decide what to do with a classified email.

    Ordering matters: input completeness is checked *before* the score, so a
    truncated or image-only email is never discarded as a confident negative.
    """
    if not email.safe_for_negative_conclusion:
        return Route.NEEDS_RICHER_INPUT
    if result.failed:
        return Route.FALLBACK_PENDING

    probability = (
        result.contains_promotion
        if result.contains_promotion is not None
        else result.contains_food_offer
    )
    if probability is None:
        return Route.FALLBACK_PENDING

    # A category from the Choice question contradicting a low Noul score is
    # uncertainty, not a licence to drop the email.
    contradictory = result.food_category in (
        FoodCategory.RESTAURANT,
        FoodCategory.DRINK,
        FoodCategory.DELIVERY,
        FoodCategory.GROCERY,
        FoodCategory.MIXED,
    ) or result.promotion_category not in (None, "other")

    if probability >= accept_above:
        return Route.EXTRACT
    if probability <= reject_below and not contradictory:
        # Only gate mode may act on this; observe mode still extracts.
        return Route.PROVISIONAL_REJECT if mode == "gate" else Route.EXTRACT
    return Route.EXTRACT_WITH_REVIEW_FLAG
