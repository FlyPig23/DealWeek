"""MealDeals as an MCP server.

This is the MCP direction that earns its keep in this project.

Using MCP to *read Gmail* would put a protocol hop between this application and
the same Gmail REST API, while weakening the three guarantees the pipeline is
built on: exhaustive pagination, complete bodies, and stable ids. See
``mail/gmail_mcp.py``.

Using MCP to *expose MealDeals* is the opposite trade. Any host -- Claude, an
IDE, another agent -- gets the four business capabilities without a bespoke
integration, and gets them through the same service layer the CLI uses, so the
rules cannot drift. It also replaces the planned SKILL.md-shells-out-to-CLI
approach with something that carries typed arguments and typed errors.

Boundaries, which the host cannot widen:

* Read and plan freely; the only mutation exposed is the user's own offer state.
* No tool returns an API key, a token or a file path.
* No tool accepts a file path, a URL or a query to execute.
* ``sync_promotions`` spends money, so it is capped per call and refuses to run
  until cloud-processing consent has been granted in the local config.
"""

from __future__ import annotations

import json
from typing import Any

from .clock import SystemClock
from .config import Settings
from .planning.explanations import explain_all
from .reporting import render as reporting
from .schemas import UserStatus
from .service import MealDealsService

SERVER_NAME = "mealdeals"
SERVER_INSTRUCTIONS = """MealDeals turns promotional email into a checkable weekly savings plan.

Offers carry explicit uncertainty. `time_status`, `eligibility_status` and
`parse_status` are not decoration: an offer in `needs_confirmation` has not been
verified, and presenting it as usable would be wrong. `within_stated_window`
means the email's own dates have not passed -- it is not a merchant confirmation
that the offer will be honoured.

Never tell the user an offer saves them money unless `cost.computable` is true.
Report unknowns as unknowns.
"""

# One call must not be able to drain a budget.
MAX_MESSAGES_PER_CALL = 200


def _offer_view(offer: Any) -> dict:
    """Compact, honest projection of an offer for a model to read."""
    from .planning.costs import face_value

    ends = offer.temporal.ends.date
    return {
        "offer_id": offer.offer_id,
        "merchant": offer.merchant,
        "title": offer.title,
        "category": str(offer.food_category),
        "benefit": face_value(offer.benefit),
        "ends": ends.isoformat() if ends else None,
        "ends_stated": ends is not None,
        "claim_deadline": (
            offer.temporal.claim_deadline.date.isoformat()
            if offer.temporal.claim_deadline.date
            else None
        ),
        "time_status": str(offer.time_status),
        "eligibility_status": str(offer.eligibility_status),
        "parse_status": str(offer.parse_status),
        "evidence_verified": offer.evidence_verified,
        "actionable": offer.actionable,
        "unresolved": offer.unresolved_fields,
        "conflicts": offer.conflicts,
        "sources": offer.source_message_ids,
    }


def _plan_view(plan: Any, language: str) -> dict:
    def bucket(items: list) -> list[dict]:
        return [
            {
                "offer_id": item.offer_id,
                "merchant": item.merchant,
                "title": item.title,
                "slot_date": item.slot_date.isoformat() if item.slot_date else None,
                "reasons": explain_all(item.reason_codes, language),
                "reason_codes": item.reason_codes,
                "unknowns": item.unknowns,
                "cost_computable": item.cost.computable,
                "cost_note": item.cost.note,
                "deadline": item.deadline_note,
            }
            for item in items
        ]

    return {
        "plan_id": plan.plan_id,
        "as_of": plan.as_of.isoformat(),
        "timezone": plan.timezone,
        "this_week": bucket(plan.this_week),
        "next_week": bucket(plan.next_week),
        "this_month": bucket(plan.this_month),
        "needs_confirmation": bucket(plan.needs_confirmation),
        "coverage": {
            "summary": reporting.coverage_sentence(plan.coverage),
            "is_partial": plan.coverage.is_partial,
            "search_exhaustive": plan.coverage.search_exhaustive,
            "unparsed_visuals": plan.coverage.unparsed_visuals,
            "last_sync_at": (
                plan.coverage.last_sync_at.isoformat() if plan.coverage.last_sync_at else None
            ),
        },
        "notes": plan.notes,
    }


def _server_class():
    """Return the MCP server class for whichever SDK major is installed.

    The SDK renamed ``FastMCP`` to ``MCPServer`` in 2.0. The constructor, the
    ``tool()`` decorator and ``run()`` are compatible across both, so supporting
    each is an import shim rather than a fork. Pinning to one major would make
    this project uninstallable alongside the other.
    """
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2.0

        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

        return FastMCP
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "the MCP server needs the 'mcp' package: pip install 'mealdeals[mcp]'"
        ) from exc


