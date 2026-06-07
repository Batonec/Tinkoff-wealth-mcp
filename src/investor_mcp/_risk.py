from __future__ import annotations

from collections import defaultdict
from typing import Any

from .models import Position
from .responses import ok_response


class RiskMixin:
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
