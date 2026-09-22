"""Conservative, reversible JEV grouping of promotion calendar entries.

Original messages/events remain untouched. The result is a display grouping,
with its source IDs and judgments retained. Cheap merchant and text matching
shortlists candidates; only a successful JEV judgment can merge them.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from difflib import SequenceMatcher
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from ..models.base import ModelError
from ..models.jev import INPUT_USD_PER_MTOK, JevClassifier, _estimate_cost, _valid_probability
from ..schemas import PromotionEvent

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "jev_dedup_v2.json"
VERSION = "jev_dedup_v2"
_TERMS = re.compile(r"discount|off\b|save|code|coupon|expire|valid|ends?|minimum|only|eligible|优惠|折|满|截止", re.I)
_CODE = re.compile(r"\b(?:promo(?:tion)?\s+code|coupon\s+code|use\s+code|code)\s*[:：]?\s*([A-Z0-9][A-Z0-9_-]{2,30})\b", re.I)
_AMOUNT = re.compile(r"\$\s*\d+(?:[,.]\d+)*|\b\d+(?:\.\d+)?\s*%")


def _get(record: Any, key: str, default: Any = "") -> Any:
    return record.get(key, default) if isinstance(record, dict) else getattr(record, key, default)


def _normal(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def _excerpt(text: str) -> dict[str, Any]:
    # MIME alternatives can leave CSS/HTML in otherwise normalized text. Strip
    # that noise before the excerpt budget, preserving visible text and alt text.
    # This model view never changes the original message or fetches image URLs.
    clean = text
    if re.search(r"<[a-zA-Z][^>]*>", clean):
        soup = BeautifulSoup(clean, "html.parser")
        for element in soup(["style", "script"]):
            element.decompose()
        for image in soup.find_all("img"):
            image.replace_with(" " + str(image.get("alt") or "") + " ")
        clean = soup.get_text("\n", strip=True)
    clean = "".join(char for char in clean
                    if unicodedata.category(char) != "Cf" and char != "\u034f")
    clean = re.sub(r"https?://\S+", "[link omitted]", clean)
    clean = "\n".join(" ".join(line.split()) for line in clean.splitlines() if line.strip())
    if len(clean) <= 4500:
        return {"text": clean, "is_excerpt": False}
    terms = [line[:500] for line in clean.splitlines() if _TERMS.search(line)]
    excerpt = clean[:1800] + "\n[... excerpt ...]\n" + "\n".join(terms)[:1800]
    excerpt += "\n[... final excerpt ...]\n" + clean[-700:]
    return {"text": excerpt, "is_excerpt": True}


def _state(event: PromotionEvent, message: Any) -> dict[str, Any]:
    return {
        "merchant": event.merchant,
        "subject": _get(message, "subject") or event.title,
        "sender": _get(message, "sender"),
        "received_or_sent_at": event.source_date.isoformat() if event.source_date else None,
        "calendar_title": event.title,
        "benefit_hint": event.benefit_hint,
        "parsed_start_date": event.start_date.isoformat() if event.start_date else None,
        "parsed_end_date": event.end_date.isoformat() if event.end_date else None,
        "date_expression": event.date_expression,
        "evidence": event.evidence,
        "body_complete": event.body_complete and _get(message, "body_complete", True),
        "has_unparsed_visuals": event.has_unparsed_visuals,
        "body": _excerpt(_get(message, "normalized_text")),
    }


def _identity(event: PromotionEvent, message: Any) -> list[Any]:
    # Reuse the store's body identity; include editable calendar fields so a
    # corrected date/benefit cannot silently reuse an obsolete judgment.
    return [event.message_id, _get(message, "body_hash"), event.promotion_id,
            event.title, event.benefit_hint, str(event.start_date), str(event.end_date),
            event.date_expression, event.evidence]


def _signature(event: PromotionEvent, message: Any) -> str:
    return _normal(" ".join(filter(None, [event.title, event.benefit_hint,
                                          _get(message, "subject")])))


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    lt, rt = set(left.split()), set(right.split())
    overlap = len(lt & rt) / max(1, min(len(lt), len(rt)))
    return max(overlap, SequenceMatcher(None, left[:500], right[:500]).ratio())


def _conflict(left: PromotionEvent, right: PromotionEvent, messages: dict[str, Any]) -> bool:
    def terms(event: PromotionEvent) -> tuple[set[str], set[str]]:
        # Amounts from the summary, not unrelated footer prices or newsletters.
        summary = " ".join(filter(None, [event.title, event.benefit_hint]))
        evidence = summary + " " + (event.evidence or "")
        evidence += " " + _get(messages.get(event.message_id), "normalized_text")
        codes = {c.upper() for c in _CODE.findall(evidence)
                 if c.upper() not in {"AND", "THE", "FOR", "WITH", "BELOW", "HERE"}}
        amounts = {re.sub(r"[\s,]", "", value) for value in _AMOUNT.findall(summary)}
        return codes, amounts
    lc, la = terms(left)
    rc, ra = terms(right)
    if (lc and rc and lc.isdisjoint(rc)) or (la and ra and la.isdisjoint(ra)):
        return True
    return bool((left.start_date and right.end_date and left.start_date > right.end_date)
                or (right.start_date and left.end_date and right.start_date > left.end_date))


def deduplicate_promotions(
    events: list[PromotionEvent],
    messages: dict[str, Any],
    classifier: JevClassifier,
    *,
    cache: list[dict[str, Any]] | None = None,
    threshold: float = 0.90,
    budget_usd: float = 1.0,
    max_comparisons: int = 5000,
    workers: int = 6,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Group duplicate campaigns after the caller enforces JEV consent.

    Each merchant is processed against at most six similar representatives per
    event; merchants run concurrently. Every member is checked against its
    representative, never joined solely through a transitive chain. Unknown
    answers/errors keep both entries. A missing cost stops new paid requests.
    ``estimated_cost_usd`` is this invocation's known cost, excluding cache hits.
    A zero budget disables the dollar cap, matching the runtime configuration;
    the comparison count and unknown-cost stop still apply.
    """
    if not 0.5 <= threshold <= 1 or budget_usd < 0 or max_comparisons < 0:
        raise ValueError("Invalid deduplication threshold, budget, or comparison limit")
    prompt = json.loads(PROMPT_PATH.read_text(encoding="utf-8"))
    saved = {d["cache_key"]: d for d in (cache or [])
             if d.get("cache_key") and not d.get("error_code")
             and _valid_probability(d.get("probability"))}
    decisions: list[dict[str, Any]] = []
    lock = threading.Lock()
    stats: dict[str, Any] = {
        "input_count": len(events), "calls": 0, "cache_hits": 0, "failures": 0,
        "estimated_cost_usd": 0.0, "cost_known": True, "limit_reached": None,
    }
    reserved = 0.0

    def judge(left: PromotionEvent, right: PromotionEvent) -> dict[str, Any] | None:
        nonlocal reserved
        lm, rm = messages.get(left.message_id), messages.get(right.message_id)
        key = json.dumps([VERSION, classifier.model_id, _identity(left, lm),
                          _identity(right, rm)], ensure_ascii=False, separators=(",", ":"))
        with lock:
            cached = saved.get(key)
            if cached:
                result = {**cached, "cached": True,
                          "duplicate": cached["probability"] >= threshold}
                decisions.append(result)
                stats["cache_hits"] += 1
                return result
        payload = {"model": classifier.model_id,
                   "state": {"left": _state(left, lm), "right": _state(right, rm)},
                   "questions": prompt["questions"]}
        # Reserve a deliberately conservative byte-based token bound, including
        # retries, before parallel work. Settlement uses actual reported usage.
        upper_cost = ((len(json.dumps(payload, ensure_ascii=False).encode()) + 2048)
                      * INPUT_USD_PER_MTOK / 1_000_000 * (classifier.max_retries + 1))
        with lock:
            if stats["limit_reached"]:
                return None
            if stats["calls"] >= max_comparisons:
                stats["limit_reached"] = "max_comparisons"
                return None
            if budget_usd > 0 and stats["estimated_cost_usd"] + reserved + upper_cost > budget_usd:
                stats["limit_reached"] = "budget"
                return None
            reserved += upper_cost
            stats["calls"] += 1
        result = {"left_id": left.promotion_id, "right_id": right.promotion_id,
                  "probability": None, "duplicate": False, "error_code": None,
                  "cached": False, "model": classifier.model_id, "cache_key": key}
        try:
            body, retries = classifier._post(payload)
            usage = _estimate_cost(body.get("usage"))
            result.update(model=body.get("model") or classifier.model_id,
                          usage=usage.model_dump(mode="json"), retry_count=retries)
            answers = body.get("answers")
            answer = answers.get("same_promotion") if isinstance(answers, dict) else None
            probability = answer.get("noul") if isinstance(answer, dict) else None
            if not _valid_probability(probability):
                result["error_code"] = "missing_noul"
            else:
                result.update(probability=float(probability), duplicate=probability >= threshold)
        except ModelError as exc:
            result.update(error_code=exc.code, usage={"cost_known": False,
                          "estimated_cost_usd": None, "input_tokens": None,
                          "output_tokens": None})
        with lock:
            reserved -= upper_cost
            usage_dict = result["usage"]
            if usage_dict.get("cost_known"):
                stats["estimated_cost_usd"] += usage_dict["estimated_cost_usd"] or 0
            else:
                stats["cost_known"] = False
                stats["limit_reached"] = "cost_unknown"
            if result["error_code"]:
                stats["failures"] += 1
            else:
                saved[key] = result
            decisions.append(result)
        return result

    buckets: dict[str, list[PromotionEvent]] = defaultdict(list)
    signatures = {}
    ungrouped = []
    today = as_of or date.today()
    for event in events:
        message = messages.get(event.message_id)
        calls = _get(message, "classifications", [])
        successful = [call for call in calls if not _get(call, "error_code", None)]
        probability = _get(successful[-1], "payload", {}).get("contains_promotion") if successful else None
        if _valid_probability(probability) and probability < 0.70:
            # Classification remains observe-only: retain these source records,
            # while spending duplicate checks on likely offers first.
            ungrouped.append({"representative_id": event.promotion_id,
                              "member_ids": [event.promotion_id]})
            continue
        merchant = _normal(event.merchant)
        sender = parseaddr(_get(message, "sender"))[1].casefold()
        key = merchant if merchant not in {"", "unknown", "unknown merchant"} else sender
        buckets[key or event.promotion_id].append(event)
        signatures[event.promotion_id] = _signature(event, message)

    def group_bucket(bucket: list[PromotionEvent]) -> list[dict[str, Any]]:
        bucket.sort(key=lambda e: (e.source_date.isoformat() if e.source_date else "",
                                   e.promotion_id), reverse=True)
        groups: list[list[PromotionEvent]] = []
        for event in bucket:
            # Work is linear in the number of established campaigns, within a
            # merchant bucket; paid comparisons are separately bounded.
            candidates = sorted(
                ((_similarity(signatures[event.promotion_id], signatures[g[0].promotion_id]), g)
                 for g in groups), key=lambda pair: pair[0], reverse=True,
            )[:6]
            for similarity, group in candidates:
                if similarity < 0.30 or any(_conflict(event, e, messages) for e in group):
                    continue
                decision = judge(group[0], event)
                if decision and decision["duplicate"]:
                    group.append(event)
                    break
            else:
                groups.append([event])
        return [{"representative_id": group[0].promotion_id,
                 "member_ids": [event.promotion_id for event in group]} for group in groups]

    ordered_buckets = sorted(buckets.values(), key=lambda bucket: (
        not any(event.end_date and event.end_date >= today for event in bucket),
        min((event.end_date for event in bucket if event.end_date and event.end_date >= today),
            default=date.max),
    ))
    with ThreadPoolExecutor(max_workers=max(1, min(16, workers))) as pool:
        groups = ungrouped + [group for result in pool.map(group_bucket, ordered_buckets) for group in result]
    stats.update(group_count=len(groups), duplicates_removed=len(events) - len(groups),
                 noncandidate_messages=len(ungrouped),
                 estimated_cost_usd=round(stats["estimated_cost_usd"], 8))
    return {"version": VERSION, "model": classifier.model_id, "threshold": threshold,
            "groups": groups, "decisions": decisions, "cache": list(saved.values()), "stats": stats}
