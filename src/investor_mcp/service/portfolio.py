"""PortfolioMixin: portfolio composition, analysis, instrument lookup, simulation, research."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

from ..models import Money, Position, utc_now_iso
from ..responses import _asset_class_label, error_response, ok_response


class PortfolioMixin:
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
        wanted_accounts = set(effective_accounts)
        instrument_query = self._ref_to_query(instrument).upper() if instrument else None
        types = {t.lower() for t in operation_types} if operation_types else None
        operations = []
        for operation in self._all_operations():
            if operation.account_id not in wanted_accounts:
                continue
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
            data_status=self._operations_status,
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
