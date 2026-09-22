"""Apply saved JEV groups as a reversible calendar view."""

from __future__ import annotations

from ..schemas import PromotionEvent


def group_promotions(
    events: list[PromotionEvent],
    groups: list[dict],
    *,
    accounts: dict[str, str] | None = None,
) -> list[PromotionEvent]:
    by_id = {event.promotion_id: event for event in events}
    used: set[str] = set()
    result: list[PromotionEvent] = []
    for group in groups:
        members = [by_id[key] for key in group.get("member_ids", []) if key in by_id and key not in used]
        if not members:
            continue
        representative = next(
            (event for event in members if event.promotion_id == group.get("representative_id")),
            members[0],
        )
        used.update(event.promotion_id for event in members)
        if len(members) == 1:
            result.append(representative)
            continue
        dates = sorted({event.end_date for event in members if event.end_date is not None})
        conflict = len(dates) > 1
        missing_date = any(event.end_date is None for event in members)
        note = f"JEV 将 {len(members)} 封同一活动提醒合并展示，原始邮件均保留。"
        if conflict:
            note += " 来源截止日期不一致；按最早日期提醒，请核对原文，不能据此延长有效期。"
        elif missing_date:
            note += " 部分来源未注明截止日期，请核对完整条款。"
        ids = [event.message_id for event in members]
        source_accounts = sorted({accounts[key] for key in ids if accounts and key in accounts})
        result.append(representative.model_copy(update={
            "end_date": dates[0] if dates else None,
            "start_date": None if conflict else representative.start_date,
            "source_message_ids": ids,
            "source_accounts": source_accounts,
            "duplicate_count": len(members),
            "deadline_conflict": conflict,
            "dedup_note": note,
            "needs_review": conflict or missing_date or any(event.needs_review for event in members),
        }))
    result.extend(event for event in events if event.promotion_id not in used)
    return result
