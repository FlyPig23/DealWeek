"""MCP server contract.

Asserts the boundary, not just that the tools exist: the host can read and plan,
can record the user's own decisions, and can do nothing else. In particular it
cannot grant itself consent to spend money on cloud model calls.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp", reason="the optional 'mcp' extra is not installed")

from weekly_deals.clock import FrozenClock
from weekly_deals.mcp_server import _offer_view, _plan_view, build_server
from weekly_deals.schemas import UserStatus
from weekly_deals.service import WeeklyDealsService

from ..conftest import REFERENCE


@pytest.fixture
def server(monkeypatch):
    """A server wired to an offline, in-memory service."""
    clock = FrozenClock(REFERENCE, "America/Chicago")
    service = WeeklyDealsService.offline(clock=clock)
    service.sync_promotions(mode="llm-only")
    monkeypatch.setattr(
        "weekly_deals.mcp_server.WeeklyDealsService", lambda *a, **k: service
    )
    built = build_server(offline=True)
    built._service = service  # type: ignore[attr-defined]
    return built


async def call(server, name: str, **arguments):
    """Invoke a tool and return its payload.

    The SDK's return shape has changed across versions: a list of content
    blocks, a (blocks, structured) tuple, and in 2.x a ``CallToolResult``.
    Normalise all three so the assertions below are about this project's
    contract rather than the SDK's wire format.
    """
    result = await server.call_tool(name, arguments)

    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)

    if isinstance(result, tuple):
        blocks = result[0]
        if len(result) > 1 and isinstance(result[1], dict):
            return result[1].get("result", result[1])
    else:
        blocks = getattr(result, "content", result)

    text = "".join(getattr(block, "text", "") for block in blocks)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class TestToolSurface:
    @pytest.mark.asyncio
    async def test_exposes_only_the_intended_tools(self, server):
        names = {tool.name for tool in await server.list_tools()}
        assert names == {
            "get_status",
            "sync_promotions",
            "list_food_offers",
            "get_offer",
            "build_meal_plan",
            "render_report",
            "set_offer_state",
        }

    @pytest.mark.asyncio
    async def test_no_tool_accepts_a_path_or_url(self, server):
        """Nothing here may be pointed at the filesystem or the network."""
        for tool in await server.list_tools():
            # The SDK renamed this attribute in 2.0; accept either spelling.
            schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
            properties = (schema or {}).get("properties", {})
            for argument in properties:
                assert not any(
                    token in argument.lower() for token in ("path", "file", "url", "query", "sql")
                ), f"{tool.name}.{argument} looks like an escape hatch"

    @pytest.mark.asyncio
    async def test_instructions_warn_about_uncertainty(self, server):
        assert "needs_confirmation" in (server.instructions or "")


class TestReadTools:
    @pytest.mark.asyncio
    async def test_status_never_returns_a_credential(self, server):
        payload = await call(server, "get_status")
        serialised = json.dumps(payload).lower()
        for secret in ("api_key", "token", "secret", "bearer", "password"):
            assert secret not in serialised

    @pytest.mark.asyncio
    async def test_list_returns_offers_with_their_uncertainty(self, server):
        payload = await call(server, "list_food_offers")
        assert payload["total"] > 0
        first = payload["offers"][0]
        for field in ("time_status", "eligibility_status", "parse_status", "actionable"):
            assert field in first

    @pytest.mark.asyncio
    async def test_limit_is_capped(self, server):
        payload = await call(server, "list_food_offers", limit=100_000)
        assert payload["returned"] <= 200

    @pytest.mark.asyncio
    async def test_invalid_status_filter_is_rejected(self, server):
        payload = await call(server, "list_food_offers", status="everything")
        assert payload["error"] == "invalid_status"

    @pytest.mark.asyncio
    async def test_unknown_offer_id_is_not_found(self, server):
        payload = await call(server, "get_offer", offer_id="nope")
        assert payload["error"] == "not_found"

    @pytest.mark.asyncio
    async def test_plan_reports_coverage_alongside_the_buckets(self, server):
        payload = await call(server, "build_meal_plan")
        assert "coverage" in payload
        assert "is_partial" in payload["coverage"]
        assert {"this_week", "next_week", "this_month", "needs_confirmation"} <= set(payload)

    @pytest.mark.asyncio
    async def test_render_report_returns_content_not_a_path(self, server):
        payload = await call(server, "render_report", output_format="markdown")
        text = payload if isinstance(payload, str) else json.dumps(payload)
        assert "本周省钱吃法" in text

    @pytest.mark.asyncio
    async def test_render_report_rejects_an_unknown_format(self, server):
        payload = await call(server, "render_report", output_format="pdf")
        text = payload if isinstance(payload, str) else json.dumps(payload)
        assert "invalid_format" in text


class TestWriteBoundary:
    @pytest.mark.asyncio
    async def test_user_state_can_be_recorded(self, server):
        offers = await call(server, "list_food_offers")
        offer_id = offers["offers"][0]["offer_id"]
        payload = await call(server, "set_offer_state", offer_id=offer_id, status="used")
        assert payload["status"] == str(UserStatus.USED)

    @pytest.mark.asyncio
    async def test_invalid_status_is_rejected(self, server):
        offers = await call(server, "list_food_offers")
        offer_id = offers["offers"][0]["offer_id"]
        payload = await call(server, "set_offer_state", offer_id=offer_id, status="redeemed")
        assert payload["error"] == "invalid_status"

    @pytest.mark.asyncio
    async def test_cannot_write_state_for_an_unknown_offer(self, server):
        payload = await call(server, "set_offer_state", offer_id="nope", status="used")
        assert payload["error"] == "not_found"


class TestSpendBoundary:
    @pytest.mark.asyncio
    async def test_invalid_mode_is_rejected(self, server):
        payload = await call(server, "sync_promotions", mode="just-do-it")
        assert payload["error"] == "invalid_mode"

    @pytest.mark.asyncio
    async def test_message_cap_is_enforced(self, server):
        payload = await call(server, "sync_promotions", max_messages=10_000)
        assert payload["status"] in ("completed", "partial")

    @pytest.mark.asyncio
    async def test_a_host_cannot_grant_itself_cloud_consent(self, monkeypatch):
        """Consent is a local decision. No tool argument can override it."""
        clock = FrozenClock(REFERENCE, "America/Chicago")
        service = WeeklyDealsService.offline(clock=clock)
        service.settings.offline = False
        service.settings.app.privacy.cloud_processing_consent = False
        monkeypatch.setattr(
            "weekly_deals.mcp_server.WeeklyDealsService", lambda *a, **k: service
        )
        monkeypatch.setattr(
            "weekly_deals.mcp_server.Settings.build", lambda *a, **k: service.settings
        )
        built = build_server()
        payload = await call(built, "sync_promotions")
        assert payload["error"] == "consent_required"


class TestViews:
    def test_offer_view_distinguishes_absent_from_unknown_deadline(self, clock):
        service = WeeklyDealsService.offline(clock=clock)
        service.sync_promotions(mode="llm-only")
        views = [_offer_view(o) for o in service.list_food_offers()]
        undated = [v for v in views if v["ends"] is None]
        assert undated
        assert all(v["ends_stated"] is False for v in undated)

    def test_plan_view_marks_uncomputable_costs(self, clock):
        service = WeeklyDealsService.offline(clock=clock)
        service.sync_promotions(mode="llm-only")
        view = _plan_view(service.build_meal_plan(), "zh-CN")
        every = (
            view["this_week"] + view["next_week"] + view["this_month"] + view["needs_confirmation"]
        )
        assert every
        assert all(item["cost_computable"] is False for item in every)
