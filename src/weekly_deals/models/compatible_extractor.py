"""OpenAI-compatible extractor.

Separate from :class:`OpenAIExtractor` on purpose. "Just change the base URL"
is not true in practice: many compatible endpoints do not implement
``response_format`` as a schema, some ignore ``temperature``, and several return
usage in a different shape or not at all.

So this adapter asks for JSON, parses it itself, allows exactly one repair
attempt, and then gives up and routes the message to human review. It also
declares its capabilities honestly rather than pretending they match OpenAI's.

Changing ``LLM_BASE_URL`` changes who receives the user's email text. That is a
consent decision, enforced in the config layer, not here.
"""

from __future__ import annotations

import json
import re
import time

from pydantic import ValidationError

from ..schemas import (
    ExtractionResult,
    ExtractionStatus,
    NormalizedEmail,
    ProviderMeta,
    Usage,
)
from .base import ModelError, OfferExtractor
from .openai_extractor import PROMPT_VERSION, build_user_payload, load_prompt
from .wire import WireExtraction, json_schema, safe_drafts

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

_SCHEMA_INSTRUCTION = (
    "\n\nReturn a single JSON object and nothing else. No prose, no markdown "
    "fence. It must match this JSON Schema:\n{schema}"
)


def extract_json(text: str) -> str | None:
    """Recover a JSON object from a response that may be wrapped in prose."""
    if not text:
        return None
    fenced = _JSON_BLOCK.search(text)
    if fenced:
        return fenced.group(1)
    bare = _BARE_OBJECT.search(text)
    return bare.group(0) if bare else None


class CompatibleExtractor(OfferExtractor):
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        *,
        prompt: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
        supports_json_mode: bool = True,
        client: object | None = None,
        prices: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("a base_url is required for the compatible provider")
        self.model = model
        self.base_url = base_url
        self.supports_json_mode = supports_json_mode
        # Only the operator knows what an arbitrary endpoint charges, so there is
        # no default table. Without one a call's cost stays unknown rather than
        # being quietly recorded as zero.
        self._prices = prices or {}
        self.prompt_version = f"{PROMPT_VERSION}+compat"
        base_prompt = prompt or load_prompt()
        self.prompt = base_prompt + _SCHEMA_INSTRUCTION.format(
            schema=json.dumps(json_schema(), ensure_ascii=False)
        )

        if client is not None:
            self._client = client
        else:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise ModelError(
                    "the 'openai' package is not installed", code="missing_dependency"
                ) from exc
            self._client = OpenAI(
                api_key=api_key or "not-required",
                base_url=base_url,
                timeout=timeout,
                max_retries=max_retries,
            )

    @property
    def model_id(self) -> str:
        return self.model

    def capabilities(self) -> dict[str, bool]:
        # Honest declaration: no native schema enforcement, usage may be absent.
        return {
            "structured_output": False,
            "json_mode": self.supports_json_mode,
            "vision": False,
            "usage_accounting": bool(self._prices),
        }

    def _priced(self, input_tokens: object, output_tokens: object) -> Usage:
        if input_tokens is None:
            return Usage(cost_known=False)
        price = self._prices.get(self.model)
        if price is None:
            return Usage(
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens) if output_tokens else None,
                cost_known=False,
            )
        cost = (int(input_tokens) / 1_000_000) * price[0]
        if output_tokens:
            cost += (int(output_tokens) / 1_000_000) * price[1]
        return Usage(
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens) if output_tokens else None,
            estimated_cost_usd=round(cost, 8),
            cost_known=True,
        )

    def _call(self, messages: list[dict[str, str]]) -> object:
        kwargs: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        }
        if self.supports_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return self._client.chat.completions.create(**kwargs)  # type: ignore[union-attr]

    def extract(self, email: NormalizedEmail) -> ExtractionResult:
        started = time.perf_counter()
        meta_base = {
            "provider": "compatible",
            "model": self.model,
            "prompt_version": self.prompt_version,
        }
        messages = [
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": build_user_payload(email)},
        ]

        def fail(code: str, retries: int = 0) -> ExtractionResult:
            return ExtractionResult(
                message_id=email.source_id,
                status=ExtractionStatus.FAILED,
                error_code=code,
                meta=ProviderMeta(
                    **meta_base,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    usage=Usage(cost_known=False),
                    retry_count=retries,
                ),
            )

        parsed: WireExtraction | None = None
        repairs = 0

        for attempt in range(2):  # original + at most one repair
            try:
                response = self._call(messages)
            except Exception as exc:
                name = type(exc).__name__.lower()
                if "timeout" in name:
                    return fail("timeout", repairs)
                if "ratelimit" in name:
                    return fail("rate_limited", repairs)
                if "authentication" in name or "permission" in name:
                    return fail("auth", repairs)
                return fail("provider_error", repairs)

            choices = getattr(response, "choices", None) or []
            if not choices:
                return fail("empty_response", repairs)
            if getattr(choices[0], "finish_reason", None) == "length":
                return fail("truncated", repairs)

            content = getattr(getattr(choices[0], "message", None), "content", None) or ""
            candidate = extract_json(content)
            if candidate:
                try:
                    parsed = WireExtraction.model_validate_json(candidate)
                    break
                except ValidationError as exc:
                    if attempt == 0:
                        repairs += 1
                        messages = [
                            *messages,
                            {"role": "assistant", "content": content[:4000]},
                            {
                                "role": "user",
                                "content": (
                                    "That response did not validate against the schema:\n"
                                    f"{str(exc)[:1500]}\n"
                                    "Return only a corrected JSON object."
                                ),
                            },
                        ]
                        continue
            elif attempt == 0:
                repairs += 1
                messages = [
                    *messages,
                    {"role": "assistant", "content": content[:2000]},
                    {"role": "user", "content": "Return only the JSON object, with no prose."},
                ]
                continue

            # Second attempt also failed: hand it to a human rather than guess.
            return fail("schema_violation", repairs)

        if parsed is None:
            return fail("schema_violation", repairs)

        usage_raw = getattr(response, "usage", None)  # type: ignore[possibly-undefined]
        input_tokens = getattr(usage_raw, "prompt_tokens", None)
        output_tokens = getattr(usage_raw, "completion_tokens", None)

        drafts, dropped = safe_drafts(parsed.offers, email.source_id)
        status = ExtractionStatus.SUCCESS
        if dropped or parsed.status == "needs_review" or any(d.needs_visual_parse for d in drafts) or (not drafts and not email.safe_for_negative_conclusion):
            status = ExtractionStatus.NEEDS_REVIEW

        return ExtractionResult(
            message_id=email.source_id,
            status=status,
            offers=drafts,
            meta=ProviderMeta(
                **meta_base,
                latency_ms=int((time.perf_counter() - started) * 1000),
                usage=self._priced(input_tokens, output_tokens),
                retry_count=repairs,
            ),
        )
