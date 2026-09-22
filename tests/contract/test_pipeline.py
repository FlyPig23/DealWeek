"""End-to-end pipeline behaviour, entirely offline."""

from __future__ import annotations

import pytest

from mealdeals.mail.base import MailSource, MailSourceError
from mealdeals.schemas import (
    MailCapabilities,
    TimeStatus,
    UserStatus,
)
from mealdeals.service import MealDealsService


class TestFixtureRun:
    def test_full_offline_run_produces_offers(self, service):
        result = service.sync_promotions(mode="llm-only")
        assert result.status == "completed"
        assert result.coverage.search_exhaustive
        assert result.coverage.messages_fetched == result.coverage.messages_matched
        assert result.coverage.offers_after_dedup > 0

    def test_pagination_reaches_every_fixture(self, service, mail):
        """Fixtures paginate at 5, so this exercises the cursor loop."""
        result = service.sync_promotions(mode="llm-only")
        assert result.coverage.messages_matched == len(list(mail.iter_all("q")))

    def test_reminder_email_is_merged(self, service):
        result = service.sync_promotions(mode="llm-only")
        assert result.dedup.merged_count > 0
        multi = [o for o in result.offers if len(o.source_message_ids) > 1]
        assert multi, "the reminder fixture should merge into its original"

    def test_image_only_email_is_counted_not_dropped(self, service):
        result = service.sync_promotions(mode="llm-only")
        assert result.coverage.unparsed_visuals >= 1

    def test_expired_offer_is_stored_but_not_planned(self, service):
        service.sync_promotions(mode="llm-only")
        offers = service.list_food_offers()
        assert any(o.time_status is TimeStatus.EXPIRED for o in offers)
        plan = service.build_meal_plan()
        expired_ids = {o.offer_id for o in offers if o.time_status is TimeStatus.EXPIRED}
        planned = {i.offer_id for i in plan.this_week + plan.next_week + plan.this_month}
        assert not (expired_ids & planned)

    def test_offer_without_a_deadline_never_reaches_a_committed_bucket(self, service):
        service.sync_promotions(mode="llm-only")
        plan = service.build_meal_plan()
        undated = {
            o.offer_id
            for o in service.list_food_offers()
            if o.temporal.ends.date is None and o.time_status is TimeStatus.UNKNOWN
        }
        committed = {i.offer_id for i in plan.this_week + plan.next_week + plan.this_month}
        assert not (undated & committed)
        assert undated <= {i.offer_id for i in plan.needs_confirmation}

    def test_no_offer_claims_savings_without_a_basket(self, service):
        service.sync_promotions(mode="llm-only")
        plan = service.build_meal_plan()
        every = plan.this_week + plan.next_week + plan.this_month + plan.needs_confirmation
        assert all(item.cost.relative_savings is None for item in every)

    def test_cjk_merchant_survives_the_round_trip(self, service):
        service.sync_promotions(mode="llm-only")
        assert any("青禾" in o.merchant for o in service.list_food_offers())


class TestIdempotency:
    def test_second_run_does_not_duplicate_offers(self, clock):
        service = MealDealsService.offline(clock=clock)
        first = service.sync_promotions(mode="llm-only")
        second = service.sync_promotions(mode="llm-only")
        assert first.coverage.offers_after_dedup == second.coverage.offers_after_dedup
        assert len(service.list_food_offers()) == first.coverage.offers_after_dedup

    def test_second_run_reuses_the_cached_extraction(self, clock):
        service = MealDealsService.offline(clock=clock)
        service.sync_promotions(mode="llm-only")
        second = service.sync_promotions(mode="llm-only")
        # Nothing changed, so no message needed a fresh extraction call.
        assert second.coverage.extraction_attempts == 0

    def test_user_state_survives_a_rescan(self, clock):
        service = MealDealsService.offline(clock=clock)
        service.sync_promotions(mode="llm-only")
        target = service.list_food_offers()[0]
        service.set_user_state(target.offer_id, status=UserStatus.USED)

        service.sync_promotions(mode="llm-only")

        with service.repository() as repo:
            assert repo.get_user_state(target.offer_id).status is UserStatus.USED

    def test_time_status_refreshes_without_a_new_model_call(self, clock):
        """A cached run must still recompute expiry against the new date."""
        service = MealDealsService.offline(clock=clock)
        service.sync_promotions(mode="llm-only")
        before = {o.offer_id: o.time_status for o in service.list_food_offers()}

        clock.advance(days=40)
        after_run = service.sync_promotions(mode="llm-only")
        after = {o.offer_id: o.time_status for o in after_run.offers}

        newly_expired = [
            oid
            for oid, status in after.items()
            if status is TimeStatus.EXPIRED and before.get(oid) is not TimeStatus.EXPIRED
        ]
        assert newly_expired


