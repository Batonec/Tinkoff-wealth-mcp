from __future__ import annotations

from typing import Any

from .models import Money
from .responses import _asset_class_label, ok_response


class RecommendMixin:
    def recommend_next_action(
        self,
        available_cash: dict[str, Any],
        goal: str = "next_purchase",
        max_options: int = 3,
        account_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        positions = self._positions(account_ids)
        total = sum(position.current_value.amount for position in positions)
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
                "context_to_research": self._context_lenses(positions, total),
                "instruction_for_assistant": (
                    "Это скелет ребаланса по целевой аллокации. ПРЕЖДЕ чем дать финальный совет, "
                    "изучи в вебе context_to_research (ставка/ДКП, кредитный риск ВДО, сырьевой цикл, "
                    "рубль, геополитика/санкции, секторы, рынок акций) и учти текущую макро- и "
                    "политическую картину, а также долгосрочные цели пользователя (profile.goals). "
                    "Затем дай рекомендацию с обоснованием, рисками и альтернативами — не категорично."
                ),
                "disclaimer": "Аналитический сценарий, не гарантия результата.",
            },
        )
