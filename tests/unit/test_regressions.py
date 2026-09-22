"""Regressions for defects found by review.

Each test here failed before its fix. They are grouped by the promise they
protect rather than by module, because that is what would be lost if one broke:
every one of these is a case where the application said something true-looking
that was not true.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from weekly_deals.config import Settings
from weekly_deals.mail.normalize import from_rfc822
from weekly_deals.offers import temporal
from weekly_deals.offers.eligibility import resolve
from weekly_deals.offers.validate import stable_offer_id
from weekly_deals.planning import costs
from weekly_deals.planning.explanations import ReasonCode
from weekly_deals.planning.planner import Planner
from weekly_deals.schemas import (
    Benefit,
    BenefitKind,
    Channel,
    Coverage,
    DateConfidence,
    DateKind,
    Eligibility,
    Money,
    OfferDraft,
    OfferUserState,
    Preferences,
    TemporalPoint,
    TemporalRules,
    TimeStatus,
    TriState,
    UserStatus,
)

from ..conftest import make_draft, make_email

USD = lambda minor: Money(minor=minor, currency="USD")  # noqa: E731


def _ends(day: dt.date) -> TemporalPoint:
    return TemporalPoint(
        raw_expression=f"Offer ends {day.strftime('%B %d, %Y')}",
        kind=DateKind.DATE_ONLY,
        date=day,
        confidence_state=DateConfidence.EXPLICIT_DATE_MISSING_TIME,
    )


def _draft(benefit: Benefit, eligibility: Eligibility | None = None) -> OfferDraft:
    return OfferDraft(
        merchant="Harbor Roasters",
        benefit=benefit,
        temporal=TemporalRules(ends=_ends(dt.date(2026, 9, 30))),
        eligibility=eligibility or Eligibility(),
    )


class TestOfferIdentityDoesNotCollide:
    """Two different promotions must never share a card -- or a 'used' flag."""

    def test_two_different_free_items_stay_separate(self):
        croissant = _draft(Benefit(kind=BenefitKind.FREE_ITEM, free_item_description="croissant"))
        cookie = _draft(Benefit(kind=BenefitKind.FREE_ITEM, free_item_description="cookie"))
        assert stable_offer_id(croissant) != stable_offer_id(cookie)

    def test_new_customer_variant_stays_separate(self):
        general = _draft(Benefit(kind=BenefitKind.AMOUNT_OFF, amount_off=USD(500)))
        restricted = _draft(
            Benefit(kind=BenefitKind.AMOUNT_OFF, amount_off=USD(500)),
            Eligibility(new_customer_only=TriState.KNOWN_YES),
        )
        assert stable_offer_id(general) != stable_offer_id(restricted)

    def test_same_number_different_currency_stays_separate(self):
        dollars = _draft(Benefit(kind=BenefitKind.AMOUNT_OFF, amount_off=USD(400)))
        euros = _draft(
            Benefit(kind=BenefitKind.AMOUNT_OFF, amount_off=Money(minor=400, currency="EUR"))
        )
        assert stable_offer_id(dollars) != stable_offer_id(euros)

    def test_different_caps_stay_separate(self):
        small = _draft(Benefit(kind=BenefitKind.PERCENT_OFF, percent_off=20, cap=USD(500)))
        large = _draft(Benefit(kind=BenefitKind.PERCENT_OFF, percent_off=20, cap=USD(5000)))
        assert stable_offer_id(small) != stable_offer_id(large)

    def test_a_reminder_for_the_same_campaign_still_merges(self):
        """The whole point of a stable id: repeat sightings must converge."""
        first = _draft(Benefit(kind=BenefitKind.AMOUNT_OFF, amount_off=USD(500)))
        again = _draft(Benefit(kind=BenefitKind.AMOUNT_OFF, amount_off=USD(500)))
        assert stable_offer_id(first) == stable_offer_id(again)


class TestBodyReconciliation:
    """Terms live in the footer, and the footer is usually only in the HTML."""

    def test_html_fine_print_survives_a_long_plain_part(self):
        raw = b"""From: "Cafe" <promo@cafe.example>
Subject: Free pastry
Date: Mon, 14 Sep 2026 10:00:00 -0500
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="IN"

--IN
Content-Type: text/plain; charset=utf-8

Free pastry with any drink!

View this email in your browser: https://cafe.example/view/abc123
You are receiving this because you subscribed to Cafe news.
Cafe, 100 Example Street, Example City, TX 77840, United States.
To stop receiving these emails, unsubscribe: https://cafe.example/u/abc
--IN
Content-Type: text/html; charset=utf-8

<html><body><h1>Free pastry with any drink!</h1>
<div style="font-size:8px">Valid through October 5, 2026. Members only.
Minimum spend $8. Claim by September 25, 2026 in the app.</div>
</body></html>
--IN--
"""
        email = from_rfc822(raw, source_id="m1")
        for term in ["October 5, 2026", "Claim by September 25", "Minimum spend $8", "Members only"]:
            assert term.lower() in email.normalized_text.lower(), f"lost: {term}"

    def test_identical_alternatives_are_not_duplicated(self):
        raw = b"""From: a@b.example
Subject: Same both ways
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="IN"

--IN
Content-Type: text/plain; charset=utf-8

Take $5 off your lunch. Offer ends September 30, 2026.
--IN
Content-Type: text/html; charset=utf-8

