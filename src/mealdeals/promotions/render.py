"""Render the promotion index as a self-contained weekly calendar."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from jinja2 import Environment, select_autoescape

from ..schemas import PromotionEvent

_env = Environment(autoescape=select_autoescape(default=True, default_for_string=True))

_CATEGORY_LABELS = {
    "food": "餐饮",
    "travel": "出行",
    "events": "活动",
    "retail": "购物",
    "services": "服务",
    "other": "其他",
}
_STATUS_LABELS = {
    "expired": "已过期",
    "ending_soon": "7 天内到期",
    "active": "仍可用",
    "upcoming": "尚未开始",
    "unknown": "日期未知",
}
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_CALENDAR_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{{ title }}</title>
<style>
:root {
  --bg: #f5f7fb; --surface: #fff; --text: #172033; --muted: #6c7485;
  --line: #e4e8f0; --shadow: 0 8px 24px rgba(36, 52, 85, .08);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #11151f; --surface: #1a2130; --text: #edf2ff; --muted: #a4aec2;
    --line: #303a4e; --shadow: 0 8px 24px rgba(0, 0, 0, .24);
  }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 28px 16px 64px; background: var(--bg); color: var(--text);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
  "Microsoft YaHei", sans-serif; }
main { max-width: 1180px; margin: 0 auto; }
h1 { margin: 0; font-size: clamp(1.55rem, 3vw, 2.2rem); letter-spacing: -.02em; }
h2 { margin: 34px 0 12px; font-size: 1.12rem; }
.subtitle { color: var(--muted); margin: 5px 0 24px; }
.summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 24px; }
.metric { background: var(--surface); border: 1px solid var(--line); border-radius: 14px;
  padding: 14px 16px; box-shadow: var(--shadow); }
.metric strong { display: block; font-size: 1.45rem; }
.metric span { color: var(--muted); font-size: .82rem; }
.calendar-grid { display: grid; grid-template-columns: repeat(7, minmax(125px, 1fr));
  gap: 10px; overflow-x: auto; padding-bottom: 4px; }
.day { min-height: 235px; background: var(--surface); border: 1px solid var(--line);
  border-radius: 14px; padding: 12px; box-shadow: var(--shadow); }
.day.today { border: 2px solid #6f65e8; padding: 11px; }
.day-head { display: flex; justify-content: space-between; align-items: baseline;
  border-bottom: 1px solid var(--line); padding-bottom: 8px; margin-bottom: 9px; }
.weekday { font-weight: 700; }
.day-date { color: var(--muted); font-size: .8rem; }
.empty { color: var(--muted); font-size: .82rem; padding-top: 8px; }
.event { border-left: 4px solid var(--category); background: color-mix(in srgb, var(--category) 10%, var(--surface));
  border-radius: 9px; padding: 9px 9px 8px; margin: 8px 0; }
.event-title { font-weight: 700; line-height: 1.35; }
.merchant { color: var(--muted); font-size: .79rem; margin-top: 2px; }
.tags { margin-top: 6px; }
.tag { display: inline-block; border-radius: 999px; padding: 2px 7px; margin: 0 3px 3px 0;
  color: #fff; background: var(--category); font-size: .7rem; white-space: nowrap; }
.tag.status { background: transparent; color: var(--category); border: 1px solid var(--category); }
.benefit { font-weight: 700; margin-top: 6px; }
.deadline { color: var(--muted); font-size: .78rem; margin-top: 4px; }
details { margin-top: 7px; font-size: .78rem; }
summary { cursor: pointer; color: var(--muted); }
blockquote { margin: 5px 0 0; padding: 5px 8px; border-left: 2px solid var(--category);
  color: var(--muted); font-size: .76rem; }
.list-section { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
.list-section .event { margin: 0; min-height: 125px; }
.legend { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0 4px; }
.legend .tag { background: var(--surface); color: var(--category); border: 1px solid var(--category); }
footer { color: var(--muted); font-size: .78rem; margin-top: 34px; padding-top: 12px;
  border-top: 1px solid var(--line); }
@media (max-width: 760px) {
  .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .calendar-grid { grid-template-columns: repeat(7, 145px); }
}
</style>
</head>
<body>
<main>
  <h1>{{ title }}</h1>
  <p class="subtitle">{{ week_label }} · 数据时点 {{ as_of }} · 共 {{ total }} 条 Promotions 记录</p>
  <section class="summary" aria-label="摘要">
    <div class="metric"><strong>{{ total }}</strong><span>已收录促销</span></div>
    <div class="metric"><strong>{{ ending_soon }}</strong><span>7 天内到期</span></div>
    <div class="metric"><strong>{{ active }}</strong><span>目前仍可用</span></div>
    <div class="metric"><strong>{{ needs_review }}</strong><span>需要复核</span></div>
  </section>
  <div class="legend" aria-label="分类图例">
    {% for category in categories %}<span class="tag category-{{ category.key }}" style="--category: {{ category.color }}">{{ category.label }}</span>{% endfor %}
  </div>

  <h2>本周到期日历</h2>
  <section class="calendar-grid">
    {% for day in days %}
    <article class="day{% if day.is_today %} today{% endif %}">
      <div class="day-head"><span class="weekday">{{ day.weekday }}</span><span class="day-date">{{ day.date }}</span></div>
      {% if not day.events %}<div class="empty">暂无到期提醒</div>{% endif %}
      {% for event in day.events %}{% include "event-card" %}{% endfor %}
    </article>
    {% endfor %}
  </section>

  {% if ongoing %}
  <h2>本周仍可使用</h2>
  <section class="list-section">{% for event in ongoing %}{% include "event-card" %}{% endfor %}</section>
  {% endif %}

  {% if other %}
  <h2>其他提醒</h2>
  <section class="list-section">{% for event in other %}{% include "event-card" %}{% endfor %}</section>
  {% endif %}

  <footer>本页只依据邮件正文整理；“仍可用”不代表商家实时确认。图片内容、缺失日期和不完整正文会保留为待复核状态。</footer>
</main>
</body>
</html>
"""

