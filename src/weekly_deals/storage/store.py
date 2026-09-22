"""On-disk JSON store.

There is no database here, and the data does not want one. Everything this
application stores is either looked up by a single key or read in full and
filtered in Python: no joins, no aggregates, no range queries. One person's
promotional mail over a 90-day window is a few hundred records.

What durability actually requires at this size is small and explicit:

* **Atomic writes.** Every file is written to a sibling temporary file, fsynced
  and then ``os.replace``d. A reader therefore sees the previous version or the
  next one, never half of either, and a crash cannot leave a torn file.
* **One writer at a time.** Writers take a lock file for the duration of a
  single write, not for a whole scan. A ninety-minute sync and the local web UI
  can run at once, because the scan releases the lock between messages.
* **Readers never lock.** They open a file that ``os.replace`` guarantees is
  complete.

The layout is meant to be read by a person:

    store/
      meta.json                    schema version
      messages/<key>.json          one message: its text, and its model results
      offers.json                  derived offers
      promotions.json              generic savings calendar entries
      user_state.json              used / dismissed / planned -- the precious one
      duplicates.json              suspected duplicate pairs, awaiting review
      runs.json                    recent runs with their costs
      plans/<plan_id>.json         plan snapshots

Every file is JSON the user can grep, diff, hand-correct or delete. "Delete my
data" is ``rm -r``, with no write-ahead log or journal left behind holding a
copy of their mail.

Schema changes are handled by Pydantic defaults rather than by migrations: a
field added later reads as its default out of an older file. ``SCHEMA_VERSION``
exists for changes that defaults cannot express, and :func:`_migrate` is where
they would go.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .records import MessageRecord, OfferRecord, StoredRun, StoredUserState

SCHEMA_VERSION = 1

# Kept deliberately small. These are diagnostics, not history worth unbounded
# disk: the offers and the user's own state are what matter.
MAX_RUNS = 200
MAX_PLANS = 50


def _key(value: str) -> str:
    """Filename-safe, collision-resistant key for an arbitrary provider id."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def now_utc() -> datetime:
    return datetime.now(UTC)


