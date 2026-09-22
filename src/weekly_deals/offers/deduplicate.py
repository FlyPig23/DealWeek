"""Offer deduplication.

Merging is conservative by design. Collapsing two different promotions into one
card loses a real offer and can wipe the user's "already used" flag; leaving two
near-duplicates on screen costs the user three seconds. So:

* identical campaign identity -> merge, appending the source.
* same merchant + same benefit shape but a *different* end date, channel or
  eligibility -> keep both, and record them as a suspected pair for review.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..schemas import Evidence, ValidatedOffer


@dataclass
class DedupResult:
    offers: list[ValidatedOffer] = field(default_factory=list)
    suspected_duplicates: list[tuple[str, str]] = field(default_factory=list)
    merged_count: int = 0


def _similarity_key(offer: ValidatedOffer) -> tuple:
    """Loose key: same merchant and same benefit shape, ignoring dates."""
    benefit = offer.benefit
    return (
        offer.merchant.strip().lower(),
        str(benefit.kind),
        benefit.amount_off.minor if benefit.amount_off else None,
        benefit.percent_off,
        benefit.fixed_price.minor if benefit.fixed_price else None,
    )


def _merge_evidence(left: list[Evidence], right: list[Evidence]) -> list[Evidence]:
    seen: dict[tuple[str, str, str], Evidence] = {}
    for item in [*left, *right]:
        key = (item.field_path, item.message_id, item.quote)
        # A verified quote beats an unverified one for the same field.
        if key not in seen or (item.verified and not seen[key].verified):
            seen[key] = item
    return list(seen.values())


def merge_pair(primary: ValidatedOffer, incoming: ValidatedOffer) -> ValidatedOffer:
    """Fold a repeat sighting into the offer we already know about."""
    sources = list(dict.fromkeys([*primary.source_message_ids, *incoming.source_message_ids]))
    return primary.model_copy(
        update={
            "source_message_ids": sources,
            "evidence": _merge_evidence(primary.evidence, incoming.evidence),
            "unresolved_fields": sorted(
                set(primary.unresolved_fields) | set(incoming.unresolved_fields)
            ),
            "conflicts": sorted(set(primary.conflicts) | set(incoming.conflicts)),
            "validation_notes": list(
                dict.fromkeys([*primary.validation_notes, *incoming.validation_notes])
            ),
        }
    )


def deduplicate(offers: list[ValidatedOffer]) -> DedupResult:
    """Collapse exact campaign matches; flag near matches instead of guessing."""
    by_id: dict[str, ValidatedOffer] = {}
    merged = 0

    for offer in offers:
        existing = by_id.get(offer.offer_id)
        if existing is None:
            by_id[offer.offer_id] = offer
        else:
            by_id[offer.offer_id] = merge_pair(existing, offer)
            merged += 1

    suspected: list[tuple[str, str]] = []
    buckets: dict[tuple, list[ValidatedOffer]] = {}
    for offer in by_id.values():
        buckets.setdefault(_similarity_key(offer), []).append(offer)

    for group in buckets.values():
        if len(group) < 2:
            continue
        # Mutually exclusive alternatives from one email are not duplicates.
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                if (
                    left.alternative_group
                    and left.alternative_group == right.alternative_group
                ):
                    continue
                suspected.append(tuple(sorted((left.offer_id, right.offer_id))))  # type: ignore[arg-type]

    ordered = sorted(
        by_id.values(),
        key=lambda item: (
            item.temporal.ends.date is None,
            item.temporal.ends.date or item.merchant,
            item.merchant,
        ),
    )
    return DedupResult(
        offers=ordered,
        suspected_duplicates=sorted(set(suspected)),
        merged_count=merged,
    )