_EVENT_CARD = """<div class="event" style="--category: {{ event.color }}">
  <div class="event-title">{{ event.title }}</div>
  <div class="merchant">{{ event.merchant }}</div>
  <div class="tags"><span class="tag">{{ event.category_label }}</span><span class="tag status">{{ event.status_label }}</span></div>
  {% if event.benefit_hint %}<div class="benefit">{{ event.benefit_hint }}</div>{% endif %}
  <div class="deadline">{% if event.end_date %}截止 {{ event.end_date }}{% else %}截止日期未识别{% endif %}{% if event.needs_review %} · 需复核{% endif %}</div>
  {% if event.evidence %}<details><summary>查看邮件依据</summary><blockquote>{{ event.evidence }}</blockquote></details>{% endif %}
</div>"""

_COLORS = {
    "food": "#e9784d",
    "travel": "#138a9e",
    "events": "#8c5bd6",
    "retail": "#3478d4",
    "services": "#2e9a70",
    "other": "#7b8494",
}


def _view(event: PromotionEvent) -> dict:
    return {
        "title": event.title,
        "merchant": event.merchant,
        "category_label": _CATEGORY_LABELS[event.category],
        "status_label": _STATUS_LABELS[event.status],
        "color": _COLORS[event.category],
        "benefit_hint": event.benefit_hint,
        "end_date": event.end_date.isoformat() if event.end_date else None,
        "needs_review": event.needs_review,
        "evidence": event.evidence,
    }


def render_calendar(
    events: list[PromotionEvent],
    *,
    now: datetime,
    title: str = "每周省钱日历",
) -> str:
    """Render a self-contained HTML calendar with no remote assets."""
    today = now.date()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    views = [_view(event) for event in events]

    day_events: dict[date, list[dict]] = {week_start + timedelta(days=i): [] for i in range(7)}
    ongoing: list[dict] = []
    other: list[dict] = []
    for event, view in zip(events, views, strict=True):
        deadline = event.end_date
        if deadline is not None and week_start <= deadline <= week_end:
            day_events[deadline].append(view)
        elif event.status in ("active", "upcoming") and (deadline is None or deadline > week_end):
            ongoing.append(view)
        else:
            other.append(view)

    days = [
        {
            "weekday": _WEEKDAYS[index],
            "date": current.isoformat(),
            "is_today": current == today,
            "events": day_events[current],
        }
        for index, current in enumerate(day_events)
    ]
    categories = [
        {"key": key, "label": label, "color": _COLORS[key]}
        for key, label in _CATEGORY_LABELS.items()
        if any(event.category == key for event in events)
    ]
    context = {
        "title": title,
        "week_label": f"{week_start.isoformat()} 至 {week_end.isoformat()}",
        "as_of": now.strftime("%Y-%m-%d %H:%M"),
        "total": len(events),
        "ending_soon": sum(event.status == "ending_soon" for event in events),
        "active": sum(event.status == "active" for event in events),
        "needs_review": sum(event.needs_review or event.status == "unknown" for event in events),
        "categories": categories,
        "days": days,
        "ongoing": ongoing,
        "other": other,
    }
    # Jinja's include syntax needs a loader; replace the small partial marker
    # with the already escaped card template before rendering.
    source = _CALENDAR_HTML.replace('{% include "event-card" %}', _EVENT_CARD)
    return _env.from_string(source).render(**context)