class JsonStore:
    """Durable JSON files under one directory."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.messages_dir = self.root / "messages"
        self.plans_dir = self.root / "plans"
        self._lock_path = self.root / ".writer.lock"
        self._cache: dict[str, Any] = {}
        self._dirty: set[str] = set()

    # -- lifecycle ---------------------------------------------------------

    def initialise(self) -> None:
        for directory in (self.root, self.messages_dir, self.plans_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        meta_path = self.root / "meta.json"
        if not meta_path.exists():
            self._write_atomic(
                meta_path, {"schema_version": SCHEMA_VERSION, "created_at": _iso(now_utc())}
            )
            return
        meta = self._read_json(meta_path) or {}
        found = int(meta.get("schema_version", 0))
        if found != SCHEMA_VERSION:
            self._migrate(found)
            meta["schema_version"] = SCHEMA_VERSION
            self._write_atomic(meta_path, meta)

    def _migrate(self, from_version: int) -> None:
        """Upgrade an older store in place.

        Nothing to do yet: every change so far has been an added field, and
        Pydantic supplies its default when an older file lacks it. A change that
        defaults cannot express -- a renamed field, a changed unit -- is handled
        here, rewriting the affected files once.
        """
        if from_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"this store was written by a newer Weekly Deals (schema {from_version}); "
                f"this build understands {SCHEMA_VERSION}. Upgrade rather than "
                "downgrade, so your offer state is not rewritten by older rules."
            )

    # -- primitives --------------------------------------------------------

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        """Exclusive across processes, held only for one write.

        Taken per write rather than per scan, so a long sync never blocks the
        user marking an offer used in the web UI.
        """
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX)
            except (ImportError, OSError):
                # No flock (or a filesystem that will not do it). Atomic replace
                # still keeps every individual file consistent; only the
                # ordering of two simultaneous writers is unguarded, and this is
                # a single-user tool.
                pass
            yield
        finally:
            with suppress(OSError):
                os.close(handle)

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            with path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            # A file that is not valid JSON is not silently treated as empty:
            # that would look exactly like "you have no saved offers".
            raise RuntimeError(
                f"{path} is not readable JSON. It may have been edited by hand or "
                "truncated by a full disk; move it aside to start fresh."
            ) from None

    def _write_atomic(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except BaseException:
            with suppress(OSError):
                os.unlink(temporary)
            raise

    # -- collections held in memory ---------------------------------------
    #
    # Small, read whole, written back whole. Loaded once per store instance and
    # flushed by `commit`, so a scan does not rewrite the offer list per message.

    def _collection(self, name: str, default: Any) -> Any:
        if name not in self._cache:
            loaded = self._read_json(self.root / f"{name}.json")
            self._cache[name] = default if loaded is None else loaded
        return self._cache[name]

    def _touch(self, name: str) -> None:
        self._dirty.add(name)

    def commit(self) -> None:
        """Flush buffered collections. Safe to call as often as you like."""
        if not self._dirty:
            return
        with self._write_lock():
            for name in sorted(self._dirty):
                self._write_atomic(self.root / f"{name}.json", self._cache[name])
        self._dirty.clear()

    # -- messages (one file each, so a scan never rewrites the whole set) ---

    def message_path(self, key: str) -> Path:
        return self.messages_dir / f"{_key(key)}.json"

    def read_message(self, key: str) -> MessageRecord | None:
        raw = self._read_json(self.message_path(key))
        if raw is None:
            return None
        record = MessageRecord.model_validate(raw)
        if record.id != key:  # pragma: no cover - 24-hex-char digest collision
            return None
        return record

    def write_message(self, record: MessageRecord) -> None:
        with self._write_lock():
            self._write_atomic(
                self.message_path(record.id),
                json.loads(record.model_dump_json()),
            )

    def iter_messages(self) -> Iterator[MessageRecord]:
        for path in sorted(self.messages_dir.glob("*.json")):
            raw = self._read_json(path)
            if raw is not None:
                yield MessageRecord.model_validate(raw)

    def find_by_source_id(self, source_id: str) -> list[MessageRecord]:
        """Every stored message with this provider id, across accounts.

        A caller that read the mailbox itself knows the provider's message id
        but not which account alias this application filed it under. Returning
        a list rather than a guess keeps the ambiguous case -- the same id in
        two accounts -- visible instead of silently picking one.
        """
        return [record for record in self.iter_messages() if record.source_id == source_id]

    def delete_message(self, key: str) -> None:
        with self._write_lock(), suppress(OSError):
            self.message_path(key).unlink()

    # -- offers ------------------------------------------------------------

    def offers(self) -> dict[str, dict]:
        return self._collection("offers", {})

    def put_offer(self, record: OfferRecord) -> None:
        self.offers()[record.offer_id] = json.loads(record.model_dump_json())
        self._touch("offers")

    def read_offer(self, offer_id: str) -> OfferRecord | None:
        raw = self.offers().get(offer_id)
        return OfferRecord.model_validate(raw) if raw else None

    # -- generic promotion calendar ---------------------------------------

    def promotions(self) -> dict[str, dict]:
        return self._collection("promotions", {})

    def put_promotion(self, promotion_id: str, payload: dict) -> None:
        self.promotions()[promotion_id] = payload
        self._touch("promotions")

    def read_promotion(self, promotion_id: str) -> dict | None:
        return self.promotions().get(promotion_id)

    def promotion_dedup(self) -> dict:
        return self._collection("promotion_dedup", {})

    def put_promotion_dedup(self, result: dict) -> None:
        self._cache["promotion_dedup"] = result
        self._touch("promotion_dedup")

    # -- user state (written through: it can never be recomputed) ----------

    def user_states(self) -> dict[str, dict]:
        return self._collection("user_state", {})

    def put_user_state(self, record: StoredUserState) -> None:
        """Write immediately.

        Everything else in this store is derived from the mailbox and can be
        rebuilt by scanning again. This cannot: it is the only record of what
        the person decided, so it is never left sitting in a buffer.
        """
        self.user_states()[record.offer_id] = json.loads(record.model_dump_json())
        with self._write_lock():
            self._write_atomic(self.root / "user_state.json", self._cache["user_state"])
        self._dirty.discard("user_state")

    # -- suspected duplicates ----------------------------------------------

    def duplicates(self) -> list[list[str]]:
        return self._collection("duplicates", [])

    def set_duplicates(self, pairs: list[tuple[str, str]]) -> None:
        merged = {tuple(pair) for pair in self.duplicates()}
        merged.update(pairs)
        self._cache["duplicates"] = sorted([list(pair) for pair in merged])
        self._touch("duplicates")

    def clear_duplicate(self, left: str, right: str) -> None:
        wanted = sorted((left, right))
        self._cache["duplicates"] = [p for p in self.duplicates() if sorted(p) != wanted]
        self._touch("duplicates")

    # -- runs ---------------------------------------------------------------

    def runs(self) -> list[dict]:
        return self._collection("runs", [])

    def put_run(self, record: StoredRun) -> None:
        payload = json.loads(record.model_dump_json())
        rows = [row for row in self.runs() if row.get("run_id") != record.run_id]
        rows.insert(0, payload)
        self._cache["runs"] = rows[:MAX_RUNS]
        self._touch("runs")

    def read_run(self, run_id: str) -> StoredRun | None:
        for row in self.runs():
            if row.get("run_id") == run_id:
                return StoredRun.model_validate(row)
        return None

    def latest_run(self) -> StoredRun | None:
        rows = self.runs()
        return StoredRun.model_validate(rows[0]) if rows else None

    # -- plans ---------------------------------------------------------------

    def put_plan(self, plan_id: str, payload: dict) -> None:
        with self._write_lock():
            self._write_atomic(self.plans_dir / f"{_key(plan_id)}.json", payload)
            # Snapshots are a convenience for explaining an old report, not an
            # archive. Unbounded, one accumulated per dashboard page load.
            existing = sorted(
                self.plans_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            for stale in existing[MAX_PLANS:]:
                with suppress(OSError):
                    stale.unlink()

    def read_plan(self, plan_id: str) -> dict | None:
        return self._read_json(self.plans_dir / f"{_key(plan_id)}.json")

    # -- retention ------------------------------------------------------------

    def prune_payloads(self, *, keep_source_ids: set[str], older_than: datetime) -> int:
        """Drop stored email text that is no longer needed as evidence.

        ``older_than`` is compared against the message's own date.

        The plan's rule, made real: temporary raw content goes early, offers that
        are still live keep the minimum evidence behind them, and a rejected
        email keeps only its hash and classification so a re-scan can skip it
        without re-reading the mailbox. Only the *text* is dropped -- the record
        stays, so the body hash still suppresses a repeat model call.
        """
        pruned = 0
        for record in list(self.iter_messages()):
            if record.source_id in keep_source_ids or not record.normalized_text:
                continue
            # Aged by the email's own date, not by when we last scanned it. A
            # message inside the lookback window is re-fetched every run, so
            # "last seen" is always today and a retention window keyed on it
            # would never expire anything.
            sent = record.sent_at or record.received_at or record.first_seen_at
            if sent is not None and sent.replace(tzinfo=UTC) > older_than:
                continue
            record.normalized_text = ""
            record.structured_markup = []
            record.payload_pruned_at = now_utc()
            self.write_message(record)
            pruned += 1
        return pruned