<html><body><p>Take $5 off your lunch. Offer ends September 30, 2026.</p></body></html>
--IN--
"""
        email = from_rfc822(raw, source_id="m2")
        assert email.normalized_text.count("$5 off") == 1


class TestCoverageTellsTheTruth:
    def test_a_capped_scan_is_not_exhaustive(self, service):
        result = service.sync_promotions(mode="llm-only", max_messages=3)
        assert result.coverage.messages_matched == 3
        assert result.coverage.search_exhaustive is False
        assert result.coverage.is_partial is True
        assert result.status == "partial"

    def test_an_uncapped_scan_is_exhaustive(self, service):
        result = service.sync_promotions(mode="llm-only")
        assert result.coverage.search_exhaustive is True
        assert result.status == "completed"

    def test_a_cached_rerun_still_counts_its_extractions(self, clock, tmp_path):
        from weekly_deals.service import WeeklyDealsService

        svc = WeeklyDealsService.offline(clock=clock, store_path=str(tmp_path / "t.db"))
        first = svc.sync_promotions(mode="llm-only")
        second = svc.sync_promotions(mode="llm-only")

        assert second.coverage.extraction_cached == first.coverage.extraction_success
        # "0 extracted" beside a full list of offers reads as a collapsed stage.
        assert second.coverage.extraction_success == first.coverage.extraction_success
        assert len(second.offers) == len(first.offers)


class TestBudgetIsEnforceable:
    def test_a_budget_without_prices_is_refused_up_front(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WEEKLY_DEALS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_MODEL", "some-model")
        settings = Settings.build()
        settings.app.runtime.per_run_budget_usd = 1.00
        assert settings.budget_enforceable() is False
        assert any("per_run_budget_usd" in p for p in settings.preflight())

    def test_prices_make_it_enforceable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WEEKLY_DEALS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_MODEL", "some-model")
        settings = Settings.build()
        settings.app.runtime.llm_price_input_usd_per_mtok = 0.15
        settings.app.runtime.llm_price_output_usd_per_mtok = 0.60
        assert settings.budget_enforceable() is True
        assert not any("per_run_budget_usd" in p for p in settings.preflight())

    def test_no_cap_configured_needs_no_prices(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WEEKLY_DEALS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        settings = Settings.build()
        settings.app.runtime.per_run_budget_usd = 0.0
        assert settings.budget_enforceable() is True


class TestCloudConsentCoversEveryRecipient:
    def test_jev_alone_still_requires_consent(self, monkeypatch, tmp_path):
        """JEV is sent the subject and body; the LLM being 'mock' is irrelevant."""
        monkeypatch.setenv("WEEKLY_DEALS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MAIL_PROVIDER", "gmail_api")
        monkeypatch.setenv("GMAIL_CLIENT_SECRET_PATH", str(tmp_path / "cs.json"))
        monkeypatch.setenv("TYPESAFE_API_KEY", "sk-jev")
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        settings = Settings.build()
        settings.app.classification.mode = "observe"
        settings.app.privacy.cloud_processing_consent = False

        assert any("JEV" in r for r in settings.cloud_recipients())
        assert any("cloud_processing_consent" in p for p in settings.preflight())

    def test_offline_sends_nothing_anywhere(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WEEKLY_DEALS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TYPESAFE_API_KEY", "sk-jev")
        settings = Settings.build(offline=True)
        assert settings.cloud_recipients() == []


class TestDerivedStateIsRecomputed:
    def test_an_expired_offer_does_not_read_as_live(self, clock, preferences):
        """Stored state is only as fresh as the scan that wrote it."""
        draft = make_draft(ends=dt.date(2026, 9, 20))
        email = make_email(
            "Take $4 off any lunch bowl. Offer ends September 20, 2026.",
        )
        from weekly_deals.offers.validate import validate

        offer = validate(draft, email, clock, preferences)
        assert offer.time_status is TimeStatus.WITHIN_STATED_WINDOW

        from weekly_deals.clock import FrozenClock

        later = FrozenClock(dt.datetime(2026, 12, 25, 9, 0), "America/Chicago")
        refreshed = temporal.refresh(offer, later)
        assert refreshed.time_status is TimeStatus.EXPIRED
        assert refreshed.actionable is False

    def test_service_refreshes_on_read(self, tmp_path):
        from weekly_deals.clock import FrozenClock
        from weekly_deals.service import WeeklyDealsService

        db = str(tmp_path / "t.db")
        scan_clock = FrozenClock(dt.datetime(2026, 9, 16, 9, 0), "America/Chicago")
        WeeklyDealsService.offline(clock=scan_clock, store_path=db).sync_promotions(mode="llm-only")

        later = FrozenClock(dt.datetime(2026, 12, 25, 9, 0), "America/Chicago")
        offers = WeeklyDealsService.offline(clock=later, store_path=db).list_food_offers()
        stale = [
            o
            for o in offers
            if o.temporal.ends.date
            and o.temporal.ends.date < dt.date(2026, 12, 25)
            and o.time_status is not TimeStatus.EXPIRED
        ]
        assert stale == [], f"{len(stale)} expired offers still reported as live"


class TestUserStateSurvives:
    def test_a_note_only_patch_keeps_the_used_flag(self, clock, tmp_path):
        from fastapi.testclient import TestClient

        from weekly_deals.service import WeeklyDealsService
        from weekly_deals.web.app import create_app

        svc = WeeklyDealsService.offline(clock=clock, store_path=str(tmp_path / "t.db"))
        svc.sync_promotions(mode="llm-only")
        offer_id = svc.list_food_offers()[0].offer_id
        svc.set_user_state(offer_id, status=UserStatus.USED)

        client = TestClient(create_app(svc), base_url="http://127.0.0.1:8765")
        token = client.get("/api/status").json()["csrf_token"]
        response = client.patch(
            f"/api/offers/{offer_id}/user-state",
            json={"note": "checked the terms"},
            headers={"x-weekly-deals-csrf": token},
        )
        assert response.status_code == 200
        with svc.repository() as repo:
            state = repo.get_user_state(offer_id)
        assert state.status is UserStatus.USED
        assert state.note == "checked the terms"

    def test_patching_an_unknown_offer_is_rejected(self, clock, tmp_path):
        from fastapi.testclient import TestClient

        from weekly_deals.service import WeeklyDealsService
        from weekly_deals.web.app import create_app

        svc = WeeklyDealsService.offline(clock=clock, store_path=str(tmp_path / "t.db"))
        client = TestClient(create_app(svc), base_url="http://127.0.0.1:8765")
        token = client.get("/api/status").json()["csrf_token"]
        response = client.patch(
            "/api/offers/does-not-exist/user-state",
            json={"status": "used"},
            headers={"x-weekly-deals-csrf": token},
        )
        assert response.status_code == 404


class TestEligibilityOverridesApply:
    def test_confirming_a_membership_unblocks_the_offer(self, clock):
        preferences = Preferences(currency="USD")
        draft = make_draft(
            ends=dt.date(2026, 9, 30),
            eligibility=Eligibility(
                membership_required=TriState.KNOWN_YES, membership_name="rewards"
            ),
        )
        email = make_email("Take $4 off any lunch bowl. Offer ends September 30, 2026.")
        from weekly_deals.offers.validate import validate

        offer = validate(draft, email, clock, preferences)
        planner = Planner(clock, preferences)

        without = planner.build([offer], {})
        assert [i.offer_id for i in without.needs_confirmation] == [offer.offer_id]

        state = OfferUserState(
            offer_id=offer.offer_id,
            eligibility_overrides={"membership_required": TriState.KNOWN_NO},
        )
        with_override = planner.build([offer], {offer.offer_id: state})
        assert [i.offer_id for i in with_override.this_week] == [offer.offer_id]


class TestChannelBlock:
    def test_a_stated_forbidden_channel_blocks_even_beside_unknown(self):
        preferences = Preferences(currency="USD", allowed_channels=[Channel.PICKUP])
        status, _ = resolve(
            Eligibility(channels=[Channel.DELIVERY, Channel.UNKNOWN]), preferences
        )
        assert str(status) == "ineligible"

    def test_an_entirely_unstated_channel_is_only_a_caveat(self):
        preferences = Preferences(currency="USD", allowed_channels=[Channel.PICKUP])
        status, notes = resolve(Eligibility(channels=[Channel.UNKNOWN]), preferences)
        assert str(status) == "confirmed"
        assert any("not stated" in n for n in notes)


class TestMoneyNeverFlatters:
    def test_an_unstated_cap_is_declared_unknown(self):
        estimate = costs.estimate(
            Benefit(kind=BenefitKind.PERCENT_OFF, percent_off=50),
            costs.BasketInput(subtotal=USD(10000)),
        )
        assert "maximum discount" in estimate.unknown_components

    def test_a_stated_absence_of_cap_is_not_unknown(self):
        estimate = costs.estimate(
            Benefit(kind=BenefitKind.PERCENT_OFF, percent_off=50, cap_stated_absent=True),
            costs.BasketInput(subtotal=USD(10000)),
        )
        assert "maximum discount" not in estimate.unknown_components

    def test_costing_more_is_reported_as_costing_more(self):
        estimate = costs.estimate(
            Benefit(kind=BenefitKind.AMOUNT_OFF, amount_off=USD(500)),
            costs.BasketInput(
                subtotal=USD(10000),
                baseline_alternative=USD(6000),
                known_tax=USD(0),
                known_fees=USD(0),
            ),
        )
        assert estimate.exceeds_baseline is True
        # Not "$0.00 saved", which is what the unsigned subtraction used to give.
        assert estimate.relative_savings is None
        assert "MORE" in (estimate.note or "")

    def test_a_fixed_price_above_the_basket_is_what_you_pay(self):
        estimate = costs.estimate(
            Benefit(kind=BenefitKind.FIXED_PRICE, fixed_price=USD(2000)),
            costs.BasketInput(subtotal=USD(1500), known_tax=USD(0), known_fees=USD(0)),
        )
        assert estimate.out_of_pocket == USD(2000)

    def test_a_cross_currency_minimum_is_unknown_not_zero(self):
        estimate = costs.estimate(
            Benefit(
                kind=BenefitKind.AMOUNT_OFF,
                amount_off=USD(500),
                minimum_spend=Money(minor=1000, currency="EUR"),
            ),
            costs.BasketInput(subtotal=USD(3000)),
        )
        assert estimate.computable is False

    def test_bogo_respects_its_own_minimum_spend(self):
        estimate = costs.estimate(
            Benefit(kind=BenefitKind.BOGO, minimum_spend=USD(5000)),
            costs.BasketInput(
                item_prices=[USD(1200), USD(1200)],
                party_size=2,
                known_tax=USD(0),
                known_fees=USD(0),
            ),
        )
        assert estimate.discount == USD(0)


class TestWeeklyBudget:
    def test_the_weekly_spend_limit_is_applied(self, clock):
        preferences = Preferences(
            currency="USD", per_meal_budget_minor=3000, weekly_dining_budget_minor=3500
        )
        from weekly_deals.offers.validate import validate

        offers = []
        for index, merchant in enumerate(("Alpha Grill", "Beta Grill")):
            draft = make_draft(
                merchant=merchant,
                minimum_minor=2500,
                ends=dt.date(2026, 9, 30),
                message_id=f"m{index}",
            )
            offers.append(
                validate(
                    draft,
                    make_email(
                        "Take $4 off any lunch bowl. Offer ends September 30, 2026.",
                        source_id=f"m{index}",
                    ),
                    clock,
                    preferences,
                )
            )

        result = Planner(clock, preferences).build(offers, {})
        # $25 + $25 is over a $35 weekly budget, so only one may be committed.
        assert len(result.this_week) == 1
        deferred = result.this_week + result.next_week + result.this_month
        assert ReasonCode.BUDGET_EXCEEDED in {
            code for item in deferred for code in item.reason_codes
        }


class TestPlanningNeedsNoProviders:
    def test_building_a_plan_never_constructs_a_mail_source_or_extractor(
        self, clock, tmp_path, monkeypatch
    ):
        """`report` must work from the database alone.

        Going through the pipeline meant loading Gmail credentials -- which can
        refresh a token over the network, or open the OAuth browser flow -- just
        to render a page of stored offers.
        """
        import weekly_deals.service as service_module
        from weekly_deals.service import WeeklyDealsService

        svc = WeeklyDealsService.offline(clock=clock, store_path=str(tmp_path / "t.db"))
        svc.sync_promotions(mode="llm-only")

        def explode(*_args, **_kwargs):
            raise AssertionError("a provider was constructed while building a plan")

        monkeypatch.setattr(service_module, "build_mail_source", explode)
        monkeypatch.setattr(service_module, "build_extractor", explode)
        monkeypatch.setattr(service_module, "build_classifier", explode)

        plan = svc.build_meal_plan()
        assert isinstance(plan.coverage, Coverage)


class TestUnknownDeadline:
    def test_no_stated_deadline_produces_no_phantom_unresolved_field(self, clock, preferences):
        from weekly_deals.offers.validate import validate

        draft = make_draft(ends=None)
        offer = validate(draft, make_email("Take $4 off any lunch bowl."), clock, preferences)
        assert offer.time_status is TimeStatus.UNKNOWN
        # The offer's deadline is unknown, but "temporal.ends has no quote" is
        # not a *separate* open question -- there is no date to quote.
        assert "temporal.ends" not in offer.unresolved_fields


@pytest.mark.parametrize("output_format", ["html", "markdown", "json"])
def test_reports_state_an_incomplete_scan(service, output_format):
    from weekly_deals.reporting.render import coverage_sentence, render

    result = service.sync_promotions(mode="llm-only", max_messages=2)
    plan = service.build_meal_plan()
    text = render(plan, service.list_food_offers(), output_format)
    assert "未穷尽" in coverage_sentence(result.coverage)
    assert text


class TestGmailErrorClassification:
    """403 means two different things and they need opposite handling."""

    def _error(self, status: int, message: str) -> Exception:
        class _Resp:
            def __init__(self, code: int) -> None:
                self.status = code

        exc = Exception(message)
        exc.resp = _Resp(status)  # type: ignore[attr-defined]
        return exc

    def test_quota_403_is_retryable_not_an_auth_failure(self):
        from weekly_deals.mail.gmail_api import GmailApiSource

        translated = GmailApiSource._translate(
            self._error(403, "Quota exceeded: userRateLimitExceeded")
        )
        assert translated.retryable is True
        assert translated.code == "http_403_quota"

    def test_permission_403_still_asks_for_reauthorisation(self):
        from weekly_deals.mail.base import AuthRequired
        from weekly_deals.mail.gmail_api import GmailApiSource

        translated = GmailApiSource._translate(
            self._error(403, "Request had insufficient authentication scopes")
        )
        assert isinstance(translated, AuthRequired)
        assert translated.retryable is False


class TestPaidWorkSurvivesACrash:
    """A scan costs money per message; a late failure must not undo it all."""

    def test_extractions_before_a_crash_are_kept_and_reused(self, clock, tmp_path, monkeypatch):
        import weekly_deals.service as service_module
        from weekly_deals.models.base import OfferExtractor
        from weekly_deals.models.mock import MockExtractor
        from weekly_deals.service import WeeklyDealsService

        calls = {"n": 0}

        class DiesPartway(OfferExtractor):
            model_id = "mock-extractor"

            def __init__(self) -> None:
                self.inner = MockExtractor()

            def extract(self, email):
                calls["n"] += 1
                if calls["n"] == 5:
                    raise RuntimeError("provider client blew up")
                return self.inner.extract(email)

        db = str(tmp_path / "t.db")
        monkeypatch.setattr(service_module, "build_extractor", lambda _s: DiesPartway())
        crashed = WeeklyDealsService.offline(clock=clock, store_path=db)
        with pytest.raises(RuntimeError):
            crashed.sync_promotions(mode="llm-only")

        monkeypatch.setattr(service_module, "build_extractor", lambda _s: MockExtractor())
        recovered = WeeklyDealsService.offline(clock=clock, store_path=db)
        with recovered.repository() as repo:
            stored = sum(len(record.extractions) for record in repo.store.iter_messages())
        assert stored == 4, "work completed before the crash was rolled back"

        # And the retry reuses them instead of paying for them a second time.
        result = recovered.sync_promotions(mode="llm-only")
        assert result.coverage.extraction_cached == 4


class TestAttachmentsAreNotSilentlyIgnored:
    """A PDF flyer is where merchants put the offer, not decoration."""

    def _with_pdf(self) -> bytes:
        return b"""From: "Deli" <promo@deli.example>