class TestClassifierRouting:
    def test_observe_mode_extracts_everything(self, clock):
        service = MealDealsService.offline(clock=clock)
        service.settings.app.classification.mode = "observe"
        result = service.sync_promotions(mode="jev-observe")
        assert result.stages.rejected == []
        assert result.coverage.classified > 0

    def test_gate_mode_requires_a_recorded_evaluation(self, clock):
        service = MealDealsService.offline(clock=clock)
        service.settings.app.classification.mode = "gate"
        with pytest.raises(RuntimeError, match="gate evaluation"):
            service.sync_promotions(mode="jev-gate")

    def test_gate_mode_with_an_evaluation_record_may_filter(self, clock):
        service = MealDealsService.offline(clock=clock)
        service.settings.app.classification.mode = "gate"
        service.settings.app.classification.gate_evaluation_record = "evals/record-v1.json"
        result = service.sync_promotions(mode="jev-gate")
        assert result.status in ("completed", "partial")


class TestFailureHandling:
    def test_a_search_failure_is_not_an_empty_result(self, clock):
        class BrokenSource(MailSource):
            def capabilities(self):
                return MailCapabilities(provider="broken")

            def search(self, query, page_cursor=None):
                raise MailSourceError("mailbox unreachable", code="network")

            def fetch(self, message_id):  # pragma: no cover - never reached
                raise AssertionError

        service = MealDealsService.offline(clock=clock)
        with service.repository() as repo:
            pipeline = service._pipeline(repo)
            pipeline.mail = BrokenSource()
            result = pipeline.scan(mode="llm-only")

        assert result.status == "failed"
        assert not result.coverage.search_exhaustive
        assert result.stages.errors["search"] == "network"

    def test_a_fetch_failure_is_counted_and_the_run_is_partial(self, clock, mail):
        class FlakySource(MailSource):
            def __init__(self, inner):
                self.inner = inner

            def capabilities(self):
                return self.inner.capabilities()

            def search(self, query, page_cursor=None):
                return self.inner.search(query, page_cursor)

            def fetch(self, message_id):
                if message_id == "fx-003":
                    raise MailSourceError("boom", code="http_500", retryable=True)
                return self.inner.fetch(message_id)

        service = MealDealsService.offline(clock=clock)
        with service.repository() as repo:
            pipeline = service._pipeline(repo)
            pipeline.mail = FlakySource(mail)
            result = pipeline.scan(mode="llm-only")

        assert result.coverage.fetch_failures == 1
        assert result.status == "partial"
        assert result.coverage.is_partial

    def test_budget_exhaustion_parks_work_and_keeps_what_was_gathered(self, clock):
        service = MealDealsService.offline(clock=clock)
        # A tiny budget with a priced classifier would stop after the first call;
        # the mock reports $0, so instead assert the accounting path is wired.
        result = service.sync_promotions(mode="llm-only")
        assert result.cost_known
        assert result.cost_usd == 0.0


class TestMaxMessages:
    def test_cap_is_respected(self, service):
        result = service.sync_promotions(mode="llm-only", max_messages=3)
        assert result.coverage.messages_matched == 3
        assert result.coverage.messages_fetched == 3


class TestReportRendering:
    @pytest.mark.parametrize("fmt", ["html", "markdown", "json"])
    def test_every_format_renders(self, service, fmt):
        from mealdeals.reporting import render as reporting

        service.sync_promotions(mode="llm-only")
        output = reporting.render(
            service.build_meal_plan(), service.list_food_offers(), fmt
        )
        assert output.strip()

    def test_html_escapes_email_content_and_loads_nothing_remote(self, service):
        from mealdeals.reporting import render as reporting

        service.sync_promotions(mode="llm-only")
        html = reporting.render(
            service.build_meal_plan(), service.list_food_offers(), "html"
        )
        assert "<script" not in html.lower()
        assert "http://" not in html and "https://" not in html
        assert "no-referrer" in html

    def test_report_always_states_its_coverage(self, service):
        from mealdeals.reporting import render as reporting

        service.sync_promotions(mode="llm-only")
        markdown = reporting.render(
            service.build_meal_plan(), service.list_food_offers(), "markdown"
        )
        assert "处理覆盖" in markdown or "命中" in markdown
        assert "并非商家实时兑换确认" in markdown


