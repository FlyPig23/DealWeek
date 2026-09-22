"""Gmail read-only adapter.

Thin by design: Google's client library handles OAuth and transport, this file
handles only what the project needs on top of it.

Three details that are easy to get wrong and are handled explicitly here:

* ``users.messages.list`` returns identifiers, not bodies. A page of ids is not
  a page of emails, and the two counts are tracked separately.
* A page can come back with no ``messages`` key while ``nextPageToken`` is still
  set. Pagination therefore stops on the token, never on an empty page.
* ``internalDate`` is when Google received the message, which is not the
  timezone the sender meant by "tomorrow". Both timestamps are kept.

The requested scope is ``gmail.readonly`` and nothing else. Note that this still
grants read access to the whole mailbox at the OAuth layer -- the restriction to
promotional mail is enforced by this application's queries, not by Google.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from ..schemas import MailCapabilities, MessagePage, MessageRef, NormalizedEmail
from .base import AuthRequired, MailSource, MailSourceError
from .normalize import from_rfc822, utc_from_millis

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
MAX_PAGE_SIZE = 500


def build_credentials(
    client_secret_path: str | Path,
    token_path: str | Path,
    *,
    allow_interactive: bool = True,
) -> Any:
    """Load or obtain read-only credentials via the official library.

    The token file is written with owner-only permissions. Preferring the OS
    keychain is a v0.2 item; until then the file mode is the protection.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise MailSourceError(
            "google auth libraries are not installed", code="missing_dependency"
        ) from exc

    token_file = Path(token_path).expanduser()
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            # A project in Testing status expires refresh tokens after 7 days.
            # That is an authorisation problem, not a model or network problem.
            raise AuthRequired(
                "the stored Gmail token could not be refreshed; re-authorise. "
                "If the OAuth app is in Testing status, refresh tokens expire "
                "after 7 days."
            ) from exc
    else:
        if not allow_interactive:
            raise AuthRequired("no valid Gmail token and interactive login is disabled")
        secret_file = Path(client_secret_path).expanduser()
        if not secret_file.exists():
            raise MailSourceError(
                f"OAuth client secret not found at {secret_file}", code="config"
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secret_file), SCOPES)
        creds = flow.run_local_server(port=0)

    token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Created 0600 rather than written-then-chmod'ed: the plain form leaves the
    # refresh token in a world-readable file for the window between the two
    # calls, which is exactly as long as another local process needs.
    descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(creds.to_json())
    token_file.chmod(0o600)  # in case the file already existed, less restricted
    return creds


