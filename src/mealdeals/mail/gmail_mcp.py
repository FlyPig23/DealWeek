"""Optional Gmail-over-MCP source.

Not implemented in v0.1, and that is a considered decision rather than a gap.

For this workload -- a batch scanner walking 90 days of promotional mail -- an
MCP mail server sits between the application and the same Gmail REST API,
adding a process, a protocol hop and an authorisation story, while making the
three things this pipeline depends on *less* certain:

* exhaustive pagination (some servers cap results or hide the cursor),
* complete bodies (full text may arrive as a file reference rather than inline),
* stable provider ids and both timestamps.

MCP earns its place where a *host* needs to call capabilities it did not compile
in. That is the outbound direction, and this project takes it: see
``mealdeals.mcp_server``, which exposes MealDeals itself over MCP so Claude,
an IDE or any other host can use it without a bespoke integration.

This module therefore stays a contract plus an acceptance checklist. Implement
it only when a concrete, already-deployed MCP server needs to be reused, and
only after every check below passes. If any check fails, fall back to
``GmailApiSource``; do not paper over a gap with a prompt asking the model to
behave.
"""

from __future__ import annotations

from ..schemas import MailCapabilities, MessagePage, NormalizedEmail
from .base import MailSource, MailSourceError

# Each entry must be demonstrated against the actual server and version in use,
# with the evidence recorded, before this source may replace GmailApiSource.
ACCEPTANCE_CHECKS: tuple[tuple[str, str], ...] = (
    ("tool_permissions", "Client exposes search/read tools only; OAuth is read-only."),
    ("pagination", "A cursor is returned and can be followed to a definite end."),
    ("full_body", "Terms in a long email's footer arrive complete, not truncated."),
    ("payload_form", "Inline text, or a file/URL from a trusted server, is handled."),
    ("identity", "Stable message id, thread id, send time and source are recoverable."),
    ("programmatic", "The client can be driven from code without an LLM in the loop."),
    ("reauth", "Token expiry surfaces as 'needs authorisation', not an empty list."),
)


class GmailMcpSource(MailSource):
    """Placeholder that fails loudly rather than silently returning nothing."""

    def __init__(self, server_command: str | None = None, **_: object) -> None:
        self.server_command = server_command

    def capabilities(self) -> MailCapabilities:
        return MailCapabilities(
            supports_incremental_history=False,
            supports_full_body=False,
            max_page_size=0,
            provider="gmail_mcp",
        )

    def _unavailable(self) -> MailSourceError:
        checks = "\n".join(f"  - {name}: {detail}" for name, detail in ACCEPTANCE_CHECKS)
        return MailSourceError(
            "GmailMcpSource is not implemented in v0.1. Use mail.provider=gmail_api.\n"
            "Before implementing it, verify against your chosen server:\n" + checks,
            code="not_implemented",
        )

    def search(self, query: str, page_cursor: str | None = None) -> MessagePage:
        raise self._unavailable()

    def fetch(self, message_id: str) -> NormalizedEmail:
        raise self._unavailable()