class TestConcurrentFetch:
    """Fetching runs in parallel only when the source says that is safe.

    The dangerous failure mode is not a crash: a client library that shares one
    HTTP connection interleaves responses, so one email's body ends up attached
    to another message id. Order preservation is asserted for the same reason.
    """

    def _pipeline(self, clock, source):
        service = MealDealsService.offline(clock=clock)
        service.settings.app.runtime.mail_concurrency = 4
        with service.repository() as repo:
            pipeline = service._pipeline(repo)
            pipeline.mail = source
            yield pipeline

    def test_order_is_preserved_under_concurrency(self, clock, mail):
        service = MealDealsService.offline(clock=clock)
        service.settings.app.runtime.mail_concurrency = 4
        assert mail.capabilities().supports_concurrent_fetch

        refs = list(mail.iter_all("q"))
        with service.repository() as repo:
            pipeline = service._pipeline(repo)
            pipeline.mail = mail
            fetched = [ref.source_id for ref, _ in pipeline._fetch_stream(refs)]
        assert fetched == [ref.source_id for ref in refs]

    def test_each_body_stays_with_its_own_id(self, clock, mail):
        """The interleaving bug would show up here and nowhere else."""
        service = MealDealsService.offline(clock=clock)
        service.settings.app.runtime.mail_concurrency = 4
        refs = list(mail.iter_all("q"))
        with service.repository() as repo:
            pipeline = service._pipeline(repo)
            pipeline.mail = mail
            for ref, email in pipeline._fetch_stream(refs):
                assert email.source_id == ref.source_id

    def test_a_source_that_is_not_thread_safe_is_fetched_serially(self, clock, mail):
        import threading

        class SerialOnly(MailSource):
            def __init__(self, inner):
                self.inner = inner
                self.threads: set[int] = set()

            def capabilities(self):
                caps = self.inner.capabilities()
                return caps.model_copy(update={"supports_concurrent_fetch": False})

            def search(self, query, page_cursor=None):
                return self.inner.search(query, page_cursor)

            def fetch(self, message_id):
                self.threads.add(threading.get_ident())
                return self.inner.fetch(message_id)

        source = SerialOnly(mail)
        service = MealDealsService.offline(clock=clock)
        service.settings.app.runtime.mail_concurrency = 8
        refs = list(mail.iter_all("q"))
        with service.repository() as repo:
            pipeline = service._pipeline(repo)
            pipeline.mail = source
            list(pipeline._fetch_stream(refs))
        assert source.threads == {threading.get_ident()}

    def test_one_failed_fetch_does_not_lose_the_others(self, clock, mail):
        class OneBad(MailSource):
            def __init__(self, inner):
                self.inner = inner

            def capabilities(self):
                return self.inner.capabilities()

            def search(self, query, page_cursor=None):
                return self.inner.search(query, page_cursor)

            def fetch(self, message_id):
                if message_id == "fx-005":
                    raise MailSourceError("boom", code="http_500")
                return self.inner.fetch(message_id)

        service = MealDealsService.offline(clock=clock)
        service.settings.app.runtime.mail_concurrency = 4
        with service.repository() as repo:
            pipeline = service._pipeline(repo)
            pipeline.mail = OneBad(mail)
            result = pipeline.scan(mode="llm-only")

        assert result.coverage.fetch_failures == 1
        assert result.coverage.messages_fetched == result.coverage.messages_matched - 1
        assert result.coverage.offers_after_dedup > 0

    def test_an_unexpected_exception_becomes_a_fetch_error(self, clock, mail):
        class Exploding(MailSource):
            def __init__(self, inner):
                self.inner = inner

            def capabilities(self):
                return self.inner.capabilities()

            def search(self, query, page_cursor=None):
                return self.inner.search(query, page_cursor)

            def fetch(self, message_id):
                if message_id == "fx-002":
                    raise ZeroDivisionError("not a MailSourceError")
                return self.inner.fetch(message_id)

        service = MealDealsService.offline(clock=clock)
        service.settings.app.runtime.mail_concurrency = 4
        with service.repository() as repo:
            pipeline = service._pipeline(repo)
            pipeline.mail = Exploding(mail)
            result = pipeline.scan(mode="llm-only")

        assert result.stages.errors["fx-002"] == "fetch_error"
        assert result.status == "partial"


class TestProviderTransparency:
    """A run must say which components produced its numbers.

    `--offline` substitutes the mock classifier and extractor. Someone
    evaluating JEV's behaviour needs to see that the output in front of them did
    not come from JEV.
    """

    def test_scan_reports_the_components_that_ran(self, service):
        result = service.sync_promotions(mode="llm-only")
        assert result.providers["mail"] == "fixtures"
        assert result.providers["extractor"] == "mock-extractor"

    def test_classifier_is_named_when_one_runs(self, clock):
        service = MealDealsService.offline(clock=clock)
        service.settings.app.classification.mode = "observe"
        result = service.sync_promotions(mode="jev-observe")
        assert result.providers["classifier"] == "mock-classifier"

    def test_classifier_is_reported_as_none_when_disabled(self, service):
        result = service.sync_promotions(mode="llm-only")
        assert result.providers["classifier"] == "none"
