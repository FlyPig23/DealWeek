"""Pipeline orchestration.

Run order (which is not the development order):

    fetch -> normalize -> store -> classify -> route -> extract -> validate
          -> deduplicate -> store -> plan -> render

Plain Python controls the sequence. No model decides what happens next, which is
why the failure modes are enumerable and the whole thing is testable offline.

Three invariants hold throughout:

* An unchanged message never costs money twice.
* A provider failure is recorded as a failure and the message is parked; it is
  never folded into the results as "nothing found here".
* The budget is checked before each paid call, and the run stops cleanly when it
  is exhausted, keeping everything already gathered.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .clock import Clock
from .config import Settings
from .mail.base import MailSource, MailSourceError
from .models.base import OfferExtractor, PromotionClassifier
from .models.jev import route_for
from .offers import validate as validation
from .offers.deduplicate import DedupResult, deduplicate
from .planning.planner import Planner
from .promotions.calendar import build_promotion_event
from .schemas import (
    SCHEMA_VERSION,
    ClassificationResult,
    Coverage,
    ExtractionStatus,
    MessageRef,
    NormalizedEmail,
    PlanResult,
    Route,
    RunRecord,
    ValidatedOffer,
)
from .storage.repository import Repository

logger = logging.getLogger("weekly_deals.pipeline")


@dataclass
class StageCounts:
    parked: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    review_flagged: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class ScanResult:
    run_id: str
    coverage: Coverage
    offers: list[ValidatedOffer]
    dedup: DedupResult
    stages: StageCounts
    status: str
    cost_usd: float
    cost_known: bool
    # Which components actually ran. `--offline` quietly substitutes the mock
    # classifier and extractor, and a user checking JEV's behaviour deserves to
    # see that the numbers in front of them did not come from JEV.
    providers: dict[str, str] = field(default_factory=dict)


class PipelineService:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        repository: Repository,
        mail_source: MailSource,
        extractor: OfferExtractor,
        classifier: PromotionClassifier | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.repo = repository
        self.mail = mail_source
        self.extractor = extractor
        self.classifier = classifier

    # -- budget ------------------------------------------------------------

    def _budget_left(self, run_id: str) -> tuple[bool, float, str]:
        """Whether another paid call may be made, the spend so far, and why not.

        Unknown spend is not zero spend. If a provider gave no usable cost, the
        total below is an *understatement* and the cap cannot be honoured, so
        the run stops rather than continuing under a limit it is not actually
        applying. ``Settings.preflight`` catches the usual cause (no configured
        prices) before a run starts; this is the backstop.
        """
        limit = self.settings.app.runtime.per_run_budget_usd
        if limit <= 0:
            return True, 0.0, ""
        spent, known = self.repo.run_cost(run_id)
        if not known:
            return False, spent, "budget_unverifiable"
        if spent >= limit:
            return False, spent, "budget_exhausted"
        return True, spent, ""

    # -- retrieval ---------------------------------------------------------

    def _fetch_stream(
        self, refs: list[MessageRef]
    ) -> Iterator[tuple[MessageRef, NormalizedEmail | MailSourceError]]:
        """Yield fetched emails in the order they were listed.

        Fetching is network-bound and buys nothing from being serial, so it runs
        in a bounded thread pool -- but only when the source has declared itself
        safe for it. A client library that shares one HTTP connection interleaves
        responses instead of failing, which would silently attribute one email's
        body to another message id: far worse than a slow scan.

        Everything after this is sequential on the caller's thread: database
        writes, the budget check and paid model calls all stay ordered.
        Results are yielded in list order so a run is reproducible.
        """
        workers = self.settings.app.runtime.mail_concurrency
        concurrent = workers > 1 and self.mail.capabilities().supports_concurrent_fetch

        if not concurrent:
            for ref in refs:
                try:
                    yield ref, self.mail.fetch(ref.source_id)
                except MailSourceError as exc:
                    yield ref, exc
            return

        # Work in windows rather than submitting everything: a 90-day scan can
        # be thousands of messages and they must not all sit in memory at once.
        window = workers * 4
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="weekly_deals-fetch") as pool:
            for start in range(0, len(refs), window):
                batch = refs[start : start + window]
                futures = [pool.submit(self.mail.fetch, ref.source_id) for ref in batch]
                for ref, future in zip(batch, futures, strict=True):
                    try:
                        yield ref, future.result()
                    except MailSourceError as exc:
                        yield ref, exc
                    except Exception as exc:  # one bad message must not stop the scan
                        yield ref, MailSourceError(
                            f"unexpected fetch error: {type(exc).__name__}", code="fetch_error"
                        )

    # -- query -------------------------------------------------------------

    def build_query(self) -> str:
        mail = self.settings.app.mail
        if mail.provider == "eml_dir":
            # The folder IS the scope. Recording a Gmail query string here would
            # put a false claim about what was searched into every report.
            return f"local folder: {mail.eml_dir}"
        parts = [f"newer_than:{mail.lookback_days}d"]
        if mail.base_query:
            parts.append(mail.base_query)
        base = " ".join(parts)
        if mail.extra_queries:
            alternatives = " OR ".join(f"({q})" for q in mail.extra_queries)
            return f"({base}) OR ({alternatives})"
        return base

    # -- main --------------------------------------------------------------

    def scan(self, *, mode: str | None = None, max_messages: int | None = None) -> ScanResult:
        mode = mode or self._default_mode()
        run_id = uuid.uuid4().hex[:16]
        query = self.build_query()
        started = datetime.now(UTC)

        coverage = Coverage(
            query=query,
            lookback_days=self.settings.app.mail.lookback_days,
            range_start=self.clock.now().date()
            - timedelta(days=self.settings.app.mail.lookback_days),
            range_end=self.clock.now().date(),
        )
        self.repo.start_run(
            RunRecord(
                run_id=run_id, scope=query, mode=mode, started_at=started, coverage=coverage
            )
        )

        # The run row lands before any message is touched, so an interrupted
        # scan leaves evidence that it started rather than vanishing.
        self.repo.checkpoint()

        stages = StageCounts()
        drafts_by_message: dict[str, list] = {}
        message_row_ids: dict[str, int] = {}
        emails: dict[str, NormalizedEmail] = {}
        cap = max_messages if max_messages is not None else self.settings.app.mail.max_messages

        # -- retrieval -----------------------------------------------------
        try:
            refs, exhaustive = self.mail.collect(query, max_messages=cap)
            coverage.messages_matched = len(refs)
            # A capped run has NOT seen the window. Claiming otherwise turns
            # "we stopped early" into "there is nothing else", which is the one
            # distinction every report here is supposed to preserve.
            coverage.search_exhaustive = exhaustive
            if not exhaustive:
                stages.errors["search"] = "capped_by_max_messages"
        except MailSourceError as exc:
            coverage.search_exhaustive = False
            stages.errors["search"] = exc.code
            self.repo.finish_run(
                run_id,
                status="failed",
                coverage=coverage,
                cost_usd=0.0,
                cost_known=True,
                error=str(exc),
            )
            return ScanResult(
                run_id=run_id,
                coverage=coverage,
                offers=[],
                dedup=DedupResult(),
                stages=stages,
                status="failed",
                cost_usd=0.0,
                cost_known=True,
                providers=self.describe_providers(),
            )

        for ref, fetched in self._fetch_stream(refs):
            # Each message is committed on its way out of the loop below. Work
            # that is done -- and paid for -- must survive a later failure.
            self.repo.checkpoint()
            if isinstance(fetched, MailSourceError):
                coverage.fetch_failures += 1
                stages.errors[ref.source_id] = fetched.code
                continue
            email = fetched

            coverage.messages_fetched += 1
            if email.has_unparsed_visuals:
                coverage.unparsed_visuals += 1

            # Index every Promotions message before the semantic model route.
            # A retail or event promotion is still useful in the savings
            # calendar even when it cannot become a dining offer.
            self.repo.upsert_promotion(build_promotion_event(email, now=self.clock.now()))
            coverage.promotions_indexed += 1

            row, changed = self.repo.upsert_message(email)
            message_row_ids[email.source_id] = row.id
            emails[email.source_id] = email

            if mode == "host-ingest" and not changed and row.processing_status == "done":
                # Already handed over and ingested. Re-classifying would pay JEV
                # again for an answer that cannot have changed.
                continue

            if mode != "host-ingest" and not changed and row.processing_status == "done":
                # Body unchanged and already processed: reuse, spend nothing.
                cached = self.repo.find_extraction(
                    row.id,
                    email.content_hash,
                    # model_id, not the class name: it has to match what the
                    # adapter recorded in meta.model or the cache never hits and
                    # every re-run silently re-pays.
                    self.extractor.model_id,
                    SCHEMA_VERSION,
                )
                if cached is not None:
                    drafts_by_message[email.source_id] = self._drafts_from_cache(cached)
                    # Count it. A cache hit is still a message whose offers were
                    # extracted successfully; leaving it out made a hot re-run
                    # report "0 extracted" beside a full list of offers, which
                    # reads exactly like the stage having collapsed.
                    coverage.extraction_cached += 1
                    coverage.extraction_success += 1
                    continue

            # -- classification --------------------------------------------
            #
            # This runs for `host-ingest` as well, and that is the point of it:
            # the classifier is what keeps the host from having to read every
            # message. Most promotional mail is not a structured food offer, and a typed
            # question answered for a fraction of a cent is far cheaper than
            # putting the whole mailbox through an extractor -- whichever
            # extractor it is.
            route = Route.EXTRACT
            gate = self._gate_for(mode)
            if self.classifier is not None and gate is not None:
                ok, _spent, why = self._budget_left(run_id)
                if not ok:
                    stages.parked.append(email.source_id)
                    stages.errors[email.source_id] = why
                    self.repo.mark_message_status(row.id, f"parked_{why}")
                    continue
                # A verdict already on file for this exact body, model and
                # question version is reused. Without this a weekly re-scan paid
                # to re-classify every rejected message -- which is most of them.
                cached_call = self.repo.find_classification(
                    row.id, email.content_hash, self.classifier.model_id, self._question_version()
                )
                if cached_call is not None:
                    result = ClassificationResult.model_validate(cached_call.payload)
                    result.meta.cached = True
                else:
                    result = self.classifier.classify(email)
                    self.repo.record_classification(row.id, result)
                coverage.classified += 1
                self.repo.record_model_call(run_id, "classify", result.meta)
                if result.failed:
                    coverage.classification_failures += 1
                    stages.errors[email.source_id] = result.error_code or "classify_failed"
                route = self._route(result, email, mode)

            if route is Route.PROVISIONAL_REJECT:
                stages.rejected.append(email.source_id)
                self.repo.mark_message_status(row.id, "rejected")
                continue
            if route is Route.FALLBACK_PENDING and not self.settings.app.runtime.automatic_fallback:
                stages.parked.append(email.source_id)
                self.repo.mark_message_status(row.id, "parked_provider_error")
                continue
            if route is Route.NEEDS_RICHER_INPUT:
                stages.review_flagged.append(email.source_id)
            if route is Route.EXTRACT_WITH_REVIEW_FLAG:
                stages.review_flagged.append(email.source_id)

            # A host that reads the mailbox does the extraction too, so this
            # mode stops here -- after normalising and after routing. Only
            # messages the classifier let through are left `pending`, which is
            # what `weekly-deals pending` hands over.
            if mode == "host-ingest":
                self.repo.mark_message_status(row.id, "pending")
                continue

            # -- extraction -------------------------------------------------
            ok, _spent, why = self._budget_left(run_id)
            if not ok:
                stages.parked.append(email.source_id)
                stages.errors[email.source_id] = why
                self.repo.mark_message_status(row.id, f"parked_{why}")
                continue

            coverage.extraction_attempts += 1
            extraction = self.extractor.extract(email)
            self.repo.record_extraction(row.id, email.content_hash, extraction)
            self.repo.record_model_call(run_id, "extract", extraction.meta)

            if extraction.status is ExtractionStatus.FAILED:
                coverage.extraction_failures += 1
                stages.errors[email.source_id] = extraction.error_code or "extract_failed"
                self.repo.mark_message_status(row.id, "failed")
                continue

            coverage.extraction_success += 1
            if extraction.status is ExtractionStatus.NEEDS_REVIEW:
                stages.review_flagged.append(email.source_id)
            drafts_by_message[email.source_id] = list(extraction.offers)
            self.repo.mark_message_status(row.id, "done")
            self.repo.checkpoint()

        # -- validation and dedup -------------------------------------------
        validated: list[ValidatedOffer] = []
        for source_id, drafts in drafts_by_message.items():
            email = emails.get(source_id)
            if email is None:
                continue
            for draft in drafts:
                validated.append(
                    validation.validate(draft, email, self.clock, self.settings.app.preferences)
                )
        coverage.offers_validated = len(validated)

        dedup = deduplicate(validated)
        coverage.offers_after_dedup = len(dedup.offers)
        coverage.last_sync_at = datetime.now(UTC)

        for offer in dedup.offers:
            self.repo.upsert_offer(offer, message_row_ids)

        # Near-matches outlive the run that spotted them. Recomputed and thrown
        # away each time, they were only ever a count in the scan output, with
        # no way to look at the pair or to settle it once.
        self.repo.record_suspected_duplicates(dedup.suspected_duplicates)

        # Retention runs after the offers are written, so "is this text still
        # evidence for a live offer?" is asked of the current answer.
        self.repo.checkpoint()
        pruned = self.repo.prune_payloads(self.settings.app.privacy.payload_retention_days)
        if pruned:
            logger.info("pruned stored text for %d message(s) past the retention window", pruned)

        coverage.parked = len(set(stages.parked))
        cost, cost_known = self.repo.run_cost(run_id)
        status = "partial" if coverage.is_partial else "completed"
        self.repo.finish_run(
            run_id, status=status, coverage=coverage, cost_usd=cost, cost_known=cost_known
        )

        return ScanResult(
            run_id=run_id,
            coverage=coverage,
            offers=dedup.offers,
            dedup=dedup,
            stages=stages,
            status=status,
            cost_usd=cost,
            cost_known=cost_known,
            providers=self.describe_providers(),
        )

    # -- planning -----------------------------------------------------------

    def plan(self, coverage: Coverage | None = None) -> PlanResult:
        """Build a plan from stored offers. Never calls a model or the network."""
        offers = self.repo.list_offers()
        states = self.repo.all_user_states()
        planner = Planner(self.clock, self.settings.app.preferences)
        if coverage is None:
            latest = self.repo.latest_run()
            coverage = Coverage.model_validate(latest.coverage) if latest else Coverage()
        result = planner.build(offers, states, coverage)
        self.repo.save_plan(result, date_range="rolling")
        return result

    # -- helpers ------------------------------------------------------------

    def describe_providers(self) -> dict[str, str]:
        return {
            "mail": self.mail.capabilities().provider,
            "classifier": self.classifier.model_id if self.classifier else "none",
            "extractor": self.extractor.model_id,
        }

    #: Modes in which this application does its own extraction. `host-ingest`
    #: is not one of them: it normalises and stops.
    EXTRACTING_MODES = ("llm-only", "jev-observe", "jev-gate")

    def _default_mode(self) -> str:
        mode = self.settings.app.classification.mode
        return {"off": "llm-only", "observe": "jev-observe", "gate": "jev-gate"}[mode]

    def _gate_for(self, mode: str) -> str | None:
        """Routing strictness for this run, or None to skip classification.

        `host-ingest` has no gate word of its own, so it follows the configured
        classification mode -- `observe` discards nothing, `gate` may skip
        confident negatives once an evaluation has been recorded.
        """
        if mode == "jev-observe":
            return "observe"
        if mode == "jev-gate":
            return "gate"
        if mode == "host-ingest":
            configured = self.settings.app.classification.mode
            return configured if configured in ("observe", "gate") else None
        return None

    def _question_version(self) -> str:
        return getattr(self.classifier, "prompt_version", "v1") if self.classifier else "v1"

    def _route(self, result: ClassificationResult, email: NormalizedEmail, mode: str) -> Route:
        config = self.settings.app.classification
        if self._gate_for(mode) == "gate" and not config.gate_evaluation_record:
            # Belt and braces: config validation should have caught this already.
            raise RuntimeError("gate mode requires a recorded gate evaluation")
        return route_for(
            result,
            email,
            mode=self._gate_for(mode) or "observe",
            reject_below=config.reject_below,
            accept_above=config.accept_above,
        )

    @staticmethod
    def _drafts_from_cache(cached) -> list:  # type: ignore[no-untyped-def]
        from .schemas import ExtractionResult

        try:
            result = ExtractionResult.model_validate(cached.payload)
        except Exception:  # pragma: no cover - corrupt cache is not fatal
            logger.warning("could not read cached extraction; re-extracting next run")
            return []
        return list(result.offers)
