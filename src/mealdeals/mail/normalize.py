"""MIME and HTML normalization.

Two separate jobs that must not be confused:

1. Producing text for the model -- footnotes and terms kept, navigation chrome
   and tracking pixels dropped.
2. Producing markup for the browser -- which this module never does. The web
   layer renders escaped text only; raw promotional HTML is never re-emitted.

JSON-LD is parsed as *data*. Scripts are removed before anything else happens,
and no JavaScript is ever executed.
"""

from __future__ import annotations

import base64
import email
import email.policy
import json
import re
from datetime import UTC, datetime
from email.message import Message as PyMessage
from email.utils import parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup

from ..schemas import EmailAttachmentHint, NormalizedEmail, ParseStatus

# Elements that carry no offer terms.
_DROP_TAGS = ("script", "style", "noscript", "iframe", "object", "embed", "svg")
# Wrappers that are usually navigation; dropped only when they have no prices.
_CHROME_HINTS = ("unsubscribe", "view in browser", "manage preferences", "privacy policy")
_MONEY = re.compile(r"[$£€¥]\s?\d|(\d+\s?%)|\bfree\b|\bbogo\b", re.IGNORECASE)
_MAX_TEXT = 200_000
# Attachments that can carry the whole offer and that v0.1 cannot read.
_DOCUMENT_TYPES = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/rtf",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)


def decode_b64url(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def html_to_text(html: str) -> tuple[str, list[dict[str, Any]], bool]:
    """Extract readable text, JSON-LD blocks, and whether images carry the message.

    Returns ``(text, structured_markup, image_heavy)``.
    """
    soup = BeautifulSoup(html, "html.parser")

    structured: list[dict[str, Any]] = []
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = node.string or node.get_text() or ""
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, list):
            structured.extend(item for item in parsed if isinstance(item, dict))
        elif isinstance(parsed, dict):
            structured.append(parsed)

    images = soup.find_all("img")
    image_alt_text: list[str] = []
    for node in images:
        alt = (node.get("alt") or "").strip()
        if alt:
            image_alt_text.append(alt)

    for tag_name in _DROP_TAGS:
        for node in soup.find_all(tag_name):
            node.decompose()

    # Keep footnotes and small print: that is where the real terms live.
    for node in soup.find_all(["a"]):
        label = node.get_text(" ", strip=True).lower()
        if any(hint in label for hint in _CHROME_HINTS) and not _MONEY.search(label):
            node.decompose()

    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # If there are pictures and the readable copy carries no benefit signal, the
    # offer is probably inside one of them. Flag it rather than concluding the
    # email has no offer.
    #
    # Keyed on content rather than length. A character-count threshold got this
    # wrong in both directions: a one-image promo slipped through because it
    # took two images to trigger, while a perfectly readable "Free pastry with
    # any drink" was pushed into the review queue for being short.
    #
    # Alt text is deliberately excluded from this test. "Free scoop" on a banner
    # tells us an offer exists; it is not the terms, which are in the picture
    # next to it whose alt attribute is empty.
    image_heavy = bool(images) and not _MONEY.search(text)

    if image_alt_text:
        text = text + "\n\n[image alt text] " + " | ".join(image_alt_text)

    return text, structured, image_heavy


_TERM_TOKEN = re.compile(
    r"[$£€¥]\s?\d"                                  # any price
    r"|\d+\s?%"                                     # any percentage
    r"|\b\d{4}-\d{2}-\d{2}\b"                       # ISO date
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}\s*[/月]\s*\d{1,2}\b"
    r"|\b(?:expires?|ends?|valid|through|until|redeem|claim|minimum|members?|"
    r"participating|limit|code)\b"
    r"|截止|有效|会员|限|优惠码|满",
    re.IGNORECASE,
)


def _adds_material(candidate: str, chosen: str) -> bool:
    """Whether ``candidate`` holds offer terms that ``chosen`` does not.

    Compared on the token level rather than by length: a second MIME alternative
    is worth keeping only when it mentions a price, a date or a condition the
    first one is missing. Boilerplate that merely repeats the headline is not.
    """
    if not candidate.strip():
        return False
    present = {m.group(0).lower() for m in _TERM_TOKEN.finditer(chosen)}
    return any(m.group(0).lower() not in present for m in _TERM_TOKEN.finditer(candidate))


def _walk_parts(message: PyMessage) -> list[tuple[PyMessage, str]]:
    found: list[tuple[PyMessage, str]] = []

    def visit(part: PyMessage, path: str) -> None:
        if part.is_multipart():
            for index, child in enumerate(part.get_payload()):  # type: ignore[arg-type]
                visit(child, f"{path}.{index}" if path else str(index))
        else:
            found.append((part, path or "0"))

    visit(message, "")
    return found


