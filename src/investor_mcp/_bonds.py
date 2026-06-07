from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from .models import Money, utc_now_iso
from .responses import ok_response


class BondMixin:
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
