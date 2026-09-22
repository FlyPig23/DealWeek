"""What the store holds on disk.

These are the storage shapes, kept apart from :mod:`mealdeals.schemas`, which
describes the domain. They are Pydantic models for one practical reason: a field
added in a later version reads back as its default from a file written by an
earlier one, so ordinary schema growth needs no migration step at all.

The separation the whole design rests on is physical here too. A sync rewrites
:class:`MessageRecord` and :class:`OfferRecord`. Nothing in that path can reach
:class:`StoredUserState`, which lives in its own file and is only ever written
by an explicit user action.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import Coverage, TriState, UserStatus


def message_key(account_alias: str, source_id: str) -> str:
    """Identity of one stored message. Unit separator cannot occur in either part."""
    return f"{account_alias}\x1f{source_id}"


class Stored(BaseModel):
    model_config = ConfigDict(extra="ignore")
    """``extra="ignore"`` on purpose: a file written by a *newer* build that
    added a field must still load, minus that field, rather than refusing to
    open and looking like data loss."""


class CachedCall(Stored):
    """One model result, keyed so a re-scan can reuse it instead of paying again."""

    body_hash: str
    provider: str = ""
    model: str = ""
    version: str = ""
    status: str = ""
    error_code: str | None = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime | None = None


class MessageRecord(Stored):
    """One email: what the mailbox said, and what the models made of it.

    Message text and model results share a file because they share a lifetime:
    both are keyed by the body hash, both are re-derivable by scanning again,
    and both should disappear together when retention expires.
    """

    source_id: str
    account_alias: str = "default"
    thread_id: str | None = None
    subject: str = ""
    sender: str = ""
    sent_at: datetime | None = None
    received_at: datetime | None = None
    date_provenance: str = "unknown"

    body_hash: str = ""
    body_complete: bool = True
    has_unparsed_visuals: bool = False
    truncated: bool = False
    parse_status: str = "complete"
    processing_status: str = "pending"

    normalized_text: str = ""
    structured_markup: list = Field(default_factory=list)

    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    payload_pruned_at: datetime | None = None

    classifications: list[CachedCall] = Field(default_factory=list)
    extractions: list[CachedCall] = Field(default_factory=list)

    @property
    def id(self) -> str:
        """Primary key: the account plus the provider's own message id.

        There is no synthetic row number to allocate, so callers carry the
        mailbox's own identifier all the way through. The account is part of it
        because two accounts can legitimately hand out the same message id, and
        merging those two emails would be a data-loss bug.
        """
        return message_key(self.account_alias, self.source_id)

    def find_extraction(self, body_hash: str, model: str, version: str) -> CachedCall | None:
        return self._find(self.extractions, body_hash, model, version)

    def find_classification(self, body_hash: str, model: str, version: str) -> CachedCall | None:
        return self._find(self.classifications, body_hash, model, version)

    @staticmethod
    def _find(
        entries: list[CachedCall], body_hash: str, model: str, version: str
    ) -> CachedCall | None:
        for entry in entries:
            if (entry.body_hash, entry.model, entry.version) == (body_hash, model, version):
                return entry
        return None

    def remember(self, bucket: str, entry: CachedCall) -> None:
        """Replace any entry with the same cache key, then append."""
        entries: list[CachedCall] = getattr(self, bucket)
        kept = [
            existing
            for existing in entries
            if (existing.body_hash, existing.model, existing.version)
            != (entry.body_hash, entry.model, entry.version)
        ]
        kept.append(entry)
        setattr(self, bucket, kept)


class OfferSourceRecord(Stored):
    """An email that evidenced an offer. Reminders append; they never replace."""

    source_id: str
    variant: str = "original"
    evidence: list = Field(default_factory=list)
    created_at: datetime | None = None


class OfferRecord(Stored):
    offer_id: str
    version: int = 1
    payload: dict = Field(default_factory=dict)
    sources: list[OfferSourceRecord] = Field(default_factory=list)
    first_seen_at: datetime | None = None
    updated_at: datetime | None = None


class StoredUserState(Stored):
    """The user's own decisions. Never written by a sync."""

    offer_id: str
    status: UserStatus = UserStatus.UNUSED_OR_UNKNOWN
    used_at: datetime | None = None
    dismissed_at: datetime | None = None
    saved: bool = False
    planned_date: date | None = None
    eligibility_overrides: dict[str, TriState] = Field(default_factory=dict)
    note: str | None = None
    #: Incremented on every write so two browser tabs cannot clobber each other.
    revision: int = 1
    updated_at: datetime | None = None


class StoredModelCall(Stored):
    stage: str
    provider: str = ""
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    cost_known: bool = False
    latency_ms: int | None = None
    retry_count: int = 0
    cached: bool = False


class StoredRun(Stored):
    run_id: str
    scope: str = ""
    mode: str = ""
    status: str = "running"
    coverage: Coverage = Field(default_factory=Coverage)
    error: str | None = None
    estimated_cost_usd: float = 0.0
    cost_known: bool = True
    started_at: datetime | None = None
    finished_at: datetime | None = None
    model_calls: list[StoredModelCall] = Field(default_factory=list)


def as_normalized_email(record: MessageRecord):
    """Rebuild the domain object from what was stored.

    Used by the host-ingest path: the normalizer already did the MIME, HTML,
    charset and date work when the message was read, and the validator must see
    exactly that text -- the same bytes its evidence quotes are checked against.
    Re-deriving it anywhere else would let the two drift.
    """
    from ..schemas import NormalizedEmail, ParseStatus

    return NormalizedEmail(
        source_id=record.source_id,
        thread_id=record.thread_id,
        account_alias=record.account_alias,
        subject=record.subject,
        sender=record.sender,
        sender_date=record.sent_at,
        received_date=record.received_at,
        date_provenance=record.date_provenance,  # type: ignore[arg-type]
        normalized_text=record.normalized_text,
        structured_markup=list(record.structured_markup),
        body_complete=record.body_complete,
        has_unparsed_visuals=record.has_unparsed_visuals,
        truncated=record.truncated,
        parse_status=ParseStatus(record.parse_status),
    )
