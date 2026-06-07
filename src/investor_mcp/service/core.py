"""InvestorService core: state, persistence wiring, and account/report/sync tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..adapters import BrokerAdapter, MockBrokerAdapter
from ..models import Account, InvestorProfile, Operation, Position, utc_now_iso
from ..responses import error_response, ok_response
from ..storage import Storage
from .cache import CacheMixin
from .portfolio import PortfolioMixin
from .risk import RiskMixin
from .bonds import BondMixin
from .research import ResearchMixin
from .goals import GoalsMixin
from .recommend import RecommendMixin


@dataclass
class InvestorService(
    CacheMixin,
    PortfolioMixin,
    RiskMixin,
    BondMixin,
    ResearchMixin,
    GoalsMixin,
    RecommendMixin,
):
    broker: BrokerAdapter = field(default_factory=MockBrokerAdapter)
    profile: InvestorProfile = field(default_factory=InvestorProfile)
    selected_account_ids: list[str] = field(default_factory=list)
    last_sync: dict[str, Any] | None = None
    recommendations: dict[str, dict[str, Any]] = field(default_factory=dict)
    reports: dict[str, str] = field(default_factory=dict)
    storage: Storage | None = None
    # Portfolio-composition data (positions, accounts) is cached for this long so
    # the read tools don't hit the broker on every call. Default: 1 day.
    cache_ttl_seconds: int = 86400
    _positions_cache: tuple[float, list[Position]] | None = field(default=None, repr=False)
    _accounts_cache: tuple[float, list[Account]] | None = field(default=None, repr=False)
    _operations_cache: tuple[float, list[Operation]] | None = field(default=None, repr=False)
    _positions_status: str = field(default="fresh", repr=False)
    _operations_status: str = field(default="fresh", repr=False)

    def __post_init__(self) -> None:
        """Hydrate persisted singletons from storage, if attached."""
        if self.storage is None:
            return
        profile = self.storage.get_setting("profile")
        if profile:
            self.profile = InvestorProfile.from_dict(profile)
        selected = self.storage.get_setting("selected_account_ids")
        if selected:
            self.selected_account_ids = list(selected)
        last_sync = self.storage.get_setting("last_sync")
        if last_sync:
            self.last_sync = last_sync

    def sync_data(
        self,
        mode: str = "incremental",
        account_ids: list[str] | None = None,
        force: bool = False,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        started = utc_now_iso()
        # Sync = force-refresh the cached data: accounts, portfolio AND operations.
        accounts = self._cached_accounts(force=True)
        positions = self._all_positions(force=True)
        operations = self._all_operations(force=True)
        finished = utc_now_iso()
        stamp = started.replace("-", "").replace(":", "").replace("T", "_").rstrip("Z")
        self.last_sync = {
            "sync_id": f"sync_{stamp}",
            "mode": mode,
            "started_at": started,
            "finished_at": finished,
            "accounts_synced": len(accounts),
            "positions_synced": len(positions),
            "operations_synced": len(operations),
            "prices_synced": len({p.instrument.instrument_id for p in positions}),
            "status": "success",
        }
        if self.storage is not None:
            self.last_sync["snapshot_id"] = self.last_sync["sync_id"]
            self.storage.set_setting("last_sync", self.last_sync)
            snapshot = self.get_portfolio(account_ids)["data"]
            self.storage.save_snapshot(self.last_sync["sync_id"], snapshot, created_at=finished)
        return ok_response("Данные синхронизированы.", dict(self.last_sync))

    def get_sync_status(self) -> dict[str, Any]:
        if not self.last_sync:
            data = {
                "last_success_at": None,
                "last_attempt_at": None,
                "status": "not_synced",
                "data_status": "stale",
                "stale_sections": ["portfolio", "operations", "prices"],
            }
            return ok_response("Данные еще не синхронизированы.", data, data_status="stale")
        data = {
            "last_success_at": self.last_sync["finished_at"],
            "last_attempt_at": self.last_sync["finished_at"],
            "status": self.last_sync["status"],
            "data_status": "fresh",
            "stale_sections": [],
        }
        return ok_response("Статус синхронизации получен.", data)

    def get_profile(self) -> dict[str, Any]:
        return ok_response("Инвестиционный профиль получен.", self.profile.to_dict())

    def save_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.profile = InvestorProfile.from_dict(payload)
        if self.storage is not None:
            self.storage.set_setting("profile", self.profile.to_dict())
        return ok_response(
            "Инвестиционный профиль сохранен в локальные настройки.",
            {"profile_saved": True, "profile_resource": "investor://profile/current"},
        )

    def list_accounts(self, include_inactive: bool = False) -> dict[str, Any]:
        accounts = self._cached_accounts()
        if not include_inactive:
            accounts = [account for account in accounts if account.status == "open"]
        selected = set(self._effective_account_ids())
        data = []
        for account in accounts:
            item = account.to_dict()
            item["included_in_analysis"] = account.account_id in selected
            data.append(item)
        return ok_response("Счета получены.", {"accounts": data})

    def select_accounts(self, account_ids: list[str]) -> dict[str, Any]:
        known_ids = {account.account_id for account in self._cached_accounts()}
        unknown = [account_id for account_id in account_ids if account_id not in known_ids]
        if unknown:
            return error_response(
                "ACCOUNT_NOT_FOUND",
                "Некоторые счета не найдены. Выбор не изменен.",
                {"selected_account_ids": self.selected_account_ids, "unknown_account_ids": unknown},
            )
        self.selected_account_ids = list(account_ids)
        if self.storage is not None:
            self.storage.set_setting("selected_account_ids", self.selected_account_ids)
        return ok_response(
            "Счета для анализа выбраны.",
            {"selected_account_ids": self.selected_account_ids, "accounts_resource": "investor://accounts"},
        )

    def generate_report(
        self,
        report_type: str = "weekly",
        from_date: str | None = None,
        to_date: str | None = None,
        account_ids: list[str] | None = None,
        format: str = "markdown",
    ) -> dict[str, Any]:
        portfolio = self.get_portfolio(account_ids)["data"]
        analysis = self.analyze_portfolio(account_ids)["data"]
        risks = self.scan_risks(account_ids)["data"]["risk_signals"]
        news = self.get_news_digest(report_type)["data"]["events"]
        date = to_date or utc_now_iso()[:10]
        report_id = f"{report_type}_{date}"
        markdown = "\n".join(
            [
                f"# {report_type.capitalize()} report",
                "",
                f"Portfolio value: {portfolio['total_value']['amount']:.2f} {portfolio['total_value']['currency']}",
                "",
                "## Key findings",
                *[f"- {finding}" for finding in analysis.get("key_findings", [])],
                "",
                "## Risks",
                *[f"- {risk['title']}: {risk['portfolio_share_percent']}%" for risk in risks],
                "",
                "## Events",
                *[f"- {event['title']}" for event in news],
            ]
        )
        self.reports[report_id] = markdown
        if self.storage is not None:
            self.storage.save_report(report_id, report_type, date, markdown)
        data: dict[str, Any] = {
            "report_id": report_id,
            "report_type": report_type,
            "period": {"from": from_date, "to": to_date},
            "resource": f"investor://reports/{report_type}/{date}",
        }
        if format == "json":
            data["report"] = {
                "portfolio_value": portfolio["total_value"],
                "key_findings": analysis.get("key_findings", []),
                "risks": risks,
                "events": news,
            }
        else:
            data["markdown"] = markdown
        return ok_response("Отчет сформирован.", data, data_status="cached")

    def get_recommendation(self, recommendation_id: str) -> dict[str, Any] | None:
        if recommendation_id in self.recommendations:
            return self.recommendations[recommendation_id]
        if self.storage is not None:
            return self.storage.get_recommendation(recommendation_id)
        return None

    def get_report(self, report_type: str, date: str) -> str | None:
        cached = self.reports.get(f"{report_type}_{date}")
        if cached is not None:
            return cached
        if self.storage is not None:
            return self.storage.get_report(report_type, date)
        return None

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        if self.storage is None:
            return None
        return self.storage.get_snapshot(snapshot_id)

    def list_snapshots(self, limit: int = 20) -> list[dict[str, Any]]:
        if self.storage is None:
            return []
        return self.storage.list_snapshots(limit)

    def domain_schema(self) -> dict[str, Any]:
        """Static description of the normalized domain model (resource context)."""
        return {
            "money": {"amount": "number", "currency": "string"},
            "instrument_ref": {
                "id_type": ["ticker", "figi", "uid", "isin", "internal_id"],
                "id": "string",
            },
            "entities": {
                "account": ["account_id", "name", "type", "status", "included_in_analysis"],
                "instrument": [
                    "instrument_id", "ticker", "name", "asset_class",
                    "currency", "issuer", "sector", "risk_level",
                ],
                "position": [
                    "account_id", "instrument", "quantity", "average_price",
                    "current_price", "current_value", "pnl", "portfolio_share_percent",
                ],
                "operation": [
                    "operation_id", "account_id", "date", "operation_type",
                    "instrument_id", "quantity", "amount", "description",
                ],
                "risk_signal": [
                    "id", "severity", "type", "title", "affected_positions",
                    "portfolio_share_percent", "why_it_matters", "suggested_actions",
                ],
                "recommendation": [
                    "id", "action", "instrument", "asset_class", "amount",
                    "rationale", "goal_alignment", "portfolio_effect",
                    "risks", "alternatives", "confidence",
                ],
            },
            "enums": {
                "asset_class": ["stock", "bond", "fund", "currency", "cash"],
                "data_status": ["fresh", "cached", "stale", "partial", "unavailable"],
                "severity": ["low", "medium", "high", "critical"],
            },
        }

    def _effective_account_ids(self) -> list[str]:
        if self.selected_account_ids:
            return list(self.selected_account_ids)
        return [account.account_id for account in self._cached_accounts() if account.status == "open"]

    @staticmethod
    def _ref_to_query(ref: Any) -> str:
        """Normalize an instrument reference to a lookup string.

        Accepts either a plain id/ticker string or the contract object
        ``{"id_type": "ticker", "id": "SBER"}``.
        """
        if isinstance(ref, dict):
            return str(ref.get("id", "")).strip()
        return str(ref or "").strip()

    @staticmethod
    def _find_position(instrument_id: str, positions: list[Position]) -> Position | None:
        needle = instrument_id.upper()
        for position in positions:
            instrument = position.instrument
            if needle in {
                instrument.instrument_id.upper(),
                instrument.ticker.upper(),
                instrument.name.upper(),
            }:
                return position
        return None

    def _accounts_payload(self, account_ids: list[str] | None) -> list[dict[str, Any]]:
        selected = set(account_ids or self._effective_account_ids())
        payload = []
        for account in self._cached_accounts():
            if account.account_id in selected:
                payload.append(account.to_dict())
        return payload

    def _mock_events(self, instrument_id: str | None = None) -> list[dict[str, Any]]:
        events = [
            {
                "id": "event_rate",
                "date": "2026-06-07",
                "title": "Mock: рынок ждет решения по ключевой ставке",
                "type": "macro",
                "importance": "medium",
                "affected_instruments": ["OFZ26243"],
                "portfolio_impact": "Может влиять на цену облигаций.",
                "suggested_attention": "watch",
            },
            {
                "id": "event_sber",
                "date": "2026-06-07",
                "title": "Mock: банковский сектор остается главным драйвером акций в портфеле",
                "type": "sector",
                "importance": "medium",
                "affected_instruments": ["SBER"],
                "portfolio_impact": "Влияет на крупнейшую акционную позицию.",
                "suggested_attention": "watch",
            },
        ]
        if instrument_id:
            return [event for event in events if instrument_id in event["affected_instruments"]]
        return events

    @staticmethod
    def _filter_by_severity(risks: list[dict[str, Any]], severity_min: str) -> list[dict[str, Any]]:
        order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        threshold = order.get(severity_min, 0)
        return [risk for risk in risks if order.get(risk.get("severity", "low"), 0) >= threshold]
