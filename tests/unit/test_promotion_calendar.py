from datetime import UTC, date, datetime

from weekly_deals.promotions.calendar import build_promotion_event
from weekly_deals.promotions.render import render_calendar
from weekly_deals.schemas import NormalizedEmail
from weekly_deals.service import WeeklyDealsService


def test_calendar_extracts_explicit_expiry_and_food_category() -> None:
    email = NormalizedEmail(
        source_id="msg-1",
        sender="Deals <offers@example.com>",
        subject="20% off bowls this week",
        normalized_text="Save 20% on bowls. Offer ends September 28, 2026."
    )

    event = build_promotion_event(
        email,
        now=datetime(2026, 9, 22, 12, tzinfo=UTC),
    )

    assert event.end_date == date(2026, 9, 28)
    assert event.category == "food"
    assert event.status == "ending_soon"
    assert event.message_id == "msg-1"


def test_calendar_keeps_unknown_dates_and_marks_visuals_for_review() -> None:
    email = NormalizedEmail(
        source_id="msg-2",
        sender="Store <offers@example.com>",
        subject="Member savings inside",
        normalized_text="See the image for details.",
        has_unparsed_visuals=True,
    )

    event = build_promotion_event(
        email,
        now=datetime(2026, 9, 22, 12, tzinfo=UTC),
    )

    assert event.end_date is None
    assert event.status == "unknown"
    assert event.needs_review is True


def test_offline_scan_indexes_all_messages_for_savings_calendar() -> None:
    service = WeeklyDealsService.offline()

    result = service.sync_promotions(mode="llm-only")

    assert result.coverage.promotions_indexed == result.coverage.messages_fetched
    assert len(service.list_promotions()) == result.coverage.messages_fetched


def test_calendar_html_uses_week_grid_and_category_colors() -> None:
    events = [
        build_promotion_event(
            NormalizedEmail(
                source_id="food-1",
                sender="Noodle Lantern <offers@example.com>",
                subject="20% off bowls",
                normalized_text="Save 20% off bowls. Offer ends September 24, 2026.",
            ),
            now=datetime(2026, 9, 22, 12, tzinfo=UTC),
        ),
        build_promotion_event(
            NormalizedEmail(
                source_id="retail-1",
                sender="Shop <offers@example.com>",
                subject="Shopping reward",
                normalized_text="$10 off supplies. See the image for details.",
                has_unparsed_visuals=True,
            ),
            now=datetime(2026, 9, 22, 12, tzinfo=UTC),
        ),
    ]

    html = render_calendar(events, now=datetime(2026, 9, 22, 12, tzinfo=UTC))

    assert "每周省钱日历" in html
    assert "calendar-grid" in html
    assert "category-food" in html
    assert "category-retail" in html
    assert "Noodle Lantern" in html
    assert "Shop" in html
    assert "http://" not in html and "https://" not in html


def test_saved_groups_keep_raw_messages_and_use_earliest_conflicting_deadline(service) -> None:
    from weekly_deals.schemas import PromotionEvent

    versions = {}
    with service.repository() as repo:
        for number, day in [(1, 23), (2, 24), (3, 24)]:
            message = NormalizedEmail(source_id=f"mail-{number}", normalized_text="Same sale")
            row, _ = repo.upsert_message(message)
            versions[message.source_id] = row.body_hash
            repo.upsert_promotion(PromotionEvent(
                promotion_id=f"promo-{number}", message_id=message.source_id,
                merchant="Example", title="Same campaign", category="retail",
                end_date=date(2026, 9, day),
            ))
        repo.store.put_promotion_dedup({
            "source_versions": versions,
            "groups": [{"representative_id": "promo-2", "member_ids": ["promo-2", "promo-1", "promo-3"]}],
        })
    grouped = service.list_promotions()
    assert len(grouped) == 1
    assert grouped[0].duplicate_count == 3
    assert set(grouped[0].source_message_ids) == {"mail-1", "mail-2", "mail-3"}
    assert grouped[0].deadline_conflict and grouped[0].needs_review
    assert grouped[0].end_date == date(2026, 9, 23)
    assert len(service.list_promotions(deduplicated=False)) == 3
    with service.repository() as repo:
        repo.upsert_message(NormalizedEmail(source_id="mail-2", normalized_text="Changed sale"))
    assert len(service.list_promotions()) == 3