def from_rfc822(
    raw: bytes,
    *,
    source_id: str,
    thread_id: str | None = None,
    account_alias: str = "default",
    internal_date: datetime | None = None,
) -> NormalizedEmail:
    """Normalize a full RFC822 message."""
    # policy.default decodes RFC2047-encoded headers *and* the raw 8-bit UTF-8
    # headers that non-compliant senders emit. compat32 mangles both into
    # replacement characters, which silently corrupts non-Latin merchant names.
    try:
        parsed = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:  # malformed message: fall back rather than lose it
        parsed = email.message_from_bytes(raw)
    parts = _walk_parts(parsed)

    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    hints: list[EmailAttachmentHint] = []
    notes: list[str] = []
    missing_body = False

    unreadable_attachment = False
    for part, path in parts:
        content_type = part.get_content_type()
        if content_type.startswith("image/") or content_type in _DOCUMENT_TYPES:
            hints.append(
                EmailAttachmentHint(
                    part_id=path,
                    mime_type=content_type,
                    filename=part.get_filename(),
                    is_image=content_type.startswith("image/"),
                )
            )
            # A PDF or Word flyer is never decoration: merchants attach them
            # precisely because the offer is in them. Silently skipping the part
            # and then reporting parse_status=complete claimed the terms had
            # been read when nothing had even opened the file.
            if content_type in _DOCUMENT_TYPES:
                unreadable_attachment = True
                notes.append(
                    f"part {path} is a {content_type} attachment; v0.1 does not read "
                    "documents, so any terms inside it are unparsed"
                )
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            # Gmail can hand back a part whose body must be fetched separately.
            missing_body = True
            notes.append(f"part {path} ({content_type}) had no inline body")
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if content_type == "text/plain":
            plain_chunks.append(text)
        elif content_type == "text/html":
            html_chunks.append(text)
        elif content_type.startswith("text/"):
            # An unusual text subtype is still text; better in the body than lost.
            plain_chunks.append(text)

    structured: list[dict[str, Any]] = []
    image_heavy = False
    html_text = ""
    if html_chunks:
        pieces = []
        for chunk in html_chunks:
            text, markup, heavy = html_to_text(chunk)
            pieces.append(text)
            structured.extend(markup)
            image_heavy = image_heavy or heavy
        html_text = "\n\n".join(pieces)

    plain_text = "\n\n".join(chunk.strip() for chunk in plain_chunks if chunk.strip())

    # Prefer whichever alternative carries more, then top the other one up.
    #
    # Choosing purely on a length threshold loses offers: a plain part padded
    # with an address block and an unsubscribe line easily clears any threshold
    # while the HTML part holds the entire fine print -- expiry, claim deadline,
    # minimum spend, membership. Dropping that silently and still reporting
    # `body_complete` is the exact failure this pipeline exists to avoid, so the
    # two parts are reconciled rather than one being picked.
    body, extra = (
        (html_text, plain_text) if len(html_text) > len(plain_text) else (plain_text, html_text)
    )
    if extra and _adds_material(extra, body):
        body = f"{body}\n\n[other MIME alternative]\n{extra}"
        notes.append("plain-text and HTML alternatives differ; both were kept")
    elif html_text and body is html_text and plain_text:
        notes.append("plain-text part was shorter; used the HTML body")

    truncated = False
    if len(body) > _MAX_TEXT:
        body = body[:_MAX_TEXT]
        truncated = True
        notes.append("body exceeded the size limit and was truncated")

    sender_date: datetime | None = None
    date_header = parsed.get("Date")
    if date_header is not None:
        # Under policy.default this is a DateHeader that already carries a
        # datetime; under compat32 it is a plain string.
        sender_date = getattr(date_header, "datetime", None)
        if sender_date is None:
            try:
                sender_date = parsedate_to_datetime(str(date_header))
            except (TypeError, ValueError):
                notes.append("unparseable Date header")

    provenance = "unknown"
    if sender_date and internal_date:
        provenance = "both"
    elif sender_date:
        provenance = "header"
    elif internal_date:
        provenance = "internal"

    subject = str(parsed.get("Subject") or "")
    sender = str(parsed.get("From") or "")

    if unreadable_attachment or (
        any(hint.is_image for hint in hints)
        and not _MONEY.search(body.split("[image alt text]")[0])
    ):
        image_heavy = True

    body_complete = not missing_body and bool(body.strip())
    if image_heavy:
        parse_status = ParseStatus.NEEDS_VISUAL
        notes.append("mostly images with little text; terms may be unparsed")
    elif truncated or not body_complete:
        parse_status = ParseStatus.PARTIAL
    else:
        parse_status = ParseStatus.COMPLETE

    return NormalizedEmail(
        source_id=source_id,
        thread_id=thread_id,
        account_alias=account_alias,
        subject=subject,
        sender=sender,
        sender_date=sender_date,
        received_date=internal_date,
        date_provenance=provenance,  # type: ignore[arg-type]
        normalized_text=f"Subject: {subject}\n\n{body}".strip(),
        structured_markup=structured,
        parts=hints,
        body_complete=body_complete,
        has_unparsed_visuals=image_heavy,
        truncated=truncated,
        parse_status=parse_status,
        parse_notes=notes,
    )


def discount_offers(markup: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pull Google's promotion annotations out of parsed JSON-LD.

    These are read first and then *cross-checked* against the body; where they
    disagree, the conflict is preserved rather than resolved in favour of the
    nicer number.
    """
    found: list[dict[str, Any]] = []
    for block in markup:
        node_type = block.get("@type")
        if node_type in ("DiscountOffer", "PromotionCard"):
            found.append(block)
        for value in block.values():
            if isinstance(value, dict) and value.get("@type") == "DiscountOffer":
                found.append(value)
            elif isinstance(value, list):
                found.extend(
                    item
                    for item in value
                    if isinstance(item, dict) and item.get("@type") == "DiscountOffer"
                )
    return found


def utc_from_millis(millis: str | int | None) -> datetime | None:
    """Gmail's ``internalDate``: when Google received it, not the sender's clock."""
    if millis is None:
        return None
    try:
        return datetime.fromtimestamp(int(millis) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return None
