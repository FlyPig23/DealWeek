"""Reason codes.

Every recommendation is explained by codes emitted from deterministic rules. A
second model call is never used to invent a justification after the fact -- that
is how a system ends up with a persuasive reason for a wrong decision.
"""

from __future__ import annotations

from enum import StrEnum


class ReasonCode(StrEnum):
    EXPIRES_SOON = "EXPIRES_SOON"
    VALID_NEXT_WEEK = "VALID_NEXT_WEEK"
    VALID_THIS_MONTH = "VALID_THIS_MONTH"
    CLAIM_FIRST = "CLAIM_FIRST"
    CLAIM_DEADLINE_PASSED = "CLAIM_DEADLINE_PASSED"
    CLAIM_DEADLINE_UNKNOWN = "CLAIM_DEADLINE_UNKNOWN"
    MIN_SPEND_TOO_HIGH = "MIN_SPEND_TOO_HIGH"
    MIN_SPEND_ABOVE_HABIT = "MIN_SPEND_ABOVE_HABIT"
    UNKNOWN_ELIGIBILITY = "UNKNOWN_ELIGIBILITY"
    INELIGIBLE = "INELIGIBLE"
    UNKNOWN_DEADLINE = "UNKNOWN_DEADLINE"
    EXPIRED = "EXPIRED"
    NOT_YET_ACTIVE = "NOT_YET_ACTIVE"
    CONFLICTING_TERMS = "CONFLICTING_TERMS"
    NEEDS_VISUAL_PARSE = "NEEDS_VISUAL_PARSE"
    EVIDENCE_UNVERIFIED = "EVIDENCE_UNVERIFIED"
    WEEKDAY_RESTRICTED = "WEEKDAY_RESTRICTED"
    PREFERRED_MERCHANT = "PREFERRED_MERCHANT"
    EXCLUDED_MERCHANT = "EXCLUDED_MERCHANT"
    FITS_PLANNED_SLOT = "FITS_PLANNED_SLOT"
    NO_SLOT_AVAILABLE = "NO_SLOT_AVAILABLE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    WEEKLY_LIMIT_REACHED = "WEEKLY_LIMIT_REACHED"
    USER_DISMISSED = "USER_DISMISSED"
    USER_USED = "USER_USED"
    USER_LOCKED = "USER_LOCKED"
    ALTERNATIVE_CHOSEN = "ALTERNATIVE_CHOSEN"
    LONGER_WINDOW_DEFERRED = "LONGER_WINDOW_DEFERRED"


# Deliberately plain wording. Nothing here promises the merchant will honour it.
_ZH: dict[ReasonCode, str] = {
    ReasonCode.EXPIRES_SOON: "即将到期",
    ReasonCode.VALID_NEXT_WEEK: "下周有明确可用日期",
    ReasonCode.VALID_THIS_MONTH: "本月内可用",
    ReasonCode.CLAIM_FIRST: "需要先领取或激活",
    ReasonCode.CLAIM_DEADLINE_PASSED: "领取截止时间已过",
    ReasonCode.CLAIM_DEADLINE_UNKNOWN: "需要领取，但邮件未写领取截止时间",
    ReasonCode.MIN_SPEND_TOO_HIGH: "门槛金额超出预算",
    ReasonCode.MIN_SPEND_ABOVE_HABIT: "门槛高于你平时的花费，用券反而多花钱",
    ReasonCode.UNKNOWN_ELIGIBILITY: "资格条件未确认",
    ReasonCode.INELIGIBLE: "不满足使用条件",
    ReasonCode.UNKNOWN_DEADLINE: "邮件未写截止日期，有效期未知",
    ReasonCode.EXPIRED: "已过期",
    ReasonCode.NOT_YET_ACTIVE: "尚未开始",
    ReasonCode.CONFLICTING_TERMS: "条款存在冲突",
    ReasonCode.NEEDS_VISUAL_PARSE: "关键细则在图片里，尚未解析",
    ReasonCode.EVIDENCE_UNVERIFIED: "关键信息缺少原文证据",
    ReasonCode.WEEKDAY_RESTRICTED: "限定星期使用",
    ReasonCode.PREFERRED_MERCHANT: "你偏好的商家",
    ReasonCode.EXCLUDED_MERCHANT: "你已排除的商家",
    ReasonCode.FITS_PLANNED_SLOT: "与你已计划的用餐时间吻合",
    ReasonCode.NO_SLOT_AVAILABLE: "本周没有可安排的餐位",
    ReasonCode.BUDGET_EXCEEDED: "超出预算上限",
    ReasonCode.WEEKLY_LIMIT_REACHED: "已达到本周外食次数上限",
    ReasonCode.USER_DISMISSED: "你已忽略",
    ReasonCode.USER_USED: "你已标记使用",
    ReasonCode.USER_LOCKED: "你已锁定此安排",
    ReasonCode.ALTERNATIVE_CHOSEN: "同组中已选择另一个更合适的优惠",
    ReasonCode.LONGER_WINDOW_DEFERRED: "有效期较长，先留到后面",
}

