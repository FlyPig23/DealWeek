"""Report rendering.

Templates produce the narrative. A second model call is never used to write the
explanation, because a fluent paragraph that contradicts the data is worse than
a terse one that matches it.

Every string that came from an email is escaped. Promotional HTML is never
re-emitted, and no remote image or script is referenced, so opening a report
cannot phone a merchant's tracker.
"""

from __future__ import annotations

import json
from datetime import datetime

from jinja2 import Environment, select_autoescape

from ..planning.costs import face_value
from ..planning.explanations import explain_all
from ..schemas import Coverage, PlanItem, PlanResult, ValidatedOffer

_env = Environment(autoescape=select_autoescape(default=True, default_for_string=True))

_MARKDOWN = """# {{ title }}

> 数据时点 {{ as_of }}（{{ tz }}）· 邮件范围 {{ coverage.query }} · 回溯 {{ coverage.lookback_days }} 天
> {{ coverage_line }}

{% for section in sections %}
## {{ section.heading }}

{% if not section.entries %}_本节没有条目。_
{% else %}{% for item in section.entries %}
### {{ item.merchant }} — {{ item.title }}

- 优惠：{{ item.face_value }}
- 适用日期：{{ item.slot or "未指定" }}
- 截止：{{ item.deadline or "邮件未写明截止日期" }}
- 推荐原因：{{ item.reasons | join("、") or "无" }}
{% if item.unknowns %}- 待确认：{{ item.unknowns | join("；") }}
{% endif %}{% if item.cost_note %}- 费用说明：{{ item.cost_note }}
{% endif %}{% if item.evidence %}- 原文证据：
{% for quote in item.evidence %}  - 「{{ quote }}」
{% endfor %}{% endif %}- 来源邮件：{{ item.sources | join(", ") }}
{% endfor %}{% endif %}
{% endfor %}
---

本报告按邮件信息整理，并非商家实时兑换确认；去店前请核对条款。
"""

