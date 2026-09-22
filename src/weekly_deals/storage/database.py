"""Opening the store.

Kept as its own module so callers say "open the store" rather than knowing where
the files live. There is no engine, no connection pool and no session: see
:mod:`weekly_deals.storage.store` for why a database was the wrong shape for this
data.

``:memory:`` is accepted for tests and the offline demo. It maps to a temporary
directory that is removed when the process exits, so a demo run leaves nothing
behind and cannot collide with a real scan's files.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .repository import Repository
from .store import JsonStore

IN_MEMORY = ":memory:"


def open_store(path: Path | str) -> JsonStore:
    """Open (creating if needed) the store at ``path``."""
    if str(path) == IN_MEMORY:
        temporary = tempfile.mkdtemp(prefix="weekly_deals-ephemeral-")
        atexit.register(shutil.rmtree, temporary, ignore_errors=True)
        store = JsonStore(temporary)
    else:
        store = JsonStore(path)
    store.initialise()
    return store


@contextmanager
def repository_scope(store: JsonStore) -> Iterator[Repository]:
    """A unit of work.

    Writes land as they happen; this flushes the buffered collections at the
    end. There is no rollback, and deliberately so -- a scan that fails on its
    ninth message must keep the eight extractions it already paid for.
    """
    repository = Repository(store)
    try:
        yield repository
    finally:
        store.commit()
