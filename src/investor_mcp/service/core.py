"""InvestorService core: state, persistence wiring, and account/report/sync tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..adapters import BrokerAdapter, MockBrokerAdapter
from ..errors import classify_broker_error, describe_error, error_hint
from ..models import Account, InvestorProfile, Operation, Position, utc_now_iso
from ..responses import error_response, ok_response
from ..storage import Storage
from .cache import SECTIONS, CacheMixin
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
    # Portfolio-composition data (positions, accounts, operations) is cached for this
    # long so the read tools don't hit the broker on every call. Default: 1 day.
    cache_ttl_seconds: int = 86400
    # After a broker failure the broker is not retried for this long (forced refreshes
    # still are), so a chain of tool calls during an outage costs one timeout, not N.
    broker_retry_cooldown_seconds: int = 60
    _positions_cache: tuple[float, list[Position]] | None = field(default=None, repr=False)
    _accounts_cache: tuple[float, list[Account]] | None = field(default=None, repr=False)
    _operations_cache: tuple[float, list[Operation]] | None = field(default=None, repr=False)
    # Per-section freshness of the LAST read: fresh / cached / stale (see CacheMixin).
    _accounts_status: str = field(default="cached", repr=False)
    _positions_status: str = field(default="cached", repr=False)
    _operations_status: str = field(default="cached", repr=False)
    # section -> {"error": text, "at": iso}; persisted so a restart doesn't hide the cause.
    _broker_errors: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    _broker_retry_after: float = field(default=0.0, repr=False)

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
        errors = self.storage.get_setting("broker_errors")
        if errors:
            self._broker_errors = dict(errors)

    def sync_data(
        self,
        mode: str = "incremental",
        account_ids: list[str] | None = None,
        force: bool = False,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Force-refresh accounts, positions and operations from the broker.

        Honest about failures: a section the broker could not deliver is served from
        the last cache and listed in ``stale_sections`` (status ``partial``); when the
        broker is unreachable and there is no cache to fall back on, the tool returns a
        ``BROKER_*`` error instead of pretending the sync succeeded.
        """
        started = utc_now_iso()
        previous = dict(self.last_sync or {})
        stamp = started.replace("-", "").replace(":", "").replace("T", "_").rstrip("Z")
        record: dict[str, Any] = {"sync_id": f"sync_{stamp}", "mode": mode, "started_at": started}
        try:
            accounts = self._cached_accounts(force=True)
            if self._accounts_status == "fresh":
                positions = self._all_positions(force=True)
                operations = self._all_operations(force=True)
            else:
                # The broker is unreachable: don't spend two more timeouts on calls that
                # will fail the same way — keep the last known data and mark it stale.
                error = self._last_broker_error("accounts")
                positions = self._degrade("positions", error)
                operations = self._degrade("operations", error)
        except Exception as exc:  # noqa: BLE001 — broker down and no cache to fall back on
            error = describe_error(exc)
            record.update(
                finished_at=utc_now_iso(),
                status="failed",
                error=error,
                stale_sections=list(SECTIONS),
                last_success_at=self._last_success_at(previous),
            )
            self._save_last_sync(record)
            hint = error_hint(error)
            return error_response(
                classify_broker_error(exc),
                f"Синхронизация не удалась: {error}. Кэша для отката нет.",
                {"error": error, "hint": hint, "sync": dict(record)},
                warnings=[hint] if hint else None,
            )

        finished = utc_now_iso()
        stale = [section for section in SECTIONS if getattr(self, f"_{section}_status") == "stale"]
        status = "success" if not stale else "partial"
        warnings = [self._stale_warning(section) for section in stale]
        record.update(
            finished_at=finished,
            accounts_synced=len(accounts),
            positions_synced=len(positions),
            operations_synced=len(operations),
            prices_synced=len({p.instrument.instrument_id for p in positions}),
            status=status,
            stale_sections=stale,
            last_success_at=finished if status == "success" else self._last_success_at(previous),
            warnings=warnings,
        )
        if self.storage is not None and self._positions_status == "fresh":
            # Snapshot only genuinely refreshed data: a stale copy would just duplicate
            # the previous snapshot under a new id.
            record["snapshot_id"] = record["sync_id"]
            self._save_last_sync(record)
            snapshot = self.get_portfolio(account_ids)["data"]
            self.storage.save_snapshot(record["sync_id"], snapshot, created_at=finished)
        else:
            self._save_last_sync(record)
        if status == "success":
            summary = "Данные синхронизированы."
        else:
            summary = (
                "Синхронизация частичная: брокер недоступен, разделы "
                + ", ".join(stale)
                + " отданы из кэша (подробности в warnings)."
            )
        return ok_response(
            summary,
            dict(record),
            data_status="fresh" if status == "success" else "stale",
            warnings=warnings,
        )

    def get_sync_status(self) -> dict[str, Any]:
        """Freshness of every cached section plus the outcome of the last sync attempt.

        ``data_status``/``stale_sections`` are derived from the caches themselves (age
        vs TTL, last broker error), not from what the last sync claimed.
        """
        sections = {section: self._section_state(section) for section in SECTIONS}
        stale_sections = [
            section for section, state in sections.items() if state["status"] in {"stale", "unavailable"}
        ]
        data_status = "stale" if stale_sections else "fresh"
        last = self.last_sync or {}
        data = {
            "last_success_at": self._last_success_at(last),
            "last_attempt_at": last.get("finished_at"),
            "status": last.get("status", "not_synced"),
            "data_status": data_status,
            "stale_sections": stale_sections,
            "sections": sections,
            "cache_ttl_seconds": self.cache_ttl_seconds,
        }
        warnings = [self._stale_warning(section) for section in stale_sections if sections[section]["fetched_at"]]
        if not last and not any(state["fetched_at"] for state in sections.values()):
            summary = "Данные еще не синхронизированы."
        elif stale_sections:
            summary = "Данные устарели: " + "; ".join(
                f"{section} — кэш от {sections[section]['fetched_at'] or 'нет данных'}"
                for section in stale_sections
            ) + ". Брокер недоступен или давно не опрашивался."
        else:
            summary = "Данные актуальны."
        return ok_response(summary, data, data_status=data_status, warnings=warnings)

    @staticmethod
    def _last_success_at(last: dict[str, Any]) -> str | None:
        if last.get("last_success_at"):
            return last["last_success_at"]
        return last.get("finished_at") if last.get("status") == "success" else None

    def _save_last_sync(self, record: dict[str, Any]) -> None:
        self.last_sync = record
        if self.storage is not None:
            self.storage.set_setting("last_sync", record)

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
        return ok_response("Счета получены.", {"accounts": data}, **self._meta("accounts"))

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