def build_server(config: str | None = None, offline: bool = False):
    """Construct the MCP server. Imported lazily so `mcp` stays optional."""
    server_class = _server_class()

    settings = Settings.build(config, offline=offline)
    service = MealDealsService(settings, SystemClock(settings.app.report.timezone))
    language = settings.app.report.language

    server = server_class(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    @server.tool()
    def get_status() -> dict:
        """Configuration and last-run status. Contains no credentials."""
        return service.status()

    @server.tool()
    def sync_promotions(mode: str = "jev-observe", max_messages: int = 50) -> dict:
        """Read new promotional email and extract offers.

        This spends money on model calls. It refuses to run until cloud
        processing has been consented to in the local config, and it is capped
        per call.

        Args:
            mode: llm-only, jev-observe or jev-gate.
            max_messages: hard cap on messages processed this call (1-200).
        """
        if mode not in ("llm-only", "jev-observe", "jev-gate"):
            return {"error": "invalid_mode", "allowed": ["llm-only", "jev-observe", "jev-gate"]}
        if not settings.offline and not settings.app.privacy.cloud_processing_consent:
            return {
                "error": "consent_required",
                "message": (
                    "Sending email text to a cloud model has not been consented to. "
                    "Set privacy.cloud_processing_consent in config.yaml locally. "
                    "This cannot be granted through this tool."
                ),
            }
        capped = max(1, min(int(max_messages), MAX_MESSAGES_PER_CALL))
        result = service.sync_promotions(mode=mode, max_messages=capped)
        return {
            "run_id": result.run_id,
            "status": result.status,
            "coverage": reporting.coverage_sentence(result.coverage),
            "is_partial": result.coverage.is_partial,
            "offers_found": len(result.offers),
            "parked": len(result.stages.parked),
            "filtered": len(result.stages.rejected),
            "errors": result.stages.errors,
            "estimated_cost_usd": result.cost_usd if result.cost_known else None,
            "cost_known": result.cost_known,
        }

    @server.tool()
    def list_food_offers(
        status: str = "all", merchant: str | None = None, limit: int = 50
    ) -> dict:
        """List stored offers.

        Args:
            status: all, actionable, or needs_confirmation.
            merchant: optional case-insensitive substring filter.
            limit: maximum offers to return (1-200).
        """
        offers = service.list_food_offers()
        if merchant:
            needle = merchant.strip().lower()
            offers = [o for o in offers if needle in o.merchant.lower()]
        if status == "actionable":
            offers = [o for o in offers if o.actionable]
        elif status == "needs_confirmation":
            offers = [o for o in offers if not o.actionable]
        elif status != "all":
            return {"error": "invalid_status", "allowed": ["all", "actionable", "needs_confirmation"]}

        capped = max(1, min(int(limit), 200))
        return {
            "total": len(offers),
            "returned": min(len(offers), capped),
            "offers": [_offer_view(o) for o in offers[:capped]],
        }

    @server.tool()
    def get_offer(offer_id: str) -> dict:
        """Full detail for one offer, including verified evidence quotes."""
        offer = service.get_offer(offer_id)
        if offer is None:
            return {"error": "not_found", "offer_id": offer_id}
        view = _offer_view(offer)
        view["evidence"] = [
            {"field": e.field_path, "quote": e.quote, "verified": e.verified}
            for e in offer.evidence
        ]
        view["validation_notes"] = offer.validation_notes
        return view

    @server.tool()
    def build_meal_plan() -> dict:
        """Build this week / next week / this month buckets from stored offers.

        Runs entirely locally: no model call, no network, no cost.
        """
        return _plan_view(service.build_meal_plan(), language)

    @server.tool()
    def render_report(output_format: str = "markdown") -> str:
        """Render the current plan as markdown or json text.

        Returns the report content itself. It does not write a file, so this
        tool cannot be pointed at a path on disk.
        """
        if output_format not in ("markdown", "json"):
            return json.dumps(
                {"error": "invalid_format", "allowed": ["markdown", "json"]}, ensure_ascii=False
            )
        plan = service.build_meal_plan()
        offers = service.list_food_offers()
        return reporting.render(plan, offers, output_format, language=language)

    @server.tool()
    def set_offer_state(
        offer_id: str, status: str, planned_date: str | None = None, note: str | None = None
    ) -> dict:
        """Record the user's decision about an offer.

        The only write this server exposes. It touches user state only -- it
        cannot alter extracted offer facts.

        Args:
            offer_id: id from list_food_offers.
            status: used, dismissed, saved, planned or unused_or_unknown.
            planned_date: YYYY-MM-DD, required in practice for 'planned'.
            note: free-text note to keep with the offer.
        """
        try:
            parsed_status = UserStatus(status)
        except ValueError:
            return {"error": "invalid_status", "allowed": [str(s) for s in UserStatus]}
        if service.get_offer(offer_id) is None:
            return {"error": "not_found", "offer_id": offer_id}
        try:
            state = service.set_user_state(
                offer_id, status=parsed_status, planned_date=planned_date, note=note
            )
        except ValueError as exc:
            return {"error": "invalid_argument", "message": str(exc)}
        return {
            "offer_id": state.offer_id,
            "status": str(state.status),
            "planned_date": state.planned_date.isoformat() if state.planned_date else None,
            "note": state.note,
        }

    return server


def run_stdio(config: str | None = None, offline: bool = False) -> None:  # pragma: no cover
    build_server(config=config, offline=offline).run()


if __name__ == "__main__":  # pragma: no cover
    run_stdio()
