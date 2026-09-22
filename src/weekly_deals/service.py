"""Shared application core.

The CLI, the local web app and the MCP server all call this. None of them holds
business rules of its own, so there is exactly one definition of what "this
week's offers" means, and a fix lands in all three at once.

Provider selection also lives here, so the offline demo and a real run differ by
configuration rather than by code path.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC

from pydantic import ValidationError

from .clock import Clock, SystemClock
from .config import JEV_ENDPOINT, Settings
from .mail.base import MailSource
from .mail.fixtures import FixtureMailSource
from .models.base import OfferExtractor, PromotionClassifier
from .models.mock import MockClassifier, MockExtractor
from .offers import temporal
from .pipeline import PipelineService, ScanResult
from .promotions.calendar import refresh_promotion_event
from .schemas import (
    OfferUserState,
    PlanResult,
    Preferences,
    PromotionEvent,
    UserStatus,
    ValidatedOffer,
)
from .storage.database import open_store, repository_scope
from .storage.repository import Repository


def build_mail_source(settings: Settings, clock: Clock) -> MailSource:
    provider = settings.app.mail.provider

    # eml_dir is checked before the offline shortcut: reading local files IS an
    # offline run, and silently swapping the user's real test mail for the
    # synthetic corpus would make `--offline` quietly test the wrong thing.
    if provider == "eml_dir":
        from .mail.eml_files import EmlDirectorySource

        if not settings.app.mail.eml_dir:
            raise ValueError("mail.eml_dir is not set for the eml_dir provider")
        return EmlDirectorySource(
            settings.app.mail.eml_dir, account_alias=settings.app.mail.account_alias
        )

    if provider == "fixtures" or settings.offline:
        return FixtureMailSource(clock)
    if provider == "gmail_api":
        from .mail.gmail_api import GmailApiSource, build_credentials

        secret = settings.secrets.gmail_client_secret_path
        if not secret:
            raise ValueError("GMAIL_CLIENT_SECRET_PATH is not set")
        token_path = settings.data_dir / "gmail_token.json"
        return GmailApiSource(build_credentials(secret, token_path))
    if provider == "gmail_mcp":
        from .mail.gmail_mcp import GmailMcpSource

        return GmailMcpSource()
    raise ValueError(f"unknown mail provider: {provider}")


def build_extractor(settings: Settings) -> OfferExtractor:
    if settings.offline or settings.secrets.llm_provider == "mock":
        return MockExtractor()

    key = settings.secrets.llm_api_key
    model = settings.secrets.llm_model
    if key is None or not model:
        raise ValueError("LLM_API_KEY and LLM_MODEL are required for a non-mock provider")

    runtime = settings.app.runtime
    prompt = None  # adapters fall back to the packaged prompt file

    # Without prices a call's cost is unknown, and unknown spend cannot be held
    # against per_run_budget_usd. `Settings.preflight` refuses that combination;
    # here we simply pass through whatever the user configured.
    prices: dict[str, tuple[float, float]] | None = None
    if (
        runtime.llm_price_input_usd_per_mtok is not None
        and runtime.llm_price_output_usd_per_mtok is not None
    ):
        prices = {
            model: (
                runtime.llm_price_input_usd_per_mtok,
                runtime.llm_price_output_usd_per_mtok,
            )
        }

    if settings.secrets.llm_provider == "openai":
        from .models.openai_extractor import OpenAIExtractor

        return OpenAIExtractor(
            key.get_secret_value(),
            model,
            base_url=settings.secrets.llm_base_url,
            prompt=prompt,
            timeout=runtime.request_timeout_seconds,
            max_retries=runtime.max_retries,
            prices=prices,
        )

    from .models.compatible_extractor import CompatibleExtractor

    if not settings.secrets.llm_base_url:
        raise ValueError("LLM_BASE_URL is required for the compatible provider")
    return CompatibleExtractor(
        key.get_secret_value(),
        model,
        settings.secrets.llm_base_url,
        prompt=prompt,
        timeout=runtime.request_timeout_seconds,
        max_retries=runtime.max_retries,
        prices=prices,
    )


def build_classifier(settings: Settings) -> PromotionClassifier | None:
    if settings.app.classification.mode == "off":
        return None
    if settings.offline or not settings.secrets.has_jev():
        return MockClassifier()

    from .models.jev import JevClassifier, load_questions

    # load_questions defaults to the packaged file; no CWD guessing.
    questions = load_questions()["questions"]
    assert settings.secrets.typesafe_api_key is not None
    return JevClassifier(
        settings.secrets.typesafe_api_key.get_secret_value(),
        model=settings.secrets.jev_model,
        endpoint=settings.secrets.jev_endpoint,
        questions=questions,
        timeout=settings.app.runtime.request_timeout_seconds,
        max_retries=settings.app.runtime.max_retries,
    )


class WeeklyDealsService:
    """One entry point for every front end."""

    def __init__(self, settings: Settings, clock: Clock | None = None) -> None:
        self.settings = settings
        self.clock = clock or SystemClock(settings.app.report.timezone)
        self._store = open_store(settings.store_path)

    @classmethod
    def offline(
        cls,
        clock: Clock | None = None,
        store_path: str | None = None,
        preferences: Preferences | None = None,
    ) -> WeeklyDealsService:
        """Fully offline instance: fixtures, mock models, no network, no keys.

        Defaults to an in-memory database so a demo run leaves nothing behind
        and cannot collide with a real scan's data.
        """
        settings = Settings.build(offline=True)
        settings.app.mail.provider = "fixtures"
        settings.app.classification.mode = "off"
        if preferences is not None:
            settings.app.preferences = preferences

        service = cls.__new__(cls)
        service.settings = settings
        service.clock = clock or SystemClock(settings.app.report.timezone)
        service._store = open_store(store_path or ":memory:")
        return service

    @contextmanager
    def repository(self) -> Iterator[Repository]:
        with repository_scope(self._store) as repo:
            yield repo

    def _pipeline(self, repo: Repository) -> PipelineService:
        return PipelineService(
            settings=self.settings,
            clock=self.clock,
            repository=repo,
            mail_source=build_mail_source(self.settings, self.clock),
            extractor=build_extractor(self.settings),
            classifier=build_classifier(self.settings),
        )

    # -- capabilities exposed to every front end --------------------------

    def sync_promotions(
        self, *, mode: str | None = None, max_messages: int | None = None
    ) -> ScanResult:
        with self.repository() as repo:
            return self._pipeline(repo).scan(mode=mode, max_messages=max_messages)

    def list_food_offers(self, *, include_dismissed: bool = False) -> list[ValidatedOffer]:
        # Time status is re-derived on the way out. Stored offers are only as
        # fresh as the scan that wrote them, and serving a coupon that ran out
        # last month as `within_stated_window` / `actionable` is the one thing
        # every surface here is supposed to avoid.
        with self.repository() as repo:
            stored = repo.list_offers(include_dismissed=include_dismissed)
        return [temporal.refresh(offer, self.clock) for offer in stored]

    def deduplicate_promotions(self, *, max_comparisons: int = 5000) -> dict:
        """Judge repeated campaigns with JEV, without changing source messages."""
        from .models.jev import JevClassifier
        from .promotions.deduplicate import _identity, deduplicate_promotions

        if self.settings.offline or not self.settings.secrets.has_jev():
            raise ValueError("JEV deduplication needs a configured key; run `weekly-deals auth jev`.")
        secrets = self.settings.secrets
        if not (
            self.settings.app.privacy.cloud_processing_consent
            or (secrets.typesafe_email_processing_consent and secrets.jev_endpoint == JEV_ENDPOINT)
        ):
            raise ValueError("JEV email-processing consent is missing; run `weekly-deals auth jev`.")
        with self.repository() as repo:
            events = repo.list_promotions()
            messages = {record.source_id: record for record in repo.store.iter_messages()}
            previous = repo.store.promotion_dedup()
        runtime = self.settings.app.runtime
        assert secrets.typesafe_api_key is not None
        with JevClassifier(
            secrets.typesafe_api_key.get_secret_value(), model=secrets.jev_model,
            endpoint=secrets.jev_endpoint, timeout=runtime.request_timeout_seconds,
            max_retries=0,
        ) as classifier:
            result = deduplicate_promotions(
                events, messages, classifier, cache=previous.get("cache", []),
                budget_usd=runtime.per_run_budget_usd, max_comparisons=max_comparisons,
                workers=runtime.jev_concurrency,
                as_of=self.clock.now().date(),
            )
        result["source_versions"] = {key: record.body_hash for key, record in messages.items()}
        result["event_identities"] = {
            event.promotion_id: _identity(event, messages.get(event.message_id)) for event in events
        }
        result["created_at"] = self.clock.now().isoformat()
        with self.repository() as repo:
            repo.store.put_promotion_dedup(result)
        return result

    def list_promotions(self, *, deduplicated: bool = True) -> list[PromotionEvent]:
        """Return the grouped calendar, or every source message on request."""
        with self.repository() as repo:
            events = repo.list_promotions()
            saved = repo.store.promotion_dedup() if deduplicated else {}
            messages = {record.source_id: record for record in repo.store.iter_messages()} if saved else {}
        if saved:
            from .promotions.deduplicate import _identity
            from .promotions.grouping import group_promotions

            # A changed or newly fetched source stays visible until re-judged.
            versions = saved.get("source_versions", {})
            identities = saved.get("event_identities", {})
            valid = {
                event.promotion_id for event in events
                if event.message_id in messages
                and versions.get(event.message_id) == messages[event.message_id].body_hash
                and (not identities or identities.get(event.promotion_id)
                     == _identity(event, messages[event.message_id]))
            }
            groups = [{**group, "member_ids": [
                key for key in group.get("member_ids", []) if key in valid
            ]} for group in saved.get("groups", []) if group.get("representative_id") in valid]
            events = group_promotions(events, groups, accounts={
                key: record.account_alias for key, record in messages.items()
            })
        return sorted(
            (refresh_promotion_event(event, now=self.clock.now()) for event in events),
            key=lambda event: (
                event.end_date is None,
                event.end_date or dt.date.max,
                event.merchant.lower(),
            ),
        )

    def promotion_candidate_selection(self) -> tuple[set[str], dict]:
        """Select a display view from current judgments without changing source records."""
        threshold = self.settings.app.classification.accept_above
        eligible: set[str] = set()
        stats = {"total": 0, "candidates": 0, "below_threshold": 0,
                 "unresolved": 0, "threshold": threshold}
        with self.repository() as repo:
            for record in repo.store.iter_messages():
                stats["total"] += 1
                latest = next((call for call in reversed(record.classifications)
                               if call.body_hash == record.body_hash), None)
                probability = latest.payload.get("contains_promotion") if latest else None
                if (
                    latest is None or latest.status != "ok" or latest.error_code is not None
                    or latest.payload.get("error_code") is not None
                    or isinstance(probability, bool) or not isinstance(probability, (int, float))
                    or not 0 <= probability <= 1
                ):
                    stats["unresolved"] += 1
                elif probability >= threshold:
                    eligible.add(record.source_id)
                    stats["candidates"] += 1
                else:
                    stats["below_threshold"] += 1
        return eligible, stats

    def get_user_state(self, offer_id: str) -> OfferUserState:
        """Including ``revision``, which a caller needs to use the write guard."""
        with self.repository() as repo:
            return repo.get_user_state(offer_id)

    def suspected_duplicates(self) -> list[dict]:
        """Near-matches the deduplicator refused to merge, for the user to settle."""
        with self.repository() as repo:
            pairs = repo.suspected_duplicates()
            offers = {offer.offer_id: offer for offer in repo.list_offers(include_dismissed=True)}
        return [
            {
                "left": left,
                "right": right,
                "left_title": getattr(offers.get(left), "title", None),
                "right_title": getattr(offers.get(right), "title", None),
                "merchant": getattr(offers.get(left), "merchant", None),
            }
            for left, right in pairs
            if left in offers and right in offers
        ]

    def dismiss_duplicate(self, left: str, right: str) -> None:
        """Record that the user checked a suspected pair and they are different."""
        with self.repository() as repo:
            repo.dismiss_duplicate(left, right)

    def get_offer(self, offer_id: str) -> ValidatedOffer | None:
        with self.repository() as repo:
            found = repo.get_offer(offer_id)
        return temporal.refresh(found, self.clock) if found is not None else None

    def build_meal_plan(self) -> PlanResult:
        """Plan from stored offers only. Touches no provider and no network.

        Deliberately does *not* go through ``_pipeline``: building that
        constructs the mail source and the extractor, which for a real
        configuration means loading Gmail credentials -- refreshing an expired
        token over the network, or launching the OAuth browser flow -- and
        instantiating an LLM client, none of which a plan needs. Rendering a
        report from the local database must never be able to do that.
        """
        from .planning.planner import Planner
        from .schemas import Coverage

        with self.repository() as repo:
            stored = repo.list_offers()
            states = repo.all_user_states()
            latest = repo.latest_run()
            coverage = Coverage.model_validate(latest.coverage) if latest else Coverage()
            offers = [temporal.refresh(offer, self.clock) for offer in stored]
            result = Planner(self.clock, self.settings.app.preferences).build(
                offers, states, coverage
            )
            repo.save_plan(result, date_range="rolling")
        return result

    def set_user_state(
        self,
        offer_id: str,
        *,
        status: UserStatus | None = None,
        planned_date: str | None = None,
        note: str | None = None,
        eligibility_overrides: dict[str, str] | None = None,
        expected_revision: int | None = None,
    ) -> OfferUserState:
        from datetime import date as _date
        from datetime import datetime as _datetime

        from .schemas import TriState

        with self.repository() as repo:
            current = repo.get_user_state(offer_id)
            now = _datetime.now(UTC)
            update: dict = {}
            if status is not None:
                update["status"] = status
                if status is UserStatus.USED:
                    update["used_at"] = now
                elif status is UserStatus.DISMISSED:
                    update["dismissed_at"] = now
                elif status is UserStatus.SAVED:
                    update["saved"] = True
            if planned_date is not None:
                update["planned_date"] = _date.fromisoformat(planned_date)
                update.setdefault("status", UserStatus.PLANNED)
            if note is not None:
                update["note"] = note
            if eligibility_overrides is not None:
                # "Yes, I am a member" / "no, I am not a new customer". These
                # are the user's own corrections to model-derived eligibility,
                # merged rather than replaced so one answer does not drop another.
                merged = dict(current.eligibility_overrides)
                merged.update(
                    {field: TriState(value) for field, value in eligibility_overrides.items()}
                )
                update["eligibility_overrides"] = merged
            update["updated_at"] = now
            return repo.set_user_state(
                current.model_copy(update=update), expected_revision=expected_revision
            )


    # -- host-provided extraction -----------------------------------------
    #
    # For a host that can already read the user's mailbox through a Gmail,
    # Outlook or other mail connector. It reads and extracts, so the user needs
    # neither a separate mail OAuth client nor an extractor API key.
    #
    # What does NOT change is that the host's output is not trusted. It goes
    # through the same validator as any model's: every quote must be locatable
    # verbatim in the text stored here, unknown deadlines stay unknown, and
    # nothing unverified reaches a plan. The host is another extractor, not an
    # authority.

    def pending_messages(self, *, limit: int | None = None) -> list[dict]:
        """Stored messages awaiting extraction, with the text to extract from.

        The text handed back is the *normalized* body this application produced
        when it read the message -- MIME walked, HTML flattened, charsets
        decoded, footnotes kept. A host must extract against this and not
        against its own copy, because this is the text the validator will check
        its quotes against.
        """
        with self.repository() as repo:
            records = [
                record
                for record in repo.store.iter_messages()
                if record.processing_status in ("pending", "parked_provider_error")
            ]
        records.sort(key=lambda r: (r.sent_at is None, r.sent_at or dt.datetime.min))
        if limit is not None:
            records = records[: max(1, limit)]
        return [
            {
                "message_id": record.source_id,
                "subject": record.subject,
                "sender": record.sender,
                "sent_at": record.sent_at.isoformat() if record.sent_at else None,
                "received_at": record.received_at.isoformat() if record.received_at else None,
                "date_provenance": record.date_provenance,
                "body_complete": record.body_complete,
                "has_unparsed_visuals": record.has_unparsed_visuals,
                "truncated": record.truncated,
                "structured_markup": record.structured_markup,
                "normalized_text": record.normalized_text,
            }
            for record in records
        ]

    def ingest_offers(self, drafts_by_message: dict[str, list]) -> dict:
        """Validate, deduplicate and store offers a host extracted.

        Returns a summary that names what could not be accepted rather than
        quietly dropping it. Coverage from this path is never exhaustive: this
        application did not choose the search, so it cannot vouch for it.
        """
        from .offers import validate as validation
        from .offers.deduplicate import deduplicate
        from .schemas import Coverage, OfferDraft, RunRecord
        from .storage.records import as_normalized_email

        run_id = uuid.uuid4().hex[:16]
        started = dt.datetime.now(UTC)
        validated = []
        accepted: dict[str, str] = {}
        rejected: dict[str, str] = {}

        with self.repository() as repo:
            coverage = Coverage(
                query="host-provided messages",
                range_end=self.clock.now().date(),
                # The host chose what to hand over. "I was given 12 messages" is
                # not "there are 12 promotional emails", and the report has to
                # keep those apart.
                search_exhaustive=False,
                messages_matched=len(drafts_by_message),
            )
            repo.start_run(
                RunRecord(
                    run_id=run_id,
                    scope="host-provided messages",
                    mode="host-ingest",
                    started_at=started,
                    coverage=coverage,
                )
            )

            for message_id, raw_drafts in drafts_by_message.items():
                matches = repo.store.find_by_source_id(message_id)
                if not matches:
                    rejected[message_id] = "unknown message id; scan it in first"
                    continue
                if len(matches) > 1:
                    rejected[message_id] = (
                        "that message id exists under "
                        f"{len(matches)} accounts; ambiguous, not guessing"
                    )
                    continue
                record = matches[0]
                if not record.normalized_text:
                    rejected[message_id] = "stored text was pruned; re-scan the message"
                    continue

                email = as_normalized_email(record)
                coverage.messages_fetched += 1
                if email.has_unparsed_visuals:
                    coverage.unparsed_visuals += 1
                coverage.extraction_attempts += 1

                try:
                    drafts = [
                        item if isinstance(item, OfferDraft) else OfferDraft.model_validate(item)
                        for item in raw_drafts
                    ]
                except ValidationError as exc:
                    rejected[message_id] = f"draft did not match the schema: {exc.error_count()} error(s)"
                    coverage.extraction_failures += 1
                    repo.mark_message_status(record.id, "failed")
                    continue

                for draft in drafts:
                    validated.append(
                        validation.validate(
                            draft, email, self.clock, self.settings.app.preferences
                        )
                    )
                coverage.extraction_success += 1
                accepted[message_id] = f"{len(drafts)} draft(s)"
                repo.mark_message_status(record.id, "done")

            coverage.offers_validated = len(validated)
            dedup = deduplicate(validated)
            coverage.offers_after_dedup = len(dedup.offers)
            coverage.last_sync_at = dt.datetime.now(UTC)

            ids = {offer_id: offer_id for offer in dedup.offers for offer_id in offer.source_message_ids}
            for offer in dedup.offers:
                repo.upsert_offer(offer, ids)
            repo.record_suspected_duplicates(dedup.suspected_duplicates)
            repo.finish_run(
                run_id,
                status="partial",  # never exhaustive, by construction
                coverage=coverage,
                cost_usd=0.0,
                cost_known=True,
            )

        return {
            "run_id": run_id,
            "accepted": accepted,
            "rejected": rejected,
            "offers_stored": len(dedup.offers),
            "unverified_evidence": sorted(
                {
                    item.field_path
                    for offer in dedup.offers
                    for item in offer.evidence
                    if not item.verified
                }
            ),
            "needs_confirmation": sum(1 for o in dedup.offers if not o.actionable),
            "coverage_note": (
                "These messages were chosen by the host, not by a scan this application "
                "ran, so the coverage is not exhaustive and the report says so."
            ),
        }

    def status(self) -> dict:
        """Configuration and last-run status. Never returns a key or token."""
        with self.repository() as repo:
            latest = repo.latest_run()
            offers = repo.list_offers()
            promotions = repo.list_promotions()
        problems = self.settings.preflight()
        return {
            "offline": self.settings.offline,
            "mail_provider": self.settings.app.mail.provider,
            "classification_mode": self.settings.app.classification.mode,
            "llm_provider": self.settings.secrets.llm_provider,
            "jev_configured": self.settings.secrets.has_jev(),
            "llm_configured": self.settings.secrets.has_llm(),
            "cloud_processing_consent": self.settings.app.privacy.cloud_processing_consent,
            "jev_email_processing_consent": self.settings.secrets.typesafe_email_processing_consent,
            "jev_setup_command": "weekly-deals auth jev",
            "jev_verification_note": "Key presence is not a live check; use doctor --check-apis.",
            "timezone": self.settings.app.report.timezone,
            "offers_stored": len(offers),
            "promotions_indexed": len(promotions),
            "last_run": (
                {
                    "run_id": latest.run_id,
                    "status": latest.status,
                    "mode": latest.mode,
                    "started_at": latest.started_at.isoformat() if latest.started_at else None,
                    "finished_at": latest.finished_at.isoformat() if latest.finished_at else None,
                    "estimated_cost_usd": latest.estimated_cost_usd,
                    "cost_known": latest.cost_known,
                }
                if latest
                else None
            ),
            "blocking_problems": problems,
        }
