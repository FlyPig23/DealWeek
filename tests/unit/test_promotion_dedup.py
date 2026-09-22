from datetime import UTC, datetime

import httpx

from weekly_deals.models.jev import JevClassifier
from weekly_deals.promotions.deduplicate import deduplicate_promotions
from weekly_deals.schemas import PromotionEvent


def event(number: int, **changes) -> PromotionEvent:
    return PromotionEvent(
        promotion_id=f"offer-{number}", message_id=f"mail-{number}", merchant="Example Shop",
        title=changes.pop("title", "Summer sale: save 25%"), category="retail",
        benefit_hint=changes.pop("benefit_hint", "25% off sitewide"),
        source_date=datetime(2026, 9, number, tzinfo=UTC), **changes,
    )


def messages(events, bodies=None):
    return {e.message_id: {"subject": e.title, "sender": "sale@example.com",
                          "body_hash": f"body-{i}",
                          "normalized_text": (bodies or ["Same summer campaign"] * len(events))[i]}
            for i, e in enumerate(events)}


def classifier(handler):
    return JevClassifier("test", max_retries=0, client=httpx.Client(
        transport=httpx.MockTransport(handler)))


def yes(request):
    return httpx.Response(200, json={"model": "jev-1.13.0",
                         "answers": {"same_promotion": {"noul": 0.999}},
                         "usage": {"input_tokens": 100, "output_tokens": 1}})


def test_reminders_group_preserve_sources_and_cache_avoids_new_calls():
    events = [event(1), event(2, title="Last chance: summer sale saves 25%")]
    records = messages(events)
    client = classifier(yes)
    result = deduplicate_promotions(events, records, client)
    assert result["groups"] == [{"representative_id": "offer-2",
                                  "member_ids": ["offer-2", "offer-1"]}]
    assert result["stats"]["calls"] == 1
    assert result["stats"]["estimated_cost_usd"] > 0
    rerun = deduplicate_promotions(events, records, client, cache=result["cache"])
    assert rerun["stats"]["calls"] == 0
    assert rerun["stats"]["cache_hits"] == 1
    records["mail-1"]["body_hash"] = "changed-body"
    changed = deduplicate_promotions(events, records, client, cache=result["cache"])
    assert changed["stats"]["calls"] == 1


def test_different_coupon_codes_and_amounts_are_not_merged():
    events = [event(1), event(2), event(3, title="Summer sale: save 50%",
                                              benefit_hint="50% off sitewide")]
    result = deduplicate_promotions(events, messages(events, [
        "Use code FIRST25", "Use code SECOND25", "Use code THIRD50"]), classifier(yes))
    assert result["stats"]["group_count"] == 3
    assert result["stats"]["calls"] == 0


def test_provider_failure_keeps_entries_and_reports_unknown_cost():
    events = [event(1), event(2), event(3)]
    result = deduplicate_promotions(events, messages(events), classifier(
        lambda request: httpx.Response(400, json={"error": "bad request"})))
    assert result["stats"]["group_count"] == 3
    assert result["stats"]["failures"] == 1
    assert result["stats"]["cost_known"] is False
    assert result["stats"]["limit_reached"] == "cost_unknown"
    assert result["cache"] == []


def test_uncertain_answer_budget_cap_and_zero_budget_semantics():
    events = [event(1), event(2)]
    client = classifier(lambda request: httpx.Response(200, json={
        "answers": {"same_promotion": {"noul": 0.72}}, "usage": {"input_tokens": 100}}))
    uncertain = deduplicate_promotions(events, messages(events), client)
    assert uncertain["stats"]["group_count"] == 2
    bounded = deduplicate_promotions(events, messages(events), client, budget_usd=0.000001)
    assert bounded["stats"]["calls"] == 0
    assert bounded["stats"]["group_count"] == 2
    assert bounded["stats"]["limit_reached"] == "budget"
    uncapped = deduplicate_promotions(events, messages(events), client, budget_usd=0)
    assert uncapped["stats"]["calls"] == 1