Subject: This week's specials
Date: Mon, 14 Sep 2026 10:00:00 -0500
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="OUT"

--OUT
Content-Type: text/plain; charset=utf-8

See the attached flyer for this week's specials.
--OUT
Content-Type: application/pdf; name="specials.pdf"
Content-Transfer-Encoding: base64
Content-Disposition: attachment; filename="specials.pdf"

JVBERi0xLjQK
--OUT--
"""

    def test_a_pdf_flyer_is_flagged_rather_than_reported_as_fully_parsed(self):
        email = from_rfc822(self._with_pdf(), source_id="m1")
        assert email.has_unparsed_visuals is True
        assert str(email.parse_status) == "needs_visual"
        assert any(hint.mime_type == "application/pdf" for hint in email.parts)
        assert any("does not read documents" in note for note in email.parse_notes)

    def test_an_offer_from_such_an_email_cannot_enter_a_plan(self, clock, preferences):
        from weekly_deals.offers.validate import validate

        email = from_rfc822(self._with_pdf(), source_id="m1")
        offer = validate(make_draft(ends=dt.date(2026, 9, 30)), email, clock, preferences)
        assert str(offer.parse_status) == "needs_visual"
        result = Planner(clock, preferences).build([offer], {})
        assert result.this_week == []
        assert [item.offer_id for item in result.needs_confirmation] == [offer.offer_id]


class TestImageOnlyDetection:
    def test_a_single_image_with_no_readable_terms_is_flagged(self):
        from weekly_deals.mail.normalize import html_to_text

        _, _, image_heavy = html_to_text(
            "<html><body><img src='x.png' alt='Free scoop'></body></html>"
        )
        assert image_heavy is True, "alt text is a hint that an offer exists, not its terms"

    def test_a_short_but_readable_promo_is_not_flagged(self):
        from weekly_deals.mail.normalize import html_to_text

        _, _, image_heavy = html_to_text(
            "<html><body><img src='logo.png' alt='Cafe'>"
            "<p>$5 off your lunch. Ends September 30, 2026.</p>"
            "<img src='pixel.gif' width='1' height='1'></body></html>"
        )
        assert image_heavy is False, "a logo and a tracking pixel do not hide the terms"


class TestRetention:
    def test_stored_text_expires_but_live_evidence_is_kept(self, clock, tmp_path):
        from weekly_deals.service import WeeklyDealsService

        svc = WeeklyDealsService.offline(clock=clock, store_path=str(tmp_path / "store"))
        svc.settings.app.privacy.payload_retention_days = 30
        svc.sync_promotions(mode="llm-only")

        with svc.repository() as repo:
            live_sources = {
                source_id
                for offer in repo.list_offers(include_dismissed=True)
                if str(offer.time_status) != "expired"
                for source_id in offer.source_message_ids
            }
            records = list(repo.store.iter_messages())

        kept = [r for r in records if r.source_id in live_sources]
        pruned = [r for r in records if r.payload_pruned_at is not None]
        assert kept, "the corpus should contain at least one live offer"
        assert all(r.normalized_text for r in kept), "evidence for a live offer was dropped"
        assert pruned, "nothing was pruned despite a 30-day retention window"
        # A pruned record keeps its hash, so a re-scan still skips it for free.
        assert all(r.body_hash for r in pruned)

    def test_retention_zero_disables_pruning(self, clock, tmp_path):
        from weekly_deals.service import WeeklyDealsService

        svc = WeeklyDealsService.offline(clock=clock, store_path=str(tmp_path / "store"))
        svc.settings.app.privacy.payload_retention_days = 0
        svc.sync_promotions(mode="llm-only")
        with svc.repository() as repo:
            assert all(r.payload_pruned_at is None for r in repo.store.iter_messages())


class TestSuspectedDuplicatesPersist:
    def test_pairs_survive_the_run_and_can_be_settled(self, clock, tmp_path):
        """Same merchant, same $4 off, different end dates: not safely mergeable."""
        from weekly_deals.offers.validate import validate
        from weekly_deals.storage.database import open_store, repository_scope

        offers = []
        for index, end in enumerate((dt.date(2026, 9, 30), dt.date(2026, 10, 15))):
            body = (
                f"Take $4 off any lunch bowl. Offer ends {end.strftime('%B %d, %Y')}."
            )
            offers.append(
                validate(
                    make_draft(ends=end, message_id=f"m{index}"),
                    make_email(body, source_id=f"m{index}"),
                    clock,
                    Preferences(currency="USD"),
                )
            )
        assert offers[0].offer_id != offers[1].offer_id

        from weekly_deals.offers.deduplicate import deduplicate

        dedup = deduplicate(offers)
        assert dedup.suspected_duplicates, "a near-match should be reported, not merged"

        store = open_store(tmp_path / "store")
        with repository_scope(store) as repo:
            repo.record_suspected_duplicates(dedup.suspected_duplicates)
            for offer in offers:
                repo.upsert_offer(offer, {sid: sid for sid in offer.source_message_ids})

        # A separate reader sees them: they outlived the run that found them.
        with repository_scope(open_store(tmp_path / "store")) as repo:
            assert repo.suspected_duplicates()
            left, right = dedup.suspected_duplicates[0]
            repo.dismiss_duplicate(left, right)

        with repository_scope(open_store(tmp_path / "store")) as repo:
            assert (left, right) not in repo.suspected_duplicates()

    def test_a_scan_persists_the_pairs_it_finds(self, clock, tmp_path):
        """The pipeline must write them, not just count them in its output."""
        from weekly_deals.config import Settings
        from weekly_deals.service import WeeklyDealsService
        from weekly_deals.storage.database import open_store

        maildir = tmp_path / "mail"
        maildir.mkdir()
        for index, ends in enumerate(("September 30, 2026", "October 15, 2026")):
            (maildir / f"near{index}.eml").write_text(
                "From: Copper Kettle <promo@copper.example>\n"
                f"Subject: $4 off your lunch\n"
                "Date: Mon, 14 Sep 2026 10:00:00 -0500\n"
                "Content-Type: text/plain; charset=utf-8\n\n"
                f"Take $4 off any lunch bowl. Offer ends {ends}. Pickup only.\n",
                encoding="utf-8",
            )

        settings = Settings.build(
            offline=False,
            overrides={"mail.provider": "eml_dir", "mail.eml_dir": str(maildir)},
        )
        settings.app.classification.mode = "off"
        svc = WeeklyDealsService.__new__(WeeklyDealsService)
        svc.settings = settings
        svc.clock = clock
        svc._store = open_store(tmp_path / "store")

        result = svc.sync_promotions(mode="llm-only")
        found = {tuple(sorted(pair)) for pair in result.dedup.suspected_duplicates}
        stored = {tuple(sorted((p["left"], p["right"]))) for p in svc.suspected_duplicates()}
        assert found, "two $4-off offers with different end dates should be a near-match"
        assert found <= stored, "the scan reported near-matches it did not store"


class TestOptimisticConcurrencyIsUsable:
    def test_a_read_returns_the_revision_a_write_needs(self, clock, tmp_path):
        from fastapi.testclient import TestClient

        from weekly_deals.service import WeeklyDealsService
        from weekly_deals.web.app import create_app

        svc = WeeklyDealsService.offline(clock=clock, store_path=str(tmp_path / "store"))
        svc.sync_promotions(mode="llm-only")
        offer_id = svc.list_food_offers()[0].offer_id

        client = TestClient(create_app(svc), base_url="http://127.0.0.1:8765")
        token = client.get("/api/status").json()["csrf_token"]

        detail = client.get(f"/api/offers/{offer_id}").json()
        revision = detail["user_state"]["revision"]

        headers = {"x-weekly-deals-csrf": token}
        first = client.patch(
            f"/api/offers/{offer_id}/user-state",
            json={"status": "saved", "expected_revision": revision},
            headers=headers,
        )
        assert first.status_code == 200

        # The second tab still holds the old revision and must be refused.
        stale = client.patch(
            f"/api/offers/{offer_id}/user-state",
            json={"status": "dismissed", "expected_revision": revision},
            headers=headers,
        )
        assert stale.status_code == 409


class TestStoreDurability:
    def test_a_torn_write_cannot_be_observed(self, tmp_path):
        """Readers see the old file or the new one, never a partial one."""
        from weekly_deals.storage.store import JsonStore

        store = JsonStore(tmp_path / "store")
        store.initialise()
        store.offers()["a"] = {"offer_id": "a", "payload": {}}
        store._touch("offers")
        store.commit()

        path = store.root / "offers.json"
        assert json.loads(path.read_text())["a"]["offer_id"] == "a"
        # No temporary files left behind to be mistaken for data.
        assert not list(store.root.glob("*.tmp"))

    def test_unreadable_json_is_an_error_not_an_empty_store(self, tmp_path):
        from weekly_deals.storage.store import JsonStore

        store = JsonStore(tmp_path / "store")
        store.initialise()
        (store.root / "offers.json").write_text("{ this is not json")
        with pytest.raises(RuntimeError, match="not readable JSON"):
            store.offers()

    def test_a_newer_schema_is_refused_rather_than_downgraded(self, tmp_path):
        from weekly_deals.storage.store import SCHEMA_VERSION, JsonStore

        root = tmp_path / "store"
        root.mkdir(parents=True)
        (root / "meta.json").write_text(json.dumps({"schema_version": SCHEMA_VERSION + 1}))
        with pytest.raises(RuntimeError, match="newer Weekly Deals"):
            JsonStore(root).initialise()

    def test_an_older_store_missing_a_field_still_loads(self, tmp_path):
        """What replaces migrations: Pydantic defaults fill the gap."""
        from weekly_deals.storage.records import MessageRecord

        record = MessageRecord.model_validate({"source_id": "m1", "body_hash": "abc"})
        assert record.processing_status == "pending"
        assert record.extractions == []
        assert record.account_alias == "default"


class TestConcurrentUseDoesNotBlock:
    """The web UI must stay usable while a scan is running.

    Under SQLite the scan held a write transaction for its whole duration, so a
    user marking an offer used in the browser got "database is locked". Writers
    now take the lock for one file write at a time.
    """

    def test_a_user_write_succeeds_during_a_scan(self, clock, tmp_path):
        import threading
        import time

        import weekly_deals.service as service_module
        from weekly_deals.models.base import OfferExtractor
        from weekly_deals.models.mock import MockExtractor
        from weekly_deals.service import WeeklyDealsService

        store_path = str(tmp_path / "store")
        seed = WeeklyDealsService.offline(clock=clock, store_path=store_path)
        seed.sync_promotions(mode="llm-only")
        offer_id = seed.list_food_offers()[0].offer_id

        class Slow(OfferExtractor):
            model_id = "mock-extractor"

            def __init__(self) -> None:
                self.inner = MockExtractor()

            def extract(self, email):
                time.sleep(0.05)
                return self.inner.extract(email)

        original = service_module.build_extractor
        service_module.build_extractor = lambda _s: Slow()
        try:
            scanner = WeeklyDealsService.offline(clock=clock, store_path=store_path)
            failures: list[Exception] = []

            def run_scan() -> None:
                try:
                    scanner.sync_promotions(mode="llm-only")
                except Exception as exc:  # pragma: no cover - the bug being guarded
                    failures.append(exc)

            thread = threading.Thread(target=run_scan)
            thread.start()
            time.sleep(0.1)  # let the scan get going

            writer = WeeklyDealsService.offline(clock=clock, store_path=store_path)
            for index in range(4):
                writer.set_user_state(offer_id, status=UserStatus.USED, note=f"write {index}")
                time.sleep(0.03)
            thread.join(timeout=30)
        finally:
            service_module.build_extractor = original

        assert not failures, f"the scan failed while the user was writing: {failures}"
        reader = WeeklyDealsService.offline(clock=clock, store_path=store_path)
        with reader.repository() as repo:
            state = repo.get_user_state(offer_id)
        assert state.status is UserStatus.USED
        assert state.note == "write 3", "a concurrent scan overwrote the user's own state"
        assert len(reader.list_food_offers()) == len(seed.list_food_offers())


class TestHostIngest:
    """The path a host with its own mailbox access uses (skills/weekly-deals).

    The host is another extractor, not an authority: its output goes through the
    same validator as a paid model's.
    """

    BODY = (
        "From: Copper Kettle <rewards@copper.example>\n"
        "Subject: $6 off your next lunch\n"
        "Date: Sat, 19 Sep 2026 09:00:00 -0500\n"
        "Content-Type: text/plain; charset=utf-8\n\n"
        "Take $6 off your next lunch. Spend $18 or more.\n"
        "Offer ends September 30, 2026.\n"
    )

    def _service(self, clock, tmp_path):
        from weekly_deals.config import Settings
        from weekly_deals.service import WeeklyDealsService
        from weekly_deals.storage.database import open_store

        maildir = tmp_path / "mail"
        maildir.mkdir()
        (maildir / "a.eml").write_text(self.BODY, encoding="utf-8")

        settings = Settings.build(
            offline=True,
            overrides={"mail.provider": "eml_dir", "mail.eml_dir": str(maildir)},
        )
        settings.app.classification.mode = "off"
        service = WeeklyDealsService.__new__(WeeklyDealsService)
        service.settings = settings
        service.clock = clock
        service._store = open_store(tmp_path / "store")
        return service

    def _draft(self, quote: str, *, merchant: str = "Copper Kettle") -> dict:
        return {
            "merchant": merchant,
            "benefit": {
                "kind": "amount_off",
                "amount_off": {"minor": 600, "currency": "USD"},
                "description": "$6 off",
            },
            "temporal": {
                "ends": {
                    "raw_expression": "Offer ends September 30, 2026",
                    "kind": "date_only",
                    "date": "2026-09-30",
                    "confidence_state": "explicit_date_missing_time",
                }
            },
            "evidence": [
                {"field_path": "benefit.amount_off", "message_id": "a.eml", "quote": quote},
                {
                    "field_path": "temporal.ends",
                    "message_id": "a.eml",
                    "quote": "Offer ends September 30, 2026",
                },
            ],
        }

    def test_host_ingest_mode_normalizes_but_does_not_extract(self, clock, tmp_path):
        svc = self._service(clock, tmp_path)
        result = svc.sync_promotions(mode="host-ingest")
        assert result.coverage.messages_fetched == 1
        assert result.coverage.extraction_attempts == 0
        assert result.offers == []

        pending = svc.pending_messages()
        assert [m["message_id"] for m in pending] == ["a.eml"]
        # The text handed out is the normalizer's, which is what evidence is
        # checked against -- not the host's own copy of the email.
        assert "Offer ends September 30, 2026" in pending[0]["normalized_text"]

    def test_a_verifiable_draft_is_accepted(self, clock, tmp_path):
        svc = self._service(clock, tmp_path)
        svc.sync_promotions(mode="host-ingest")
        summary = svc.ingest_offers({"a.eml": [self._draft("Take $6 off your next lunch")]})

        assert summary["rejected"] == {}
        assert summary["offers_stored"] == 1
        assert summary["unverified_evidence"] == []
        stored = svc.list_food_offers()
        assert [o.evidence_verified for o in stored] == [True]

    def test_an_invented_quote_is_refused_and_cannot_reach_a_plan(self, clock, tmp_path):
        svc = self._service(clock, tmp_path)
        svc.sync_promotions(mode="host-ingest")
        summary = svc.ingest_offers(
            {"a.eml": [self._draft("Take $60 off and get a free dessert every Tuesday")]}
        )

        assert "benefit.amount_off" in summary["unverified_evidence"]
        offer = svc.list_food_offers()[0]
        assert offer.evidence_verified is False
        plan = svc.build_meal_plan()
        assert plan.this_week == []
        assert [i.offer_id for i in plan.needs_confirmation] == [offer.offer_id]

    def test_coverage_from_this_path_is_never_exhaustive(self, clock, tmp_path):
        svc = self._service(clock, tmp_path)
        svc.sync_promotions(mode="host-ingest")
        svc.ingest_offers({"a.eml": [self._draft("Take $6 off your next lunch")]})
        with svc.repository() as repo:
            run = repo.latest_run()
        assert run.coverage.search_exhaustive is False
        assert run.status == "partial"

    def test_an_unknown_message_id_is_reported_not_dropped(self, clock, tmp_path):
        svc = self._service(clock, tmp_path)
        svc.sync_promotions(mode="host-ingest")
        summary = svc.ingest_offers({"never-seen": [self._draft("anything")]})
        assert "never-seen" in summary["rejected"]
        assert summary["offers_stored"] == 0

    def test_a_malformed_draft_is_reported_not_swallowed(self, clock, tmp_path):
        svc = self._service(clock, tmp_path)
        svc.sync_promotions(mode="host-ingest")
        summary = svc.ingest_offers({"a.eml": [{"merchant": "X"}]})  # no benefit
        assert "a.eml" in summary["rejected"]
        assert "schema" in summary["rejected"]["a.eml"]

    def test_ingest_refuses_when_the_text_was_pruned(self, clock, tmp_path):
        """Without the text there is nothing to check quotes against."""
        svc = self._service(clock, tmp_path)
        svc.sync_promotions(mode="host-ingest")
        with svc.repository() as repo:
            record = repo.store.find_by_source_id("a.eml")[0]
            record.normalized_text = ""
            repo.store.write_message(record)

        summary = svc.ingest_offers({"a.eml": [self._draft("Take $6 off your next lunch")]})
        assert "re-scan" in summary["rejected"]["a.eml"]


def test_the_skill_documents_commands_that_exist():
    """SKILL.md tells a host what to run; those commands must be real."""
    import pathlib
    import re

    from weekly_deals.cli import app

    text = pathlib.Path("skills/weekly-deals/SKILL.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^weekly-deals (\w[\w-]*)", text, re.MULTILINE))
    registered = {command.name or command.callback.__name__ for command in app.registered_commands}
    registered |= {group.name for group in app.registered_groups}
    missing = documented - registered
    assert not missing, f"SKILL.md references commands that do not exist: {sorted(missing)}"


class TestClassifierFiltersBeforeTheHostSeesAnything:
    """JEV's whole job: keep most of the mailbox out of the extractor.

    In host-ingest mode the extractor is the *host's* context, so a message the
    classifier rejects is a message the host never has to read. That is the
    saving, and it only exists if classification runs on this path.
    """

    def _service(self, clock, tmp_path, mode, record=None):
        from weekly_deals.service import WeeklyDealsService

        svc = WeeklyDealsService.offline(clock=clock, store_path=str(tmp_path / "store"))
        svc.settings.app.classification.mode = mode
        if record:
            svc.settings.app.classification.gate_evaluation_record = record
        return svc

    def test_gate_mode_withholds_confident_negatives_from_the_host(self, clock, tmp_path):
        svc = self._service(clock, tmp_path, "gate", record="evals/2026-09-20.json")
        result = svc.sync_promotions(mode="host-ingest")
        assert result.coverage.classified > 0, "the classifier did not run on this path"
        assert result.stages.rejected, "nothing was filtered, so the host reads everything"
        handed_over = {m["message_id"] for m in svc.pending_messages()}
        assert handed_over.isdisjoint(set(result.stages.rejected))

    def test_observe_mode_discards_nothing(self, clock, tmp_path):
        svc = self._service(clock, tmp_path, "observe")
        result = svc.sync_promotions(mode="host-ingest")
        assert result.coverage.classified > 0
        assert result.stages.rejected == []
        assert len(svc.pending_messages()) == result.coverage.messages_fetched

    def test_a_rerun_does_not_pay_to_classify_unchanged_mail(self, clock, tmp_path):
        """The weekly case: the same 90-day window, mostly unchanged."""
        import weekly_deals.service as service_module
        from weekly_deals.models.mock import MockClassifier

        calls = {"n": 0}

        class Counting(MockClassifier):
            def classify(self, email):
                calls["n"] += 1
                return super().classify(email)

        original = service_module.build_classifier
        service_module.build_classifier = lambda _s: Counting()
        try:
            self._service(clock, tmp_path, "observe").sync_promotions(mode="host-ingest")
            first = calls["n"]
            self._service(clock, tmp_path, "observe").sync_promotions(mode="host-ingest")
            second = calls["n"] - first
        finally:
            service_module.build_classifier = original

        assert first > 0
        assert second == 0, f"re-classified {second} unchanged message(s) and paid for them again"

    def test_a_contradiction_is_never_discarded(self):
        """Low probability plus a confident food category is uncertainty, not a no."""
        from weekly_deals.models.jev import route_for
        from weekly_deals.schemas import (
            ClassificationResult,
            FoodCategory,
            NormalizedEmail,
            ProviderMeta,
            Route,
        )

        email = NormalizedEmail(source_id="m", normalized_text="...")

        def verdict(category):
            return ClassificationResult(
                message_id="m",
                body_hash="h",
                contains_food_offer=0.02,
                food_category=category,
                meta=ProviderMeta(provider="jev", model="jev-1.13.0"),
            )

        gate = {"mode": "gate", "reject_below": 0.05, "accept_above": 0.70}
        assert route_for(verdict(FoodCategory.OTHER_OR_UNCLEAR), email, **gate) is (
            Route.PROVISIONAL_REJECT
        )
        assert route_for(verdict(FoodCategory.RESTAURANT), email, **gate) is not (
            Route.PROVISIONAL_REJECT
        )

    def test_the_mock_classifier_does_not_contradict_itself(self):
        """Otherwise the offline demo cannot show gating working at all."""
        from weekly_deals.models.mock import MockClassifier
        from weekly_deals.schemas import FoodCategory, NormalizedEmail

        verdict = MockClassifier().classify(
            NormalizedEmail(
                source_id="m",
                # An announcement with no concrete benefit -- the shape of the
                # corpus's deliberate negative.
                normalized_text=(
                    "Our autumn menu is here, featuring roasted squash soup. "
                    "Come try it at any of our four locations. "
                    "We look forward to seeing you."
                ),
            )
        )
        assert verdict.contains_food_offer < 0.05
        assert verdict.food_category is FoodCategory.OTHER_OR_UNCLEAR
