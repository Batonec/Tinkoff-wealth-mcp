from __future__ import annotations

import argparse
import json
import os
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent

from .adapters import build_broker_adapter
from .service import InvestorService
from .storage import Storage

load_dotenv()

mcp = FastMCP("Investor MCP")
_db_path = os.getenv("INVESTOR_MCP_STORAGE_PATH", "./data/investor_mcp.db")
_cache_ttl = int(os.getenv("INVESTOR_MCP_CACHE_TTL_SECONDS", "86400"))  # default: 1 day
service = InvestorService(
    broker=build_broker_adapter(),
    storage=Storage(_db_path),
    cache_ttl_seconds=_cache_ttl,
)


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


class _BearerAuthMiddleware:
    """Minimal ASGI middleware: require ``Authorization: Bearer <token>`` on HTTP.

    Active only when ``INVESTOR_MCP_AUTH_TOKEN`` is set. Defence-in-depth for a
    publicly reachable endpoint that exposes the user's whole portfolio.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if headers.get(b"authorization", b"").decode() == self._expected:
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})


def _result(payload: dict[str, Any]) -> CallToolResult:
    """Wrap a service response dict into an MCP CallToolResult.

    The human-readable text is the ``summary`` field; the full wrapper goes into
    ``structuredContent``; ``isError`` is derived from ``ok``.
    """
    text = payload.get("summary") or ("Ошибка." if not payload.get("ok", True) else "Готово.")
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=payload,
        isError=not payload.get("ok", True),
    )


@mcp.resource("investor://profile/current")
def profile_resource() -> str:
    """Current investment profile."""
    return _json(service.get_profile()["data"])


@mcp.resource("investor://accounts")
def accounts_resource() -> str:
    """Broker accounts selected for analysis."""
    return _json(service.list_accounts()["data"])


@mcp.resource("investor://portfolio/current")
def portfolio_resource() -> str:
    """Current aggregated portfolio."""
    return _json(service.get_portfolio()["data"])


@mcp.resource("investor://risks/current")
def risks_resource() -> str:
    """Current portfolio risk signals."""
    return _json(service.scan_risks()["data"])


@mcp.resource("investor://portfolio/snapshots/{snapshot_id}")
def snapshot_resource(snapshot_id: str) -> str:
    """Historical portfolio snapshot stored at sync time."""
    snapshot = service.get_snapshot(snapshot_id)
    if snapshot is None:
        return _json({"available": False, "snapshot_id": snapshot_id})
    return _json(snapshot)


@mcp.resource("investor://positions/{instrument_id}")
def position_resource(instrument_id: str) -> str:
    """Position detail for an instrument."""
    return _json(service.get_instrument({"id_type": "internal_id", "id": instrument_id})["data"])


@mcp.resource("investor://operations/{account_id}/{from_date}/{to_date}")
def operations_resource(account_id: str, from_date: str, to_date: str) -> str:
    """Operations for an account over a date range."""
    return _json(service.get_operations(from_date, to_date, [account_id])["data"])


@mcp.resource("investor://recommendations/{recommendation_id}")
def recommendation_resource(recommendation_id: str) -> str:
    """A stored recommendation."""
    recommendation = service.get_recommendation(recommendation_id)
    if recommendation is None:
        return _json({"available": False, "recommendation_id": recommendation_id})
    return _json(recommendation)


@mcp.resource("investor://reports/{report_type}/{date}", mime_type="text/markdown")
def report_resource(report_type: str, date: str) -> str:
    """A generated report in Markdown."""
    markdown = service.get_report(report_type, date)
    return markdown if markdown is not None else f"# Отчет не найден\n\n{report_type} / {date}"


@mcp.resource("investor://research/{instrument_id}/{date}", mime_type="text/markdown")
def research_resource(instrument_id: str, date: str) -> str:
    """An instrument research draft in Markdown."""
    result = service.research_instrument({"id_type": "internal_id", "id": instrument_id})
    if not result["ok"]:
        return f"# Инструмент не найден\n\n{instrument_id}"
    return result["data"].get("markdown", "# Исследование недоступно")


@mcp.resource("investor://schema/domain")
def domain_schema_resource() -> str:
    """Description of the normalized domain model."""
    return _json(service.domain_schema())


@mcp.tool()
def investor_sync_data(
    mode: str = "incremental",
    account_ids: list[str] | None = None,
    force: bool = False,
    from_date: str | None = None,
    to_date: str | None = None,
) -> CallToolResult:
    """Synchronize broker and market data. Read-only in MVP."""
    return _result(service.sync_data(mode, account_ids, force, from_date, to_date))


@mcp.tool()
def investor_get_sync_status() -> CallToolResult:
    """Return last sync status and data freshness."""
    return _result(service.get_sync_status())


@mcp.tool()
def investor_get_profile() -> CallToolResult:
    """Return investment goals, risk profile, and limits."""
    return _result(service.get_profile())


@mcp.tool()
def investor_save_profile(profile: dict[str, Any]) -> CallToolResult:
    """Save local investment profile. Does not change broker data."""
    return _result(service.save_profile(profile))


@mcp.tool()
def investor_list_accounts(include_inactive: bool = False) -> CallToolResult:
    """List broker accounts available for analysis."""
    return _result(service.list_accounts(include_inactive))


@mcp.tool()
def investor_select_accounts(account_ids: list[str]) -> CallToolResult:
    """Select broker accounts included in portfolio analysis."""
    return _result(service.select_accounts(account_ids))


@mcp.tool()
def investor_get_portfolio(
    account_ids: list[str] | None = None,
    refresh: bool = False,
    include_positions: bool = True,
    include_allocation: bool = True,
) -> CallToolResult:
    """Return current aggregated portfolio."""
    return _result(service.get_portfolio(account_ids, refresh, include_positions, include_allocation))


@mcp.tool()
def investor_analyze_portfolio(
    account_ids: list[str] | None = None,
    as_of: str | None = None,
    include_goal_comparison: bool = True,
) -> CallToolResult:
    """Analyze allocation, concentration, and goal deviation."""
    return _result(service.analyze_portfolio(account_ids, as_of, include_goal_comparison))


@mcp.tool()
def investor_explain_portfolio_change(
    period: str = "week",
    from_date: str | None = None,
    to_date: str | None = None,
    account_ids: list[str] | None = None,
    include_news: bool = True,
) -> CallToolResult:
    """Explain portfolio change for a period. Date range uses from_date/to_date."""
    return _result(service.explain_portfolio_change(period, from_date, to_date, account_ids, include_news))


@mcp.tool()
def investor_get_operations(
    from_date: str,
    to_date: str,
    account_ids: list[str] | None = None,
    instrument: dict[str, Any] | None = None,
    operation_types: list[str] | None = None,
) -> CallToolResult:
    """Return account operations for a date range (from_date/to_date)."""
    return _result(service.get_operations(from_date, to_date, account_ids, instrument, operation_types))


@mcp.tool()
def investor_get_instrument(
    instrument: dict[str, Any],
    include_position: bool = True,
    include_events: bool = True,
) -> CallToolResult:
    """Return instrument card and portfolio position. ``instrument`` is {id_type, id}."""
    return _result(service.get_instrument(instrument, include_position, include_events))


@mcp.tool()
def investor_scan_risks(
    account_ids: list[str] | None = None,
    risk_types: list[str] | None = None,
    severity_min: str = "low",
) -> CallToolResult:
    """Scan portfolio risk signals."""
    return _result(service.scan_risks(account_ids, risk_types, severity_min))


@mcp.tool()
def investor_get_news_digest(
    period: str = "week",
    from_date: str | None = None,
    to_date: str | None = None,
    account_ids: list[str] | None = None,
    importance_min: str = "medium",
) -> CallToolResult:
    """Return portfolio-related news and events digest."""
    return _result(service.get_news_digest(period, from_date, to_date, account_ids, importance_min))


@mcp.tool()
def investor_recommend_next_action(
    available_cash: dict[str, Any],
    goal: str = "next_purchase",
    max_options: int = 3,
    account_ids: list[str] | None = None,
) -> CallToolResult:
    """Recommend next analytical action, usually for a cash contribution."""
    return _result(service.recommend_next_action(available_cash, goal, max_options, account_ids))


@mcp.tool()
def investor_simulate_action(
    actions: list[dict[str, Any]],
    account_ids: list[str] | None = None,
) -> CallToolResult:
    """Simulate buy/sell/reduce/increase actions as a 'what-if'.

    Each action is {action, instrument: {id_type, id}, amount: {amount, currency}}.
    Does NOT place any broker order.
    """
    return _result(service.simulate_action(actions, account_ids))


@mcp.tool()
def investor_generate_report(
    report_type: str = "weekly",
    from_date: str | None = None,
    to_date: str | None = None,
    account_ids: list[str] | None = None,
    format: str = "markdown",
) -> CallToolResult:
    """Generate a portfolio report (markdown or json)."""
    return _result(service.generate_report(report_type, from_date, to_date, account_ids, format))


@mcp.tool()
def investor_research_instrument(
    instrument: dict[str, Any],
    depth: str = "standard",
    focus: list[str] | None = None,
) -> CallToolResult:
    """Prepare an instrument/issuer research draft (external sources not wired yet).

    ``instrument`` is {id_type, id}. ``depth`` is brief|standard|deep.
    """
    return _result(service.research_instrument(instrument, depth, focus))


@mcp.prompt()
def portfolio_weekly_review(week_ending: str | None = None) -> str:
    window = f" на неделю, заканчивающуюся {week_ending}," if week_ending else ""
    return (
        f"Подготовь недельный обзор портфеля{window}: проверь свежесть данных, получи портфель, "
        "объясни изменения, просканируй риски, добавь новостную выжимку и сформируй отчет."
    )


@mcp.prompt()
def portfolio_drop_explainer(period: str = "day") -> str:
    return (
        f"Объясни, почему портфель изменился за период: {period}. "
        "Отдели рыночный шум от возможных фундаментальных причин."
    )


@mcp.prompt()
def next_purchase_advice(amount: float, currency: str = "RUB") -> str:
    return (
        f"Подбери варианты для следующего пополнения на {amount} {currency}. "
        "Учитывай цели, текущую аллокацию, риски и альтернативы."
    )


@mcp.prompt()
def instrument_deep_dive(instrument: str, depth: str = "standard") -> str:
    return (
        f"Сделай разбор инструмента {instrument} (глубина: {depth}): получи карточку и позицию, "
        "подготовь исследование, оцени риски и роль актива в портфеле относительно целей."
    )


@mcp.prompt()
def risk_review() -> str:
    return (
        "Покажи, что сейчас требует внимания: просканируй риски портфеля, "
        "подмешай важные новости и события и предложи приоритетные действия."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Investor MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport to use.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("INVESTOR_MCP_HOST", "127.0.0.1"),
        help="Host for streamable-http transport.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("INVESTOR_MCP_PORT", "8000")),
        help="Port for streamable-http transport.",
    )
    parser.add_argument(
        "--mcp-path",
        default=os.getenv("INVESTOR_MCP_PATH", "/mcp"),
        help="HTTP path for streamable MCP endpoint.",
    )
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.settings.streamable_http_path = args.mcp_path
        # The server binds to localhost behind a trusted reverse proxy/tunnel, so the
        # built-in DNS-rebinding Host check (localhost-only) would reject proxied
        # requests. Disable it by default; set INVESTOR_MCP_ALLOWED_HOSTS for strict mode.
        allowed = os.getenv("INVESTOR_MCP_ALLOWED_HOSTS")
        if allowed:
            hosts = [h.strip() for h in allowed.split(",") if h.strip()]
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=hosts,
                allowed_origins=[f"https://{h}" for h in hosts],
            )
        else:
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )
        token = os.getenv("INVESTOR_MCP_AUTH_TOKEN")
        if token:
            import uvicorn

            app = _BearerAuthMiddleware(mcp.streamable_http_app(), token)
            uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        else:
            mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
