"""Repository layer.

The one rule that shapes this file: a sync writes model facts, a user writes
personal state, and the two never collide. ``upsert_offer`` deliberately has no
path that can reach the user-state file.

This interface is the seam the rest of the application is written against. It
sits on :mod:`weekly_deals.storage.store`, a directory of JSON files: a message id
here is the account plus the mailbox's own identifier, not an autoincrementing
row number, because there is no table to allocate one from and carrying the
provider's id through is one fewer mapping to get wrong.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from ..schemas import (
    ClassificationResult,
    Coverage,
    ExtractionResult,
    NormalizedEmail,
    OfferUserState,
    PlanResult,
    PromotionEvent,
    RunRecord,
    TimeStatus,
    UserStatus,
    ValidatedOffer,
)
from .records import (
    CachedCall,
    MessageRecord,
    OfferRecord,
    OfferSourceRecord,
    StoredModelCall,
    StoredRun,
    StoredUserState,
    message_key,
)
from .store import JsonStore, now_utc


class Repository:
    def __init__(self, store: JsonStore) -> None:
        self.store = store

    def checkpoint(self) -> None:
        """Flush buffered collections to disk.

        A scan is long, costs money per message, and can be interrupted by
        anything from a provider client raising to the machine sleeping. Called
        between messages, this keeps finished work finished: everything already
        written stays written, and a retry reuses it from cache instead of
        paying for it twice.
        """
        self.store.commit()

    # -- messages ---------------------------------------------------------

    def upsert_message(self, email: NormalizedEmail) -> tuple[MessageRecord, bool]:
        """Insert or update one message. Returns (record, content_changed).

        ``content_changed`` is False when the body hash is unchanged, which is
        what lets a re-scan skip paid model calls.
        """
        record = self.store.read_message(message_key(email.account_alias, email.source_id))
        new_hash = email.content_hash
        changed = True
        moment = now_utc()

        if record is None:
            record = MessageRecord(
                source_id=email.source_id,
                account_alias=email.account_alias,
                thread_id=email.thread_id,
                first_seen_at=moment,
            )
        else:
            changed = record.body_hash != new_hash

        record.subject = email.subject
        record.sender = email.sender
        record.sent_at = email.sender_date
        record.received_at = email.received_date
        record.date_provenance = email.date_provenance
        record.body_hash = new_hash
        record.body_complete = email.body_complete
        record.has_unparsed_visuals = email.has_unparsed_visuals
        record.truncated = email.truncated
        record.parse_status = str(email.parse_status)
        record.last_seen_at = moment
        record.normalized_text = email.normalized_text
        record.structured_markup = list(email.structured_markup)
        record.payload_pruned_at = None
        if changed:
            record.processing_status = "pending"
            # The body moved on, so results for the old body are not results for
            # this one. Dropping them here stops a stale extraction being served
            # as a cache hit for text it never saw.
            record.classifications = [
                entry for entry in record.classifications if entry.body_hash == new_hash
            ]
            record.extractions = [
                entry for entry in record.extractions if entry.body_hash == new_hash
            ]

        self.store.write_message(record)
        return record, changed

    def get_message(self, account_alias: str, provider_message_id: str) -> MessageRecord | None:
        return self.store.read_message(message_key(account_alias, provider_message_id))

    def mark_message_status(self, message_id: str, status: str) -> None:
        record = self.store.read_message(message_id)
        if record is not None:
            record.processing_status = status
            self.store.write_message(record)

    # -- model results ----------------------------------------------------

    def record_classification(
        self,
        message_id: str,
        result: ClassificationResult,
        *,
        cache_model: str | None = None,
    ) -> None:
        record = self.store.read_message(message_id)
        if record is None:
            return
        record.remember(
            "classifications",
            CachedCall(
                body_hash=record.body_hash,
                provider=result.meta.provider,
                # An alias such as jev-latest is known before the request;
                # the resolved version remains in the payload for reporting.
                model=cache_model or result.meta.model,
                version=result.meta.prompt_version or "v1",
                status="failed" if result.failed else "ok",
                error_code=result.error_code,
                payload=json.loads(result.model_dump_json()),
                created_at=now_utc(),
            ),
        )
        self.store.write_message(record)

    def find_classification(
        self, message_id: str, body_hash: str, model: str, prompt_version: str
    ) -> CachedCall | None:
        record = self.store.read_message(message_id)
        if record is None:
            return None
        found = record.find_classification(body_hash, model, prompt_version)
        # An old authentication failure must not prevent a retry after the
        # user fixes their key. Only successful judgments are reusable.
        if found is not None and found.status == "failed":
            return None
        return found

    def record_extraction(
        self, message_id: str, body_hash: str, result: ExtractionResult
    ) -> None:
        record = self.store.read_message(message_id)
        if record is None:
            return
        record.remember(
            "extractions",
            CachedCall(
                body_hash=body_hash,
                provider=result.meta.provider,
                model=result.meta.model,
                version=result.meta.schema_version,
                status=str(result.status),
                error_code=result.error_code,
                payload=json.loads(result.model_dump_json()),
                created_at=now_utc(),
            ),
        )
        self.store.write_message(record)

    def find_extraction(
        self, message_id: str, body_hash: str, model: str, schema_version: str
    ) -> CachedCall | None:
        record = self.store.read_message(message_id)
        if record is None:
            return None
        found = record.find_extraction(body_hash, model, schema_version)
        # A cached *failure* is not a cached result. Serving it as one would turn
        # a transient 429 into a permanent "this email has no offers".
        if found is not None and found.status == "failed":
            return None
        return found

    # -- offers ------------------------------------------------------------

    def upsert_offer(self, offer: ValidatedOffer, message_ids: dict[str, str]) -> OfferRecord:
        """Write model-derived offer facts.

        Never reads or writes user state. A bumped ``version`` signals the facts
        changed; the user's own state stays attached to ``offer_id``.
        """
        existing = self.store.read_offer(offer.offer_id)
        payload = json.loads(offer.model_dump_json())
        moment = now_utc()

        if existing is None:
            record = OfferRecord(
                offer_id=offer.offer_id,
                version=offer.version,
                payload=payload,
                first_seen_at=moment,
                updated_at=moment,
            )
        else:
            record = existing
            if record.payload != payload:
                record.version += 1
                record.updated_at = moment
            record.payload = payload

        known = {source.source_id for source in record.sources}
        for source_id in offer.source_message_ids:
            if source_id in known or source_id not in message_ids:
                continue
            record.sources.append(
                OfferSourceRecord(
                    source_id=source_id,
                    evidence=[
                        json.loads(item.model_dump_json())
                        for item in offer.evidence
                        if item.message_id == source_id
                    ],
                    created_at=moment,
                )
            )

        self.store.put_offer(record)
        return record

    def list_offers(self, *, include_dismissed: bool = False) -> list[ValidatedOffer]:
        states = self.store.user_states()
        offers: list[ValidatedOffer] = []
        for offer_id, raw in self.store.offers().items():
            if not include_dismissed:
                state = states.get(offer_id)
                if state and state.get("status") == str(UserStatus.DISMISSED):
                    continue
            offers.append(ValidatedOffer.model_validate(raw["payload"]))
        return offers

    def get_offer(self, offer_id: str) -> ValidatedOffer | None:
        record = self.store.read_offer(offer_id)
        return ValidatedOffer.model_validate(record.payload) if record else None

    # -- generic promotion calendar ---------------------------------------

    def upsert_promotion(self, event: PromotionEvent) -> None:
        self.store.put_promotion(event.promotion_id, json.loads(event.model_dump_json()))

    def list_promotions(self) -> list[PromotionEvent]:
        return [PromotionEvent.model_validate(raw) for raw in self.store.promotions().values()]

    def offer_version(self, offer_id: str) -> int | None:
        record = self.store.read_offer(offer_id)
        return record.version if record else None

    # -- suspected duplicates -----------------------------------------------

    def record_suspected_duplicates(self, pairs: list[tuple[str, str]]) -> None:
        """Keep near-matches across runs so the user can settle them once.

        Recomputed and thrown away each run, they were only ever a count in the
        scan output -- there was no way to look at the pair, and no way to say
        "these two really are different" and stop being told again.
        """
        if pairs:
            self.store.set_duplicates(pairs)

    def suspected_duplicates(self) -> list[tuple[str, str]]:
        return [(pair[0], pair[1]) for pair in self.store.duplicates() if len(pair) == 2]

    def dismiss_duplicate(self, left: str, right: str) -> None:
        self.store.clear_duplicate(left, right)

    # -- user state (never touched by sync) --------------------------------

    def get_user_state(self, offer_id: str) -> OfferUserState:
        raw = self.store.user_states().get(offer_id)
        if raw is None:
            return OfferUserState(offer_id=offer_id)
        stored = StoredUserState.model_validate(raw)
        return OfferUserState(
            offer_id=stored.offer_id,
            status=stored.status,
            used_at=stored.used_at,
            dismissed_at=stored.dismissed_at,
            saved=stored.saved,
            planned_date=stored.planned_date,
            eligibility_overrides=stored.eligibility_overrides,
            note=stored.note,
            revision=stored.revision,
            updated_at=stored.updated_at,
        )

    def set_user_state(
        self, state: OfferUserState, *, expected_revision: int | None = None
    ) -> OfferUserState:
        """Write user state with optimistic concurrency.

        ``expected_revision`` guards against two browser tabs clobbering each
        other. Passing None means "last write wins", which the CLI uses. The
        current revision is returned on every read, so a caller that wants the
        guard can actually obtain the value to pass.
        """
        raw = self.store.user_states().get(state.offer_id)
        current = StoredUserState.model_validate(raw) if raw else None
        if (
            expected_revision is not None
            and current is not None
            and current.revision != expected_revision
        ):
            raise ValueError(
                f"user state for {state.offer_id} changed "
                f"(expected revision {expected_revision}, found {current.revision})"
            )

        self.store.put_user_state(
            StoredUserState(
                offer_id=state.offer_id,
                status=state.status,
                used_at=state.used_at,
                dismissed_at=state.dismissed_at,
                saved=state.saved,
                planned_date=state.planned_date,
                eligibility_overrides=state.eligibility_overrides,
                note=state.note,
                revision=(current.revision + 1) if current else 1,
                updated_at=now_utc(),
            )
        )
        return self.get_user_state(state.offer_id)

    def all_user_states(self) -> dict[str, OfferUserState]:
        return {offer_id: self.get_user_state(offer_id) for offer_id in self.store.user_states()}

    # -- runs, plans, costs ------------------------------------------------

    def start_run(self, record: RunRecord) -> StoredRun:
        run = StoredRun(
            run_id=record.run_id,
            scope=record.scope,
            mode=record.mode,
            status=record.status,
            coverage=record.coverage,
            started_at=record.started_at,
        )
        self.store.put_run(run)
        return run

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        coverage: Coverage,
        cost_usd: float,
        cost_known: bool,
        error: str | None = None,
    ) -> None:
        run = self.store.read_run(run_id)
        if run is None:
            return
        run.status = status
        run.coverage = coverage
        run.estimated_cost_usd = cost_usd
        run.cost_known = cost_known
        run.error = error
        run.finished_at = now_utc()
        self.store.put_run(run)

    def record_model_call(self, run_id: str, stage: str, meta: Any) -> None:
        run = self.store.read_run(run_id)
        if run is None:
            return
        run.model_calls.append(
            StoredModelCall(
                stage=stage,
                provider=meta.provider,
                model=meta.model,
                input_tokens=meta.usage.input_tokens,
                output_tokens=meta.usage.output_tokens,
                estimated_cost_usd=meta.usage.estimated_cost_usd,
                cost_known=meta.usage.cost_known,
                latency_ms=meta.latency_ms,
                retry_count=meta.retry_count,
                cached=meta.cached,
            )
        )
        self.store.put_run(run)

    def run_cost(self, run_id: str) -> tuple[float, bool]:
        """Total spend for a run and whether every call reported usage."""
        run = self.store.read_run(run_id)
        if run is None:
            return 0.0, True
        total = 0.0
        known = True
        for call in run.model_calls:
            if call.cached:
                continue
            if call.cost_known and call.estimated_cost_usd is not None:
                total += call.estimated_cost_usd
            else:
                known = False
        return round(total, 6), known

    def latest_run(self) -> StoredRun | None:
        return self.store.latest_run()

    def save_plan(self, plan: PlanResult, date_range: str) -> None:
        payload = json.loads(plan.model_dump_json())
        payload["date_range"] = date_range
        self.store.put_plan(plan.plan_id, payload)

    def get_plan(self, plan_id: str) -> PlanResult | None:
        raw = self.store.read_plan(plan_id)
        if raw is None:
            return None
        raw.pop("date_range", None)
        return PlanResult.model_validate(raw)

    # -- retention -----------------------------------------------------------

    def prune_payloads(self, retention_days: int) -> int:
        """Forget stored email text once it is no longer evidence.

        Text behind an offer that is still live is kept, because the validator's
        quotes have to remain checkable. Everything else -- rejected mail,
        expired promotions -- keeps only its hash and its model verdict, which is
        enough to skip it on the next scan without reading the mailbox again.

        The window is measured from the email's own date, which is what a person
        means by "do not keep my mail for more than N days".
        """
        if retention_days <= 0:
            return 0
        keep: set[str] = set()
        for offer in self.list_offers(include_dismissed=True):
            if offer.time_status is not TimeStatus.EXPIRED:
                keep.update(offer.source_message_ids)
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        return self.store.prune_payloads(keep_source_ids=keep, older_than=cutoff)
