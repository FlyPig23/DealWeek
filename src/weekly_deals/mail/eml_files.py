"""Read a folder of exported ``.eml`` files.

This is the missing rung on the ladder between the synthetic demo and a real
mailbox. It runs the entire pipeline against *real* promotional email -- real
nested MIME, real vendor HTML, real footnotes, real encodings -- while needing
no OAuth, no API key, and sending nothing off the machine.

That matters because the normalizer is the component most likely to break on
real mail, and it is the one the synthetic corpus tests least convincingly:
fixtures are written by the same person who wrote the parser.

Use raw RFC822/MIME from a host mail connector, or export messages as .eml from
your mail client. Gmail calls this "Download message". Outlook's .msg format is
not RFC822 and must be exported as MIME/.eml instead, not simply renamed.

Usage::

    weekly-deals scan --mail-dir ~/Desktop/test-emails --mode llm-only --offline

With ``--offline`` (or ``LLM_PROVIDER=mock``) nothing leaves the machine at all,
so this is also the safe way to check the parser against mail you would rather
not send to a provider.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from ..schemas import MailCapabilities, MessagePage, MessageRef, NormalizedEmail
from .base import MailSource, MailSourceError
from .normalize import from_rfc822

SUFFIXES = (".eml", ".mbox.eml", ".msg.eml", ".txt")


class EmlDirectorySource(MailSource):
    """A directory of RFC822 files, presented as a mail source.

    The directory *is* the scope, so the search query is ignored. The whole
    directory is enumerated, which is why ``is_complete`` can honestly be true.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        account_alias: str = "local-eml",
        page_size: int = 25,
        recursive: bool = True,
    ) -> None:
        self.directory = Path(directory).expanduser()
        if not self.directory.is_dir():
            raise MailSourceError(
                f"not a directory: {self.directory}", code="config", retryable=False
            )
        self.account_alias = account_alias.strip()
        if not self.account_alias:
            raise MailSourceError("account_alias must not be blank", code="config")
        self.page_size = page_size
        self._files = self._discover(recursive)
        if not self._files:
            raise MailSourceError(
                f"no .eml files found in {self.directory}. Export a few messages "
                "from your mail client first.",
                code="empty_directory",
                retryable=False,
            )

    def _discover(self, recursive: bool) -> dict[str, Path]:
        pattern = "**/*" if recursive else "*"
        found: dict[str, Path] = {}
        for path in sorted(self.directory.glob(pattern)):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() == ".msg":
                raise MailSourceError(
                    "Outlook .msg files are not supported. Export RFC822/MIME .eml "
                    "or use your agent's mail connector; renaming .msg is not conversion.",
                    code="unsupported_format",
                )
            if not any(path.name.lower().endswith(suffix) for suffix in SUFFIXES):
                continue
            # The path relative to the root is a stable, readable id: re-running
            # a scan on the same folder must hit the same rows, not duplicate
            # them.
            source_id = path.relative_to(self.directory).as_posix()
            # The calendar and host extraction also use source_id. Namespace it
            # as well as the stored account so two mailboxes can use the same
            # filenames safely. Preserve IDs for existing unnamed imports.
            if self.account_alias != "local-eml":
                source_id = f"{quote(self.account_alias, safe='')}::{source_id}"
            found[source_id] = path
        return found

    def capabilities(self) -> MailCapabilities:
        return MailCapabilities(
            supports_incremental_history=False,
            supports_full_body=True,
            max_page_size=self.page_size,
            provider="eml_dir",
            # Independent file reads.
            supports_concurrent_fetch=True,
        )

    def search(self, query: str, page_cursor: str | None = None) -> MessagePage:
        keys = list(self._files)
        offset = int(page_cursor) if page_cursor else 0
        chunk = keys[offset : offset + self.page_size]
        next_offset = offset + len(chunk)
        complete = next_offset >= len(keys)
        return MessagePage(
            items=[MessageRef(source_id=key) for key in chunk],
            next_cursor=None if complete else str(next_offset),
            is_complete=complete,
        )

    def fetch(self, message_id: str) -> NormalizedEmail:
        path = self._files.get(message_id)
        if path is None:
            raise MailSourceError(f"unknown message: {message_id}", code="not_found")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise MailSourceError(
                f"could not read {path.name}", code="io_error", retryable=True
            ) from exc
        if raw.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise MailSourceError(
                "This is a binary Outlook/Office file, not RFC822. Export MIME/.eml "
                "without renaming a .msg file.",
                code="unsupported_format",
            )
        return from_rfc822(
            raw,
            source_id=message_id,
            account_alias=self.account_alias,
            # There is no provider receive time for a file on disk. Leaving it
            # absent is correct: a file mtime is not when the mail arrived, and
            # pretending otherwise would corrupt relative-date resolution.
            internal_date=None,
        )
