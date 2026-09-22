"""Local HTTP API and dashboard.

These are MealDeals' own endpoints. They are not Gmail's or TypeSafe's, and none
of them proxies a provider request.

Every route reads through :class:`MealDealsService`, so the web UI and the CLI
cannot disagree about what "this week" means. No route accepts a file path, and
no response carries a credential.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..mcp_server import _offer_view, _plan_view
from ..reporting import render as reporting
from ..schemas import UserStatus


def register(app: FastAPI) -> None:
    def service():  # type: ignore[no-untyped-def]
        return app.state.service

    @app.get("/api/status")
    def status() -> dict:
        payload = service().status()
        payload["csrf_token"] = app.state.csrf_token
        return payload

    @app.get("/api/offers")
    def offers(status: str = "all", limit: int = 100) -> dict:
        items = service().list_food_offers()
        if status == "actionable":
            items = [o for o in items if o.actionable]
        elif status == "needs_confirmation":
            items = [o for o in items if not o.actionable]
        capped = max(1, min(limit, 500))
        return {"total": len(items), "offers": [_offer_view(o) for o in items[:capped]]}

    @app.get("/api/offers/{offer_id}")
    def offer_detail(offer_id: str) -> Any:
        found = service().get_offer(offer_id)
        if found is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        view = _offer_view(found)
        view["evidence"] = [
            {"field": e.field_path, "quote": e.quote, "verified": e.verified}
            for e in found.evidence
        ]
        view["validation_notes"] = found.validation_notes
        # The revision a PATCH should send back as `expected_revision`. Without
        # it on a read, the optimistic-concurrency guard had no way to be used.
        view["user_state"] = json.loads(service().get_user_state(offer_id).model_dump_json())
        return view

    @app.get("/api/duplicates")
    def duplicates() -> dict:
        """Pairs the deduplicator refused to merge, so the user can settle them."""
        return {"pairs": service().suspected_duplicates()}

    @app.post("/api/duplicates/dismiss")
    async def dismiss_duplicate(request: Request) -> Any:
        body = await request.json()
        left, right = body.get("left"), body.get("right")
        if not left or not right:
            return JSONResponse({"error": "left and right are required"}, status_code=400)
        service().dismiss_duplicate(str(left), str(right))
        return {"dismissed": [left, right]}

    @app.patch("/api/offers/{offer_id}/user-state")
    async def set_state(offer_id: str, request: Request) -> Any:
        body = await request.json()
        if service().get_offer(offer_id) is None:
            # Otherwise a typo creates a row of user state attached to no offer,
            # which nothing ever reads and nothing ever cleans up.
            return JSONResponse({"error": "not_found"}, status_code=404)
        # PATCH means "change these fields". Defaulting an absent status to
        # unused_or_unknown made a note-only edit silently clear the user's
        # "used" flag -- the one piece of state a re-sync is promised never to
        # touch, destroyed by the API meant to maintain it.
        status_value: UserStatus | None = None
        if "status" in body:
            try:
                status_value = UserStatus(body["status"])
            except ValueError:
                return JSONResponse({"error": "invalid_status"}, status_code=400)
        try:
            state = service().set_user_state(
                offer_id,
                status=status_value,
                planned_date=body.get("planned_date"),
                note=body.get("note"),
                eligibility_overrides=body.get("eligibility_overrides"),
                expected_revision=body.get("expected_revision"),
            )
        except ValueError as exc:
            # Optimistic-concurrency failure: another tab changed it first.
            return JSONResponse({"error": "conflict", "message": str(exc)}, status_code=409)
        return json.loads(state.model_dump_json())

    @app.post("/api/plans")
    def build_plan() -> dict:
        svc = service()
        return _plan_view(svc.build_meal_plan(), svc.settings.app.report.language)

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        svc = service()
        plan = svc.build_meal_plan()
        offers = svc.list_food_offers()
        # Reuse the report renderer: it already escapes everything and emits no
        # remote references, which is exactly what the CSP requires.
        html = reporting.render(
            plan, offers, "html", language=svc.settings.app.report.language
        )
        return HTMLResponse(html)
