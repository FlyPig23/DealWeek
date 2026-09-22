"""MailSource contract.

A mail source reads. It holds no model, makes no judgement about whether an
email is interesting, and returns the same normalized shape whatever the
backend is -- Gmail REST, an MCP server, or fixtures.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from ..schemas import MailCapabilities, MessagePage, MessageRef, NormalizedEmail


class MailSourceError(RuntimeError):
    """Transport or auth failure. Never to be interpreted as 'no messages'."""

    def __init__(self, message: str, *, code: str = "mail_error", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class AuthRequired(MailSourceError):
    def __init__(self, message: str = "authorisation required or expired") -> None:
        super().__init__(message, code="auth_required", retryable=False)


class MailSource(ABC):
    """Read-only mailbox access."""

    #: Set by ``iter_all`` when a caller's cap stopped the walk early. Read it
    #: through ``collect``; subclasses never need to touch it.
    _truncated: bool = False

    @abstractmethod
    def capabilities(self) -> MailCapabilities: ...

    @abstractmethod
    def search(self, query: str, page_cursor: str | None = None) -> MessagePage:
        """One page of matching message references.

        Implementations must only set ``is_complete`` once the backend has
        confirmed there is no further page.
        """

    @abstractmethod
    def fetch(self, message_id: str) -> NormalizedEmail:
        """Full normalized message. Raise rather than return a partial stub."""

    def changes(self, sync_cursor: str | None) -> MessagePage:
        """Incremental changes. Sources without history declare it and re-scan."""
        raise NotImplementedError("this source does not support incremental history")

    def iter_all(self, query: str, *, max_messages: int | None = None) -> Iterator[MessageRef]:
        """Walk every page to the end.

        Stops only on a terminal page or the caller's cap -- never because a page
        came back empty, which Gmail can do mid-pagination.

        Use :meth:`collect` instead of ``list(iter_all(...))`` unless you can
        already prove the search was exhaustive: a generator cannot tell the
        caller *why* it stopped, and treating "hit the cap" as "reached the end"
        puts a false completeness claim into the report.
        """
        cursor: str | None = None
        seen: set[str] = set()
        yielded = 0
        while True:
            page = self.search(query, cursor)
            for item in page.items:
                if item.source_id in seen:
                    continue
                seen.add(item.source_id)
                yield item
                yielded += 1
                if max_messages is not None and yielded >= max_messages:
                    self._truncated = True
                    return
            if page.is_complete or page.next_cursor is None:
                return
            cursor = page.next_cursor

    def collect(
        self, query: str, *, max_messages: int | None = None
    ) -> tuple[list[MessageRef], bool]:
        """Every matching ref, plus whether the search actually reached the end.

        The second element is ``False`` when ``max_messages`` cut the walk short.
        A capped scan has not seen the whole window, and a report that says
        otherwise is the "we didn't finish" state disguised as "nothing more".
        """
        self._truncated = False
        refs = list(self.iter_all(query, max_messages=max_messages))
        return refs, not self._truncated
