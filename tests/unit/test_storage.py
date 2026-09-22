"""Storage.

The invariant under test: a re-sync rewrites model facts and never touches the
user's own decisions.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mealdeals.offers.validate import validate
from mealdeals.schemas import Coverage, OfferUserState, RunRecord, UserStatus
from mealdeals.storage.database import open_store, repository_scope

from ..conftest import make_draft, make_email

BODY = (
    "Take $4 off any lunch bowl when you spend $12 or more.\n"
    "Offer ends September 30, 2026. Pickup only."
)


@pytest.fixture
def repo(tmp_path):
    with repository_scope(open_store(tmp_path / "store")) as repository:
        yield repository


def an_offer(clock, preferences, message_id="m1"):
    draft = make_draft(ends=dt.date(2026, 9, 30), quote="$4 off", message_id=message_id)
    return validate(draft, make_email(BODY, source_id=message_id), clock, preferences)


class TestMessages:
    def test_reinserting_the_same_message_is_idempotent(self, repo):
        email = make_email(BODY, source_id="m1")
        first, changed_first = repo.upsert_message(email)
        second, changed_second = repo.upsert_message(email)
        assert first.id == second.id
        assert changed_first is True
        assert changed_second is False

    def test_changed_body_is_detected_and_requeued(self, repo):
        repo.upsert_message(make_email(BODY, source_id="m1"))
        repo.mark_message_status(repo.get_message("default", "m1").id, "done")
        row, changed = repo.upsert_message(make_email(BODY + " Extra terms.", source_id="m1"))
        assert changed
        assert row.processing_status == "pending"

    def test_same_id_under_a_different_account_is_a_different_row(self, repo):
        repo.upsert_message(make_email(BODY, source_id="m1", account_alias="a"))
        repo.upsert_message(make_email(BODY, source_id="m1", account_alias="b"))
        assert repo.get_message("a", "m1").id != repo.get_message("b", "m1").id


class TestUserStateIsolation:
    def test_sync_does_not_reset_user_state(self, clock, preferences, repo):
        """The regression that would silently un-use a used coupon."""
        offer = an_offer(clock, preferences)
        row, _ = repo.upsert_message(make_email(BODY, source_id="m1"))
        repo.upsert_offer(offer, {"m1": row.id})

        repo.set_user_state(
            OfferUserState(offer_id=offer.offer_id, status=UserStatus.USED, used_at=dt.datetime.now())
        )

        # A later scan re-derives the same offer facts.
        repo.upsert_offer(offer, {"m1": row.id})

        assert repo.get_user_state(offer.offer_id).status is UserStatus.USED

    def test_a_reminder_email_does_not_clear_used_state(self, clock, preferences, repo):
        offer = an_offer(clock, preferences, "m1")
        reminder = an_offer(clock, preferences, "m2")
        assert offer.offer_id == reminder.offer_id

        first, _ = repo.upsert_message(make_email(BODY, source_id="m1"))
        repo.upsert_offer(offer, {"m1": first.id})
        repo.set_user_state(OfferUserState(offer_id=offer.offer_id, status=UserStatus.USED))

        second, _ = repo.upsert_message(make_email(BODY, source_id="m2"))
        repo.upsert_offer(reminder, {"m2": second.id})

        assert repo.get_user_state(offer.offer_id).status is UserStatus.USED

    def test_dismissed_offers_are_hidden_by_default(self, clock, preferences, repo):
        offer = an_offer(clock, preferences)
        row, _ = repo.upsert_message(make_email(BODY, source_id="m1"))
        repo.upsert_offer(offer, {"m1": row.id})
        repo.set_user_state(
            OfferUserState(offer_id=offer.offer_id, status=UserStatus.DISMISSED)
        )
        assert repo.list_offers() == []
        assert len(repo.list_offers(include_dismissed=True)) == 1

    def test_optimistic_concurrency_rejects_a_stale_write(self, clock, preferences, repo):
        offer = an_offer(clock, preferences)
        state = OfferUserState(offer_id=offer.offer_id, status=UserStatus.SAVED)
        repo.set_user_state(state)
        repo.set_user_state(state)  # revision is now 2
        with pytest.raises(ValueError):
            repo.set_user_state(state, expected_revision=1)


class TestOfferVersioning:
    def test_changed_facts_bump_the_version(self, clock, preferences, repo):
        offer = an_offer(clock, preferences)
        row, _ = repo.upsert_message(make_email(BODY, source_id="m1"))
        repo.upsert_offer(offer, {"m1": row.id})
        updated = offer.model_copy(update={"title": "New title"})
        stored = repo.upsert_offer(updated, {"m1": row.id})
        assert stored.version == 2

    def test_identical_facts_do_not_bump_the_version(self, clock, preferences, repo):
        offer = an_offer(clock, preferences)
        row, _ = repo.upsert_message(make_email(BODY, source_id="m1"))
        repo.upsert_offer(offer, {"m1": row.id})
        stored = repo.upsert_offer(offer, {"m1": row.id})
        assert stored.version == 1

    def test_sources_accumulate_without_duplicates(self, clock, preferences, repo):
        offer = an_offer(clock, preferences)
        row, _ = repo.upsert_message(make_email(BODY, source_id="m1"))
        repo.upsert_offer(offer, {"m1": row.id})
        stored = repo.upsert_offer(offer, {"m1": row.id})
        assert len(stored.sources) == 1


class TestCostAccounting:
    def test_missing_usage_marks_the_cost_unknown_not_zero(self, repo):
        from mealdeals.schemas import ProviderMeta, Usage

        repo.start_run(
            RunRecord(
                run_id="r1", scope="s", mode="m", started_at=dt.datetime.now(dt.UTC)
            )
        )
        repo.record_model_call(
            "r1", "extract", ProviderMeta(provider="p", model="m", usage=Usage(cost_known=False))
        )
        total, known = repo.run_cost("r1")
        assert total == 0.0
        assert known is False

    def test_cached_calls_do_not_add_cost(self, repo):
        from mealdeals.schemas import ProviderMeta, Usage

        repo.start_run(
            RunRecord(
                run_id="r1", scope="s", mode="m", started_at=dt.datetime.now(dt.UTC)
            )
        )
        repo.record_model_call(
            "r1",
            "extract",
            ProviderMeta(
                provider="p",
                model="m",
                cached=True,
                usage=Usage(estimated_cost_usd=5.0, cost_known=True),
            ),
        )
        total, known = repo.run_cost("r1")
        assert total == 0.0
        assert known is True


class TestCoverage:
    def test_partial_run_is_reported_as_partial(self):
        assert Coverage(search_exhaustive=False).is_partial
        assert Coverage(search_exhaustive=True, fetch_failures=1).is_partial
        assert not Coverage(search_exhaustive=True).is_partial


class TestThreadSafety:
    """The store must survive being used from another thread.

    This is not academic: the MCP server dispatches tools off the event loop
    into a worker thread, and FastAPI runs sync endpoints the same way.
    """

    def test_store_is_visible_from_another_thread(self, clock):
        import concurrent.futures

        from mealdeals.service import MealDealsService

        service = MealDealsService.offline(clock=clock)
        service.sync_promotions(mode="llm-only")
        expected = len(service.list_food_offers())
        assert expected > 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(lambda: len(service.list_food_offers())).result() == expected
            assert pool.submit(service.status).result()["offers_stored"] == expected
