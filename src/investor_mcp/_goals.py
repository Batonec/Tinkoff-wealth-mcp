from __future__ import annotations

from typing import Any

from .models import Money
from .responses import ok_response


class GoalsMixin:
    def get_goal_progress(
        self,
        account_ids: list[str] | None = None,
        expected_return_percent: float = 12.0,
    ) -> dict[str, Any]:
        """Progress to the user's long-term goals: capital target, passive-income
        coverage (coupons + dividends), and a rough timeline projection."""
        positions = self._positions(account_ids)
        total = sum(position.current_value.amount for position in positions)
        base = self.profile.base_currency
        goals = self.profile.goals or {}

        coupons = self.get_bond_calendar(account_ids)["data"]["projected_coupon_income_12m"]["amount"]
        dividends = 0.0
        fetch = getattr(self.broker, "get_dividend_data", None)
        shares = [p for p in positions if p.instrument.asset_class == "stock"]
        if fetch is not None and shares:
            div_data = fetch([share.instrument.instrument_id for share in shares])
            for share in shares:
                per_share = div_data.get(share.instrument.instrument_id, {}).get("annual_dividend_per_share", 0.0)
                dividends += per_share * share.quantity

        annual_income = coupons + dividends
        monthly_income = annual_income / 12
        income_yield = annual_income / total if total else 0.0

        target_capital = (goals.get("target_capital") or {}).get("amount")
        target_monthly = (goals.get("target_monthly_income") or {}).get("amount")
        annual_add = self.profile.monthly_contribution.amount * 12 + float(
            (goals.get("annual_bonus") or {}).get("amount", 0)
        )
        rate = expected_return_percent / 100

        projection: dict[str, Any] = {}
        if target_capital:
            projection["years_to_capital_target"] = self._years_to(total, target_capital, annual_add, rate)
        if target_monthly and income_yield > 0:
            capital_for_income = (target_monthly * 12) / income_yield
            projection["capital_needed_for_income"] = Money(round(capital_for_income, 2), base).to_dict()
            projection["years_to_income_target"] = self._years_to(total, capital_for_income, annual_add, rate)

        data = {
            "capital": {
                "current": Money(round(total, 2), base).to_dict(),
                "target": Money(target_capital, base).to_dict() if target_capital else None,
                "progress_percent": round(total / target_capital * 100, 1) if target_capital else None,
                "gap": Money(round(target_capital - total, 2), base).to_dict() if target_capital else None,
            },
            "income": {
                "annual_coupons": Money(round(coupons, 2), base).to_dict(),
                "annual_dividends": Money(round(dividends, 2), base).to_dict(),
                "annual_total": Money(round(annual_income, 2), base).to_dict(),
                "monthly_total": Money(round(monthly_income, 2), base).to_dict(),
                "target_monthly": Money(target_monthly, base).to_dict() if target_monthly else None,
                "coverage_percent": round(monthly_income / target_monthly * 100, 1) if target_monthly else None,
                "current_income_yield_percent": round(income_yield * 100, 2),
            },
            "projection": projection,
            "assumptions": {
                "annual_contribution": Money(round(annual_add, 2), base).to_dict(),
                "expected_return_percent": expected_return_percent,
                "note": "Доход реинвестируется, тело не проедается; проекция — грубый сценарий.",
            },
            "disclaimer": "Аналитический сценарий, не гарантия результата.",
        }
        if not (target_capital or target_monthly):
            data["warning_no_goals"] = "Цели не заданы в профиле — задай target_capital/target_monthly_income через investor_save_profile."

        cap_pct = data["capital"]["progress_percent"]
        cov_pct = data["income"]["coverage_percent"]
        summary = (
            f"Капитал: {cap_pct}% к цели. " if cap_pct is not None else ""
        ) + (
            f"Пассивный доход покрывает {cov_pct}% цели (≈{round(monthly_income):,} ₽/мес)." if cov_pct is not None else
            f"Пассивный доход ≈{round(monthly_income):,} ₽/мес."
        )
        return ok_response(summary, data, data_status=self._positions_status)

    @staticmethod
    def _years_to(current: float, target: float, annual_add: float, rate: float) -> int | None:
        if current >= target:
            return 0
        value = current
        for year in range(1, 51):
            value = value * (1 + rate) + annual_add
            if value >= target:
                return year
        return None