_EN: dict[ReasonCode, str] = {
    ReasonCode.EXPIRES_SOON: "expires soon",
    ReasonCode.VALID_NEXT_WEEK: "has a stated window next week",
    ReasonCode.VALID_THIS_MONTH: "usable later this month",
    ReasonCode.CLAIM_FIRST: "must be claimed or activated first",
    ReasonCode.CLAIM_DEADLINE_PASSED: "the claim deadline has passed",
    ReasonCode.CLAIM_DEADLINE_UNKNOWN: "a claim is required but no deadline was stated",
    ReasonCode.MIN_SPEND_TOO_HIGH: "minimum spend is above your budget",
    ReasonCode.MIN_SPEND_ABOVE_HABIT: "minimum spend is above what you would normally spend",
    ReasonCode.UNKNOWN_ELIGIBILITY: "eligibility is not confirmed",
    ReasonCode.INELIGIBLE: "conditions are not met",
    ReasonCode.UNKNOWN_DEADLINE: "no end date stated, validity unknown",
    ReasonCode.EXPIRED: "expired",
    ReasonCode.NOT_YET_ACTIVE: "not started yet",
    ReasonCode.CONFLICTING_TERMS: "the terms contradict each other",
    ReasonCode.NEEDS_VISUAL_PARSE: "key terms are in an unparsed image",
    ReasonCode.EVIDENCE_UNVERIFIED: "key values lack verbatim evidence",
    ReasonCode.WEEKDAY_RESTRICTED: "restricted to specific weekdays",
    ReasonCode.PREFERRED_MERCHANT: "a merchant you prefer",
    ReasonCode.EXCLUDED_MERCHANT: "a merchant you excluded",
    ReasonCode.FITS_PLANNED_SLOT: "fits a meal slot you already planned",
    ReasonCode.NO_SLOT_AVAILABLE: "no meal slot available this week",
    ReasonCode.BUDGET_EXCEEDED: "over the budget limit",
    ReasonCode.WEEKLY_LIMIT_REACHED: "weekly dining-out limit reached",
    ReasonCode.USER_DISMISSED: "you dismissed it",
    ReasonCode.USER_USED: "you marked it used",
    ReasonCode.USER_LOCKED: "you locked this slot",
    ReasonCode.ALTERNATIVE_CHOSEN: "another offer in the same exclusive group was chosen",
    ReasonCode.LONGER_WINDOW_DEFERRED: "long validity, deferred for later",
}


def explain(code: ReasonCode | str, language: str = "zh-CN") -> str:
    try:
        key = ReasonCode(code)
    except ValueError:
        return str(code)
    table = _ZH if language.lower().startswith("zh") else _EN
    return table.get(key, str(key))


def explain_all(codes: list[str], language: str = "zh-CN") -> list[str]:
    return [explain(code, language) for code in codes]