_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{{ title }}</title>
<style>
  :root {
    --bg: #ffffff; --fg: #1a1a1a; --muted: #6b6b6b; --line: #e4e4e4;
    --accent: #8a5a2b; --warn-bg: #fdf6ec; --warn-line: #e8d5b7;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #1b1b1d; --fg: #ececec; --muted: #9a9a9a; --line: #333336;
            --accent: #d9a066; --warn-bg: #2a231a; --warn-line: #4a3d2a; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px 16px 64px; background: var(--bg); color: var(--fg);
         font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
         "Hiragino Sans GB", "Microsoft YaHei", sans-serif; }
  main { max-width: 860px; margin: 0 auto; }
  h1 { font-size: 1.7rem; margin: 0 0 4px; }
  h2 { font-size: 1.2rem; margin: 40px 0 12px; padding-bottom: 6px;
       border-bottom: 1px solid var(--line); }
  .meta { color: var(--muted); font-size: .86rem; margin-bottom: 8px; }
  .coverage { background: var(--warn-bg); border: 1px solid var(--warn-line);
              border-radius: 8px; padding: 12px 14px; font-size: .86rem; margin: 16px 0 8px; }
  .card { border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px;
          margin: 12px 0; }
  .card h3 { margin: 0 0 4px; font-size: 1.02rem; }
  .merchant { color: var(--muted); font-size: .85rem; }
  .value { color: var(--accent); font-weight: 600; }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px;
       margin: 12px 0 0; font-size: .9rem; }
  dt { color: var(--muted); }
  dd { margin: 0; }
  .tag { display: inline-block; padding: 1px 8px; border: 1px solid var(--line);
         border-radius: 999px; font-size: .78rem; margin: 0 4px 4px 0; }
  blockquote { margin: 6px 0; padding: 6px 12px; border-left: 3px solid var(--line);
               color: var(--muted); font-size: .86rem; }
  .empty { color: var(--muted); font-style: italic; }
  footer { margin-top: 48px; padding-top: 12px; border-top: 1px solid var(--line);
           color: var(--muted); font-size: .82rem; }
  @media (max-width: 520px) { dl { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<main>
  <h1>{{ title }}</h1>
  <p class="meta">数据时点 {{ as_of }}（{{ tz }}）</p>
  <div class="coverage">
    <strong>处理覆盖：</strong>{{ coverage_line }}<br>
    查询：<code>{{ coverage.query }}</code> · 回溯 {{ coverage.lookback_days }} 天
    {% if coverage.is_partial %}<br><strong>注意：</strong>本次运行未完整覆盖，
    下面的结果不代表全部优惠。{% endif %}
  </div>

  {% for section in sections %}
  <h2>{{ section.heading }}</h2>
  {% if not section.entries %}
    <p class="empty">本节没有条目。</p>
  {% else %}
    {% for item in section.entries %}
    <div class="card">
      <h3>{{ item.title }}</h3>
      <div class="merchant">{{ item.merchant }}</div>
      <p class="value">{{ item.face_value }}</p>
      <div>
        {% for reason in item.reasons %}<span class="tag">{{ reason }}</span>{% endfor %}
      </div>
      <dl>
        <dt>适用日期</dt><dd>{{ item.slot or "未指定" }}</dd>
        <dt>截止</dt><dd>{{ item.deadline or "邮件未写明截止日期" }}</dd>
        {% if item.unknowns %}<dt>待确认</dt><dd>{{ item.unknowns | join("；") }}</dd>{% endif %}
        {% if item.cost_note %}<dt>费用说明</dt><dd>{{ item.cost_note }}</dd>{% endif %}
        <dt>来源邮件</dt><dd>{{ item.sources | join(", ") }}</dd>
      </dl>
      {% for quote in item.evidence %}<blockquote>{{ quote }}</blockquote>{% endfor %}
    </div>
    {% endfor %}
  {% endif %}
  {% endfor %}

  <footer>
    本报告按邮件信息整理，并非商家实时兑换确认；去店前请核对条款。<br>
    未解析图片 {{ coverage.unparsed_visuals }} 封 · 读取失败 {{ coverage.fetch_failures }} 封 ·
    抽取失败 {{ coverage.extraction_failures }} 封
  </footer>
</main>
</body>
</html>
"""


def coverage_sentence(coverage: Coverage) -> str:
    """One line stating what the scan actually covered.

    "Found nothing" and "did not finish" have to be distinguishable here, so an
    unfinished search and any parked messages are named rather than folded into
    the success counts.
    """
    extracted = f"抽取成功 {coverage.extraction_success} 封"
    if coverage.extraction_cached:
        extracted += f"（其中 {coverage.extraction_cached} 封来自缓存，未重复付费）"
    text = (
        f"命中 {coverage.messages_matched} 封，读取成功 {coverage.messages_fetched} 封，"
        f"{extracted}，去重后 {coverage.offers_after_dedup} 条优惠"
    )
    if coverage.promotions_indexed:
        text += f"；促销日历收录 {coverage.promotions_indexed} 封"
    if coverage.search_exhaustive:
        text += "（搜索已穷尽）"
    else:
        text += "（搜索未穷尽：本次未覆盖全部范围，不代表其余邮件没有优惠）"
    if coverage.parked:
        text += f"；另有 {coverage.parked} 封因预算或供应商故障未处理"
    return text


def _item_view(item: PlanItem, offers: dict[str, ValidatedOffer], language: str) -> dict:
    offer = offers.get(item.offer_id)
    evidence: list[str] = []
    if offer is not None:
        # A merged campaign carries the same quote once per source email; show
        # each distinct quote once.
        evidence = list(dict.fromkeys(e.quote for e in offer.evidence if e.verified))[:3]
    return {
        "merchant": item.merchant,
        "title": item.title,
        "face_value": face_value(offer.benefit) if offer else "",
        "slot": item.slot_date.isoformat() if item.slot_date else None,
        "deadline": item.deadline_note,
        "reasons": explain_all(item.reason_codes, language),
        "unknowns": item.unknowns,
        "cost_note": item.cost.note,
        "evidence": evidence,
        "sources": offer.source_message_ids if offer else [],
    }


def _sections(plan: PlanResult, offers: dict[str, ValidatedOffer], language: str) -> list[dict]:
    zh = language.lower().startswith("zh")
    labels = (
        ["本周优先", "可以留到下周", "本月其他优惠", "待确认 / 暂不推荐"]
        if zh
        else ["This week", "Hold for next week", "Later this month", "Needs confirmation"]
    )
    buckets = [plan.this_week, plan.next_week, plan.this_month, plan.needs_confirmation]
    return [
        {"heading": heading, "entries": [_item_view(i, offers, language) for i in items]}
        for heading, items in zip(labels, buckets, strict=True)
    ]


def render(
    plan: PlanResult,
    offers: list[ValidatedOffer],
    output_format: str = "html",
    *,
    language: str = "zh-CN",
    title: str = "本周省钱吃法",
) -> str:
    by_id = {offer.offer_id: offer for offer in offers}

    if output_format == "json":
        return json.dumps(
            {
                "plan": json.loads(plan.model_dump_json()),
                "offers": [json.loads(o.model_dump_json()) for o in offers],
                "coverage_summary": coverage_sentence(plan.coverage),
                "disclaimer": (
                    "Compiled from email content. Not a merchant confirmation that an "
                    "offer is redeemable. Check the terms before you go."
                ),
            },
            ensure_ascii=False,
            indent=2,
        )

    context = {
        "title": title,
        "as_of": plan.as_of.strftime("%Y-%m-%d %H:%M"),
        "tz": plan.timezone,
        "coverage": plan.coverage,
        "coverage_line": coverage_sentence(plan.coverage),
        "sections": _sections(plan, by_id, language),
    }

    if output_format == "markdown":
        # Autoescape is HTML-specific; Markdown output is plain text by design.
        return Environment(autoescape=False).from_string(_MARKDOWN).render(**context)
    if output_format == "html":
        return _env.from_string(_HTML).render(**context)
    raise ValueError(f"unsupported format: {output_format}")


def render_to_file(
    plan: PlanResult,
    offers: list[ValidatedOffer],
    path: str,
    output_format: str | None = None,
    *,
    language: str = "zh-CN",
) -> str:
    if output_format is None:
        output_format = (
            "markdown" if path.endswith(".md") else "json" if path.endswith(".json") else "html"
        )
    content = render(plan, offers, output_format, language=language)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def timestamped_name(prefix: str, extension: str, now: datetime) -> str:
    return f"{prefix}-{now.strftime('%Y%m%d-%H%M')}.{extension}"
