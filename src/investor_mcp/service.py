from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from .adapters import BrokerAdapter, MockBrokerAdapter
from .models import Account, InvestorProfile, Money, Position, utc_now_iso
from .storage import Storage


def ok_response(
    summary: str,
    data: dict[str, Any],
    data_status: str = "fresh",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "data_status": data_status,
        "as_of": utc_now_iso(),
        "summary": summary,
        "data": data,
        "warnings": warnings or [],
        "sources": [],
        "resource_links": [],
    }


def error_response(
    error_code: str,
    summary: str,
    data: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Machine-readable tool error. Mapped to isError=true at the MCP layer."""
    return {
        "ok": False,
        "error_code": error_code,
        "data_status": "unavailable",
        "as_of": utc_now_iso(),
        "summary": summary,
        "data": data or {},
        "warnings": warnings or [],
        "sources": [],
        "resource_links": [],
    }


def _asset_class_label(asset_class: str) -> str:
    return {
        "stock": "Акции",
        "bond": "Облигации",
        "fund": "Фонды",
        "currency": "Валюта",
        "cash": "Кэш",
    }.get(asset_class, asset_class)


@dataclass
class InvestorService:
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
    _positions_status: str = field(default="fresh", repr=False)

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
        # Sync = force-refresh the cached composition data.
        accounts = self._cached_accounts(force=True)
        positions = self._all_positions(force=True)
        operations = self.broker.get_operations(account_ids)
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

    def get_portfolio(
        self,
        account_ids: list[str] | None = None,
        refresh: bool = False,
        include_positions: bool = True,
        include_allocation: bool = True,
    ) -> dict[str, Any]:
        positions = self._positions(account_ids, force=refresh)
        total = sum(position.current_value.amount for position in positions)
        data: dict[str, Any] = {
            "base_currency": self.profile.base_currency,
            "total_value": Money(total, self.profile.base_currency).to_dict(),
            "accounts": self._accounts_payload(account_ids),
        }
        if include_positions:
            data["positions"] = [position.to_dict(total) for position in positions]
        if include_allocation:
            data["allocation"] = self._allocation_payload(positions, total)
        return ok_response("Портфель получен.", data, data_status=self._positions_status)

    def analyze_portfolio(
        self,
        account_ids: list[str] | None = None,
        as_of: str | None = None,
        include_goal_comparison: bool = True,
    ) -> dict[str, Any]:
        positions = self._positions(account_ids)
        total = sum(position.current_value.amount for position in positions)
        allocation = self._allocation_payload(positions, total)
        concentration = self._concentration_payload(positions, total)
        goal_deviation = self._goal_deviation(allocation["by_asset_class"]) if include_goal_comparison else []
        findings = []
        if concentration["top_positions"] and concentration["top_positions"][0]["share_percent"] > 10:
            findings.append("Есть концентрация выше 10% в одной позиции.")
        if goal_deviation:
            findings.append("Есть отклонения от целевой аллокации.")
        data = {
            "portfolio_value": Money(total, self.profile.base_currency).to_dict(),
            "allocation": allocation,
            "concentration": concentration,
            "goal_deviation": goal_deviation,
            "key_findings": findings,
        }
        return ok_response("Портфель проанализирован.", data, data_status=self._positions_status)

    def explain_portfolio_change(
        self,
        period: str = "week",
        from_date: str | None = None,
        to_date: str | None = None,
        account_ids: list[str] | None = None,
        include_news: bool = True,
    ) -> dict[str, Any]:
        positions = self._positions(account_ids)
        total_value = sum(position.current_value.amount for position in positions)
        contributors = []
        for position in positions:
            pnl = position.pnl.amount
            contributors.append(
                {
                    "instrument": position.instrument.to_dict(),
                    "change": Money(pnl, position.current_price.currency).to_dict(),
                    "change_percent": round(
                        (position.current_price.amount / position.average_price.amount - 1) * 100,
                        2,
                    )
                    if position.average_price.amount
                    else 0,
                }
            )
        contributors.sort(key=lambda item: item["change"]["amount"], reverse=True)
        total_change = sum(item["change"]["amount"] for item in contributors)
        data = {
            "period": {"label": period, "from": from_date, "to": to_date},
            "total_change": Money(total_change, self.profile.base_currency).to_dict(),
            "total_change_percent": round(total_change / total_value * 100, 2) if total_value else 0.0,
            "contributors": {
                "positive": [item for item in contributors if item["change"]["amount"] >= 0],
                "negative": [item for item in contributors if item["change"]["amount"] < 0],
            },
            "currency_effect": [],
            "related_events": self._mock_events() if include_news else [],
            "interpretation": "Основной вклад рассчитан по разнице средней и текущей цены mock-позиций.",
        }
        return ok_response("Изменение портфеля объяснено.", data, data_status="cached")

    def get_operations(
        self,
        from_date: str,
        to_date: str,
        account_ids: list[str] | None = None,
        instrument: Any = None,
        operation_types: list[str] | None = None,
    ) -> dict[str, Any]:
        if from_date > to_date:
            return error_response(
                "VALIDATION_ERROR",
                "Дата начала периода позже даты конца.",
                {"from_date": from_date, "to_date": to_date},
            )
        effective_accounts = account_ids or self._effective_account_ids()
        instrument_query = self._ref_to_query(instrument).upper() if instrument else None
        types = {t.lower() for t in operation_types} if operation_types else None
        operations = []
        for operation in self.broker.get_operations(effective_accounts):
            if not (from_date <= operation.date <= to_date):
                continue
            if types and operation.operation_type.lower() not in types:
                continue
            if instrument_query and (operation.instrument_id or "").upper() != instrument_query:
                continue
            operations.append(operation.to_dict())
        account_key = effective_accounts[0] if len(effective_accounts) == 1 else "all"
        return ok_response(
            "Операции получены.",
            {
                "operations": operations,
                "total_count": len(operations),
                "resource": f"investor://operations/{account_key}/{from_date}/{to_date}",
            },
        )

    def get_instrument(
        self,
        instrument: Any,
        include_position: bool = True,
        include_events: bool = True,
    ) -> dict[str, Any]:
        query = self._ref_to_query(instrument)
        if not query:
            return error_response("VALIDATION_ERROR", "Не указан инструмент.", {"instrument": instrument})
        positions = self._positions(None)
        position = self._find_position(query, positions)
        if position is None:
            return error_response(
                "INSTRUMENT_NOT_FOUND",
                f"Инструмент {query} не найден в портфеле.",
                {"instrument": instrument},
            )
        instrument_id = position.instrument.instrument_id
        data: dict[str, Any] = {
            "instrument": position.instrument.to_dict(),
            "position": position.to_dict(sum(p.current_value.amount for p in positions))
            if include_position
            else None,
            "events": self._mock_events(instrument_id) if include_events else [],
            "resource": f"investor://positions/{instrument_id}",
        }
        return ok_response("Инструмент найден.", data, data_status=self._positions_status)

    def scan_risks(
        self,
        account_ids: list[str] | None = None,
        risk_types: list[str] | None = None,
        severity_min: str = "low",
    ) -> dict[str, Any]:
        positions = self._positions(account_ids)
        total = sum(position.current_value.amount for position in positions)
        risks = self._compute_risk_signals(positions, total)
        if risk_types:
            wanted = set(risk_types)
            risks = [risk for risk in risks if risk["type"] in wanted]
        filtered = self._filter_by_severity(risks, severity_min)
        return ok_response(
            "Риски просканированы.",
            {"risk_signals": filtered, "resource": "investor://risks/current"},
            data_status=self._positions_status,
        )

    def _compute_risk_signals(self, positions: list[Position], total: float) -> list[dict[str, Any]]:
        """Compute the full (unfiltered) set of risk signals for a position set."""
        risks: list[dict[str, Any]] = []
        max_position_limit = float(self.profile.limits.get("max_single_position_percent", 10))
        issuer_limit = float(self.profile.limits.get("max_single_issuer_percent", 15))

        for position in positions:
            share = position.current_value.amount / total * 100 if total else 0
            if share > max_position_limit:
                risks.append(
                    {
                        "id": f"risk_position_{position.instrument.instrument_id}",
                        "severity": "medium",
                        "type": "concentration",
                        "title": "Высокая доля одной позиции",
                        "affected_positions": [position.instrument.instrument_id],
                        "portfolio_share_percent": round(share, 2),
                        "why_it_matters": "Позиция сильнее влияет на общий результат портфеля.",
                        "suggested_actions": ["наблюдать", "не докупать без причины", "сравнить с лимитами"],
                    }
                )

        issuer_shares: dict[str, float] = defaultdict(float)
        for position in positions:
            issuer_shares[position.instrument.issuer] += position.current_value.amount
        for issuer, value in issuer_shares.items():
            share = value / total * 100 if total else 0
            if share > issuer_limit:
                risks.append(
                    {
                        "id": f"risk_issuer_{issuer}",
                        "severity": "high",
                        "type": "issuer",
                        "title": "Высокая доля одного эмитента",
                        "affected_positions": [
                            p.instrument.instrument_id for p in positions if p.instrument.issuer == issuer
                        ],
                        "portfolio_share_percent": round(share, 2),
                        "why_it_matters": "Портфель зависит от одного эмитента сильнее целевого лимита.",
                        "suggested_actions": ["оценить фундаментальные риски", "диверсифицировать пополнения"],
                    }
                )

        sector_limit = float(self.profile.limits.get("max_single_sector_percent", 30))
        sector_shares: dict[str, float] = defaultdict(float)
        for position in positions:
            sector_shares[position.instrument.sector] += position.current_value.amount
        for sector, value in sector_shares.items():
            if sector in {"unknown", "cash"}:
                continue
            share = value / total * 100 if total else 0
            if share > sector_limit:
                risks.append(
                    {
                        "id": f"risk_sector_{sector}",
                        "severity": "medium",
                        "type": "sector",
                        "title": "Высокая доля одного сектора",
                        "affected_positions": [
                            p.instrument.instrument_id for p in positions if p.instrument.sector == sector
                        ],
                        "portfolio_share_percent": round(share, 2),
                        "why_it_matters": "Портфель зависит от одного сектора сильнее целевого лимита.",
                        "suggested_actions": ["оценить отраслевые риски", "диверсифицировать по секторам"],
                    }
                )

        return risks

    def get_news_digest(
        self,
        period: str = "week",
        from_date: str | None = None,
        to_date: str | None = None,
        account_ids: list[str] | None = None,
        importance_min: str = "medium",
    ) -> dict[str, Any]:
        order = {"low": 0, "medium": 1, "high": 2}
        threshold = order.get(importance_min, 0)
        events = [
            event for event in self._mock_events()
            if order.get(event.get("importance", "low"), 0) >= threshold
        ]
        return ok_response(
            "Новостная выжимка подготовлена.",
            {
                "period": period,
                "events": events,
                "summary": "Пока используется mock-лента. Реальные новости будут отдельным источником.",
                "importance_min": importance_min,
            },
            data_status="cached",
        )

    def recommend_next_action(
        self,
        available_cash: dict[str, Any],
        goal: str = "next_purchase",
        max_options: int = 3,
        account_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        analysis = self.analyze_portfolio(account_ids)["data"]
        deviations = analysis.get("goal_deviation", [])
        sorted_deviations = sorted(deviations, key=lambda item: item["deviation_percent"])
        underweight = [item for item in sorted_deviations if item["deviation_percent"] < 0]
        cash_amount = float(available_cash.get("amount", 0))
        currency = available_cash.get("currency", "RUB")
        recommendations = []
        for index, deviation in enumerate(underweight[:max_options], start=1):
            asset_class = deviation["asset_class"]
            recommendations.append(
                {
                    "id": f"rec_{index}",
                    "action": "buy",
                    "instrument": None,
                    "asset_class": asset_class,
                    "amount": Money(round(cash_amount / max(1, len(underweight[:max_options])), 2), currency).to_dict(),
                    "rationale": f"Класс {_asset_class_label(asset_class)} ниже целевой доли.",
                    "goal_alignment": "Помогает приблизить портфель к целевой аллокации.",
                    "portfolio_effect": {"target_gap_percent": deviation["deviation_percent"]},
                    "risks": ["Проверить ликвидность и новости перед покупкой."],
                    "alternatives": ["оставить часть суммы в кэше", "распределить покупку на несколько дней"],
                    "confidence": "medium",
                }
            )
        if not recommendations:
            recommendations.append(
                {
                    "id": "rec_cash",
                    "action": "hold_cash",
                    "instrument": None,
                    "asset_class": "cash",
                    "amount": Money(cash_amount, currency).to_dict(),
                    "rationale": "Сильных отклонений от целевой аллокации не найдено.",
                    "goal_alignment": "Сохраняет гибкость для следующего решения.",
                    "portfolio_effect": {},
                    "risks": ["Деньги в кэше могут отставать от доходности рынка."],
                    "alternatives": ["дождаться новых данных", "изучить watchlist"],
                    "confidence": "low",
                }
            )
        for recommendation in recommendations:
            self.recommendations[recommendation["id"]] = recommendation
            if self.storage is not None:
                self.storage.save_recommendation(recommendation["id"], recommendation)
        return ok_response(
            "Рекомендации подготовлены.",
            {
                "goal": goal,
                "recommendations": recommendations,
                "disclaimer": "Аналитический сценарий, не гарантия результата.",
            },
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

    # Net value change a single action applies to a position.
    _ADD_ACTIONS = {"buy", "increase"}
    _SUB_ACTIONS = {"sell", "reduce"}

    def simulate_action(
        self,
        actions: list[dict[str, Any]],
        account_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Simulate buy/sell/reduce/increase actions as a 'what-if'.

        Does NOT place any broker order. Pure calculation on the current snapshot.
        In MVP only instruments already held in the portfolio can be simulated.
        """
        if not actions:
            return error_response("VALIDATION_ERROR", "Список actions пуст.", {"actions": actions})

        positions = self._positions(account_ids)
        before_total = sum(position.current_value.amount for position in positions)
        by_id = {position.instrument.instrument_id: position for position in positions}

        deltas: dict[str, float] = defaultdict(float)
        affected_ids: list[str] = []
        for index, item in enumerate(actions):
            action = str(item.get("action", "")).lower()
            if action not in self._ADD_ACTIONS | self._SUB_ACTIONS:
                return error_response(
                    "VALIDATION_ERROR",
                    f"Недопустимое действие: {action or '∅'}.",
                    {"index": index, "action": item.get("action")},
                )
            query = self._ref_to_query(item.get("instrument"))
            if not query:
                return error_response("VALIDATION_ERROR", "Не указан инструмент.", {"index": index})
            position = self._find_position(query, positions)
            if position is None:
                return error_response(
                    "INSTRUMENT_NOT_FOUND",
                    f"Инструмент {query} не найден в портфеле. "
                    "В MVP симуляция доступна для уже имеющихся позиций.",
                    {"index": index, "instrument": item.get("instrument")},
                )
            amount = item.get("amount") or {}
            amount_value = float(amount.get("amount", 0))
            if amount_value <= 0:
                return error_response(
                    "VALIDATION_ERROR",
                    "Сумма действия должна быть положительной.",
                    {"index": index, "amount": amount},
                )
            instrument_id = position.instrument.instrument_id
            deltas[instrument_id] += amount_value if action in self._ADD_ACTIONS else -amount_value
            if instrument_id not in affected_ids:
                affected_ids.append(instrument_id)

        for instrument_id, delta in deltas.items():
            current_value = by_id[instrument_id].current_value.amount
            if current_value + delta < 0:
                return error_response(
                    "VALIDATION_ERROR",
                    f"Суммарная продажа по {instrument_id} превышает стоимость позиции.",
                    {"instrument_id": instrument_id, "position_value": current_value, "net_delta": delta},
                )

        # Build the 'after' position set by adjusting quantities; price stays constant.
        after_positions: list[Position] = []
        for position in positions:
            delta = deltas.get(position.instrument.instrument_id, 0.0)
            if delta and position.current_price.amount:
                new_quantity = position.quantity + delta / position.current_price.amount
                after_positions.append(replace(position, quantity=new_quantity))
            else:
                after_positions.append(position)
        after_total = sum(position.current_value.amount for position in after_positions)

        before = self._snapshot(positions)
        after = self._snapshot(after_positions)

        before_risks = self._compute_risk_signals(positions, before_total)
        after_risks = self._compute_risk_signals(after_positions, after_total)
        before_risk_ids = {risk["id"] for risk in before_risks}
        after_risk_ids = {risk["id"] for risk in after_risks}

        data = {
            "before": before,
            "after": after,
            "changed_metrics": self._diff_metrics(positions, after_positions, affected_ids),
            "new_risks": [risk for risk in after_risks if risk["id"] not in before_risk_ids],
            "reduced_risks": [risk for risk in before_risks if risk["id"] not in after_risk_ids],
            "goal_impact": self._goal_impact(
                before["allocation"]["by_asset_class"], after["allocation"]["by_asset_class"]
            ),
            "disclaimer": "Аналитический сценарий, реальная сделка не выставляется.",
        }
        return ok_response("Действия смоделированы.", data)

    def research_instrument(
        self,
        instrument: Any,
        depth: str = "standard",
        focus: list[str] | None = None,
    ) -> dict[str, Any]:
        query = self._ref_to_query(instrument)
        if not query:
            return error_response("VALIDATION_ERROR", "Не указан инструмент.", {"instrument": instrument})
        positions = self._positions(None)
        position = self._find_position(query, positions)
        if position is None:
            return error_response(
                "INSTRUMENT_NOT_FOUND",
                f"Инструмент {query} не найден в портфеле.",
                {"instrument": instrument},
            )

        instr = position.instrument
        total = sum(p.current_value.amount for p in positions)
        share = round(position.current_value.amount / total * 100, 2) if total else 0.0
        date = utc_now_iso()[:10]
        focus_list = focus or ["fundamentals", "portfolio_fit", "risks"]

        key_points = [
            f"{instr.name} ({instr.ticker}) — класс актива: {_asset_class_label(instr.asset_class)}.",
            f"Доля в портфеле: {share}%.",
            f"Эмитент: {instr.issuer or 'н/д'}, сектор: {instr.sector}.",
        ]
        risks = [
            f"Базовый уровень риска инструмента: {instr.risk_level}.",
            "Внешние источники (рейтинги/отчётность/новости) не подключены — оценка неполная.",
        ]
        portfolio_fit = (
            f"Позиция занимает {share}% портфеля и относится к классу "
            f"{_asset_class_label(instr.asset_class)}."
        )
        markdown = "\n".join(
            [
                f"# Исследование: {instr.name} ({instr.ticker})",
                "",
                f"_Глубина: {depth}. Фокус: {', '.join(focus_list)}. Данные на {date}._",
                "",
                "## Ключевые точки",
                *[f"- {point}" for point in key_points],
                "",
                "## Риски",
                *[f"- {risk}" for risk in risks],
                "",
                "## Соответствие портфелю",
                portfolio_fit,
                "",
                "> Внешние источники пока не подключены: черновик на брокерских и mock-данных.",
            ]
        )
        data = {
            "research_id": f"research_{instr.ticker}_{date}",
            "instrument": instr.to_dict(),
            "markdown": markdown,
            "key_points": key_points,
            "risks": risks,
            "portfolio_fit": portfolio_fit,
            "depth": depth,
            "focus": focus_list,
            "events": self._mock_events(instr.instrument_id),
            "resource": f"investor://research/{instr.instrument_id}/{date}",
        }
        return ok_response(
            f"Исследование по {instr.ticker} подготовлено (черновик).",
            data,
            data_status="partial",
            warnings=[
                "Внешние источники (OpenAI/новости/рейтинги/отчётность) пока не подключены: "
                "исследование строится только на брокерских данных и mock-событиях."
            ],
        )

    def get_bond_calendar(
        self,
        account_ids: list[str] | None = None,
        horizon_days: int = 90,
    ) -> dict[str, Any]:
        """Bond calendar: upcoming coupons/maturities/offers, maturity ladder, 12m income."""
        positions = self._positions(account_ids)
        bonds = [p for p in positions if p.instrument.asset_class == "bond"]
        base = self.profile.base_currency
        empty = {
            "bond_value": Money(0, base).to_dict(),
            "bonds": [],
            "upcoming_events": [],
            "maturity_ladder": [],
            "projected_coupon_income_12m": Money(0, base).to_dict(),
            "horizon_days": horizon_days,
        }
        if not bonds:
            return ok_response("Облигаций в портфеле нет.", empty, data_status=self._positions_status)
        fetch = getattr(self.broker, "get_bond_data", None)
        if fetch is None:
            return ok_response(
                "Источник не поддерживает данные по облигациям.",
                empty,
                data_status="partial",
                warnings=["Текущий брокерский адаптер не отдаёт расписание купонов."],
            )

        bond_data = fetch([bond.instrument.instrument_id for bond in bonds])
        today = utc_now_iso()[:10]
        horizon_end = (date.fromisoformat(today) + timedelta(days=horizon_days)).isoformat()
        one_year = (date.fromisoformat(today) + timedelta(days=365)).isoformat()
        total_bond_value = sum(bond.current_value.amount for bond in bonds)

        events: list[dict[str, Any]] = []
        per_bond: list[dict[str, Any]] = []
        income_12m = 0.0
        ladder_values: dict[str, float] = defaultdict(float)

        for bond in bonds:
            info = bond_data.get(bond.instrument.instrument_id, {})
            quantity = bond.quantity
            currency = bond.current_price.currency
            ref = {
                "instrument_id": bond.instrument.instrument_id,
                "ticker": bond.instrument.ticker,
                "name": bond.instrument.name,
                "issuer": bond.instrument.issuer,
            }
            next_coupon = None
            for coupon in info.get("coupons", []):
                coupon_date = coupon.get("date")
                if not coupon_date or coupon_date < today:
                    continue
                amount = round(float(coupon.get("amount_per_bond", 0)) * quantity, 2)
                if next_coupon is None:
                    next_coupon = {"date": coupon_date, "amount": Money(amount, currency).to_dict()}
                if coupon_date <= one_year:
                    income_12m += amount
                if coupon_date <= horizon_end:
                    events.append({"type": "coupon", "date": coupon_date, "instrument": ref,
                                   "amount": Money(amount, currency).to_dict()})

            maturity = info.get("maturity_date")
            if maturity and today <= maturity <= horizon_end:
                redemption = round(float(info.get("nominal", 0)) * quantity, 2)
                events.append({"type": "maturity", "date": maturity, "instrument": ref,
                               "amount": Money(redemption, currency).to_dict()})
            offer = info.get("offer_date")
            if offer and today <= offer <= horizon_end:
                events.append({"type": "offer", "date": offer, "instrument": ref, "amount": None})

            ladder_values[self._maturity_bucket(today, maturity, info.get("perpetual", False))] += (
                bond.current_value.amount
            )
            per_bond.append({
                **ref,
                "quantity": quantity,
                "value": bond.current_value.to_dict(),
                "maturity_date": maturity,
                "offer_date": offer,
                "coupon_quantity_per_year": info.get("coupon_quantity_per_year", 0),
                "amortization": info.get("amortization", False),
                "floating_coupon": info.get("floating", False),
                "next_coupon": next_coupon,
            })

        events.sort(key=lambda event: event["date"])
        ladder = [
            {
                "bucket": bucket,
                "value": Money(round(ladder_values[bucket], 2), base).to_dict(),
                "share_percent": round(ladder_values[bucket] / total_bond_value * 100, 2)
                if total_bond_value else 0.0,
            }
            for bucket in ("<1y", "1-3y", "3-5y", ">5y", "perpetual", "unknown")
            if bucket in ladder_values
        ]
        data = {
            "bond_value": Money(round(total_bond_value, 2), base).to_dict(),
            "bonds": sorted(per_bond, key=lambda item: item["value"]["amount"], reverse=True),
            "upcoming_events": events,
            "maturity_ladder": ladder,
            "projected_coupon_income_12m": Money(round(income_12m, 2), base).to_dict(),
            "horizon_days": horizon_days,
        }
        summary = (
            f"Бонд-календарь на {horizon_days} дн.: {len(events)} событий; "
            f"купонный доход за 12 мес ≈ {round(income_12m):,} {base}."
        )
        return ok_response(summary, data, data_status=self._positions_status)

    @staticmethod
    def _maturity_bucket(today: str, maturity: str | None, perpetual: bool) -> str:
        if perpetual:
            return "perpetual"
        if not maturity:
            return "unknown"
        try:
            years = (date.fromisoformat(maturity) - date.fromisoformat(today)).days / 365.25
        except Exception:
            return "unknown"
        if years < 1:
            return "<1y"
        if years < 3:
            return "1-3y"
        if years < 5:
            return "3-5y"
        return ">5y"

    def _snapshot(self, positions: list[Position]) -> dict[str, Any]:
        total = sum(position.current_value.amount for position in positions)
        return {
            "portfolio_value": Money(total, self.profile.base_currency).to_dict(),
            "allocation": self._allocation_payload(positions, total),
            "concentration": self._concentration_payload(positions, total),
        }

    def _diff_metrics(
        self,
        before_positions: list[Position],
        after_positions: list[Position],
        affected_ids: list[str],
    ) -> list[dict[str, Any]]:
        before_total = sum(p.current_value.amount for p in before_positions)
        after_total = sum(p.current_value.amount for p in after_positions)
        before_by_id = {p.instrument.instrument_id: p for p in before_positions}
        after_by_id = {p.instrument.instrument_id: p for p in after_positions}

        def pct(value: float, total: float) -> float:
            return round(value / total * 100, 2) if total else 0.0

        metrics: list[dict[str, Any]] = [
            {
                "metric": "portfolio_value",
                "before": Money(before_total, self.profile.base_currency).to_dict(),
                "after": Money(after_total, self.profile.base_currency).to_dict(),
            }
        ]
        affected_classes: list[str] = []
        for instrument_id in affected_ids:
            position = after_by_id.get(instrument_id) or before_by_id.get(instrument_id)
            ticker = position.instrument.ticker
            before_value = before_by_id[instrument_id].current_value.amount if instrument_id in before_by_id else 0.0
            after_value = after_by_id[instrument_id].current_value.amount if instrument_id in after_by_id else 0.0
            metrics.append(
                {
                    "metric": f"position_share_percent:{ticker}",
                    "before": pct(before_value, before_total),
                    "after": pct(after_value, after_total),
                }
            )
            asset_class = position.instrument.asset_class
            if asset_class not in affected_classes:
                affected_classes.append(asset_class)

        for asset_class in affected_classes:
            before_value = sum(
                p.current_value.amount for p in before_positions if p.instrument.asset_class == asset_class
            )
            after_value = sum(
                p.current_value.amount for p in after_positions if p.instrument.asset_class == asset_class
            )
            metrics.append(
                {
                    "metric": f"asset_class_share_percent:{asset_class}",
                    "before": pct(before_value, before_total),
                    "after": pct(after_value, after_total),
                }
            )
        return metrics

    def _goal_impact(
        self,
        before_by_class: list[dict[str, Any]],
        after_by_class: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        before_dev = {item["asset_class"]: item for item in self._goal_deviation(before_by_class)}
        after_dev = {item["asset_class"]: item for item in self._goal_deviation(after_by_class)}
        impact = []
        for asset_class, deviation in before_dev.items():
            impact.append(
                {
                    "asset_class": asset_class,
                    "deviation_before": deviation["deviation_percent"],
                    "deviation_after": after_dev.get(asset_class, deviation)["deviation_percent"],
                }
            )
        return impact

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

    def _positions(self, account_ids: list[str] | None, force: bool = False) -> list[Position]:
        positions = self._all_positions(force=force)
        wanted = set(account_ids or self._effective_account_ids())
        return [position for position in positions if position.account_id in wanted]

    # ---- caching of broker composition data (positions, accounts) ----------

    def _all_positions(self, force: bool = False) -> list[Position]:
        """Return all positions, served from cache within ``cache_ttl_seconds``.

        Sets ``_positions_status`` to fresh/cached/stale. On broker failure falls
        back to any cache (even expired) so the server stays useful offline.
        """
        now = time.time()
        if not force:
            cached = self._read_positions_cache(now)
            if cached is not None:
                self._positions_status = "cached"
                return cached
        try:
            positions = self.broker.get_positions(None)
        except Exception:
            stale = self._read_positions_cache(now, ignore_ttl=True)
            if stale is not None:
                self._positions_status = "stale"
                return stale
            raise
        self._positions_cache = (now, positions)
        self._positions_status = "fresh"
        if self.storage is not None:
            self.storage.set_setting(
                "positions_cache",
                {"fetched_at": utc_now_iso(), "fetched_at_epoch": now,
                 "positions": [p.to_dict() for p in positions]},
            )
        return positions

    def _read_positions_cache(self, now: float, ignore_ttl: bool = False) -> list[Position] | None:
        if self._positions_cache is not None:
            ts, positions = self._positions_cache
            if ignore_ttl or now - ts < self.cache_ttl_seconds:
                return positions
        if self.storage is not None:
            cached = self.storage.get_setting("positions_cache")
            if cached:
                ts = float(cached.get("fetched_at_epoch", 0))
                if ignore_ttl or now - ts < self.cache_ttl_seconds:
                    positions = [Position.from_dict(d) for d in cached.get("positions", [])]
                    self._positions_cache = (ts, positions)
                    return positions
        return None

    def _cached_accounts(self, force: bool = False) -> list[Account]:
        now = time.time()
        if not force and self._accounts_cache is not None:
            ts, accounts = self._accounts_cache
            if now - ts < self.cache_ttl_seconds:
                return accounts
        if not force and self._accounts_cache is None and self.storage is not None:
            cached = self.storage.get_setting("accounts_cache")
            if cached and now - float(cached.get("fetched_at_epoch", 0)) < self.cache_ttl_seconds:
                accounts = [Account.from_dict(d) for d in cached.get("accounts", [])]
                self._accounts_cache = (float(cached["fetched_at_epoch"]), accounts)
                return accounts
        try:
            accounts = self.broker.list_accounts()
        except Exception:
            if self._accounts_cache is not None:
                return self._accounts_cache[1]
            raise
        self._accounts_cache = (now, accounts)
        if self.storage is not None:
            self.storage.set_setting(
                "accounts_cache",
                {"fetched_at": utc_now_iso(), "fetched_at_epoch": now,
                 "accounts": [a.to_dict() for a in accounts]},
            )
        return accounts

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

    def _allocation_payload(self, positions: list[Position], total: float) -> dict[str, list[dict[str, Any]]]:
        buckets: dict[str, dict[str, float]] = {
            "by_asset_class": defaultdict(float),
            "by_currency": defaultdict(float),
            "by_sector": defaultdict(float),
            "by_issuer": defaultdict(float),
        }
        for position in positions:
            value = position.current_value.amount
            buckets["by_asset_class"][position.instrument.asset_class] += value
            buckets["by_currency"][position.instrument.currency] += value
            buckets["by_sector"][position.instrument.sector] += value
            buckets["by_issuer"][position.instrument.issuer] += value
        return {
            name: [
                {
                    "key": key,
                    "value": Money(value, self.profile.base_currency).to_dict(),
                    "share_percent": round(value / total * 100, 2) if total else 0,
                }
                for key, value in sorted(bucket.items(), key=lambda item: item[1], reverse=True)
            ]
            for name, bucket in buckets.items()
        }

    def _concentration_payload(self, positions: list[Position], total: float) -> dict[str, Any]:
        top_positions = sorted(positions, key=lambda position: position.current_value.amount, reverse=True)
        allocation = self._allocation_payload(positions, total)
        return {
            "top_positions": [
                {
                    "instrument_id": position.instrument.instrument_id,
                    "ticker": position.instrument.ticker,
                    "value": position.current_value.to_dict(),
                    "share_percent": round(position.current_value.amount / total * 100, 2) if total else 0,
                }
                for position in top_positions[:5]
            ],
            "top_issuers": allocation["by_issuer"][:5],
            "top_sectors": allocation["by_sector"][:5],
        }

    def _goal_deviation(self, by_asset_class: list[dict[str, Any]]) -> list[dict[str, Any]]:
        current = {item["key"]: item["share_percent"] for item in by_asset_class}
        deviations = []
        for target in self.profile.target_allocation:
            asset_class = target["asset_class"]
            target_percent = float(target["target_percent"])
            current_percent = float(current.get(asset_class, 0))
            deviations.append(
                {
                    "asset_class": asset_class,
                    "current_percent": current_percent,
                    "target_percent": target_percent,
                    "deviation_percent": round(current_percent - target_percent, 2),
                }
            )
        return deviations

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