class GmailApiSource(MailSource):
    def __init__(
        self,
        credentials: Any,
        *,
        account_alias: str = "default",
        page_size: int = 100,
        service: Any | None = None,
    ) -> None:
        self.account_alias = account_alias
        self.page_size = min(page_size, MAX_PAGE_SIZE)
        if service is not None:
            self._service = service
        else:
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise MailSourceError(
                    "google-api-python-client is not installed", code="missing_dependency"
                ) from exc
            self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def capabilities(self) -> MailCapabilities:
        return MailCapabilities(
            supports_incremental_history=True,
            supports_full_body=True,
            max_page_size=self.page_size,
            provider="gmail_api",
            # googleapiclient's service object wraps a single httplib2.Http,
            # which is not thread-safe: sharing it across threads interleaves
            # responses rather than raising. Enabling this needs a per-thread
            # service (or an `http=` passed per request) and a test that proves
            # it, so until then fetches here are serialised.
            supports_concurrent_fetch=False,
        )

    def _users(self) -> Any:
        return self._service.users()

    def search(self, query: str, page_cursor: str | None = None) -> MessagePage:
        try:
            request = self._users().messages().list(
                userId="me",
                q=query,
                maxResults=self.page_size,
                pageToken=page_cursor,
                includeSpamTrash=False,
            )
            response = request.execute()
        except Exception as exc:
            raise self._translate(exc) from exc

        raw_items = response.get("messages") or []
        next_token = response.get("nextPageToken")
        return MessagePage(
            items=[
                MessageRef(source_id=item["id"], thread_id=item.get("threadId"))
                for item in raw_items
            ],
            next_cursor=next_token,
            # Complete only when Google itself says there is no further page. An
            # empty `messages` list mid-pagination does not mean the end.
            is_complete=next_token is None,
        )

    def fetch(self, message_id: str) -> NormalizedEmail:
        try:
            # `raw` gives the full RFC822 message, so nested MIME, charsets and
            # footnotes are handled by one well-tested parser instead of by
            # walking Google's payload tree.
            response = (
                self._users()
                .messages()
                .get(userId="me", id=message_id, format="raw")
                .execute()
            )
        except Exception as exc:
            raise self._translate(exc) from exc

        raw = response.get("raw")
        if not raw:
            raise MailSourceError(
                f"message {message_id} returned no raw body", code="empty_body"
            )
        payload = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        return from_rfc822(
            payload,
            source_id=message_id,
            thread_id=response.get("threadId"),
            account_alias=self.account_alias,
            internal_date=utc_from_millis(response.get("internalDate")),
        )

    def changes(self, sync_cursor: str | None) -> MessagePage:
        """Incremental sync via the history API.

        A 404 means the cursor is older than Gmail's retained history. That is
        recoverable -- the caller must fall back to a full re-scan of the
        configured range -- so it is signalled distinctly.
        """
        if not sync_cursor:
            raise MailSourceError("a history cursor is required", code="no_cursor")
        try:
            response = (
                self._users()
                .history()
                .list(userId="me", startHistoryId=sync_cursor, historyTypes=["messageAdded"])
                .execute()
            )
        except Exception as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                raise MailSourceError(
                    "history cursor expired; a full re-scan is required",
                    code="history_expired",
                    retryable=False,
                ) from exc
            raise self._translate(exc) from exc

        items: list[MessageRef] = []
        for record in response.get("history", []):
            for added in record.get("messagesAdded", []):
                message = added.get("message", {})
                if message.get("id"):
                    items.append(
                        MessageRef(source_id=message["id"], thread_id=message.get("threadId"))
                    )
        next_token = response.get("nextPageToken")
        return MessagePage(items=items, next_cursor=next_token, is_complete=next_token is None)

    def current_history_id(self) -> str | None:
        try:
            profile = self._users().getProfile(userId="me").execute()
        except Exception as exc:
            raise self._translate(exc) from exc
        value = profile.get("historyId")
        return str(value) if value is not None else None

    #: Gmail returns 403 for quota and rate limits as well as for permission
    #: problems. They need opposite handling -- back off and retry, versus stop
    #: and ask the user to re-authorise -- so they are told apart by reason.
    _QUOTA_REASONS = (
        "ratelimitexceeded",
        "userratelimitexceeded",
        "quotaexceeded",
        "dailylimitexceeded",
        "backenderror",
    )

    @classmethod
    def _translate(cls, exc: Exception) -> MailSourceError:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status == 403:
            detail = str(exc).lower()
            if any(reason in detail for reason in cls._QUOTA_REASONS):
                # Treating this as "authorisation required" aborted the scan and
                # told the user to re-authorise over what is just throttling.
                return MailSourceError(
                    "Gmail rate limit or quota exceeded (HTTP 403)",
                    code="http_403_quota",
                    retryable=True,
                )
            return AuthRequired("Gmail refused the request (HTTP 403): check the granted scope")
        if status == 401:
            return AuthRequired("Gmail rejected the credentials (HTTP 401)")
        if status == 429 or (isinstance(status, int) and 500 <= status < 600):
            return MailSourceError(
                f"Gmail returned HTTP {status}", code=f"http_{status}", retryable=True
            )
        return MailSourceError(f"Gmail request failed: {type(exc).__name__}", code="gmail_error")
