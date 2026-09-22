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
