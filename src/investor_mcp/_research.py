from __future__ import annotations

from collections import defaultdict
from typing import Any

from .models import Money, Position, utc_now_iso
from .responses import _NON_ISSUER, _sector_label, ok_response


class ResearchMixin:
    def get_news_digest(
        self,
        period: str = "week",
        from_date: str | None = None,
        to_date: str | None = None,
        account_ids: list[str] | None = None,
        importance_min: str = "medium",
    ) -> dict[str, Any]:
        """Dynamic research brief: WHAT the assistant should web-search, derived from the
        current portfolio (concentrations, asset mix, goals). The server does not fetch
        news itself — it computes targets + tailored search queries; the client searches.
        """
        positions = self._positions(account_ids)
        total = sum(position.current_value.amount for position in positions)
        if not total:
            return ok_response(
                "Портфель пуст — искать нечего.",
                {"period": period, "research_targets": [], "guidance": "", "events": []},
                data_status=self._positions_status,
            )

        issuer_value: dict[str, float] = defaultdict(float)
        issuer_classes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        sector_value: dict[str, float] = defaultdict(float)
        bond_value = 0.0
        for position in positions:
            value = position.current_value.amount
            instrument = position.instrument
            issuer_value[instrument.issuer] += value
            issuer_classes[instrument.issuer][instrument.asset_class] += value
            sector_value[instrument.sector] += value
            if instrument.asset_class == "bond":
                bond_value += value

        issuer_limit = float(self.profile.limits.get("max_single_issuer_percent", 15))
        sector_limit = float(self.profile.limits.get("max_single_sector_percent", 30))
        bond_share = bond_value / total * 100

        targets: list[dict[str, Any]] = []

        # --- Issuers: over the limit, or simply a top exposure (>= 5%) ---
        for issuer, value in sorted(issuer_value.items(), key=lambda item: -item[1]):
            if issuer in _NON_ISSUER:
                continue
            share = value / total * 100
            over = share > issuer_limit
            if not over and share < 5:
                continue
            dominant = max(issuer_classes[issuer], key=issuer_classes[issuer].get)
            if dominant == "bond":
                queries = [
                    f"{issuer} кредитный рейтинг",
                    f"{issuer} облигации новости дефолт реструктуризация",
                    f"{issuer} финансовая отчётность долговая нагрузка",
                ]
                focus = "облигации → кредитный/дефолтный риск, рейтинги"
            elif dominant == "stock":
                queries = [
                    f"{issuer} отчётность прибыль прогноз",
                    f"{issuer} дивиденды новости",
                    f"{issuer} санкции регуляторные риски",
                ]
                focus = "акции → отчётность, дивиденды, корпоративные события"
            else:
                queries = [f"{issuer} новости"]
                focus = "следить за основными событиями"
            limit_note = f" при лимите эмитента {issuer_limit:.0f}%" if over else ""
            targets.append({
                "entity": issuer,
                "kind": "issuer",
                "asset_class": dominant,
                "portfolio_share_percent": round(share, 2),
                "priority": "high" if over else "medium",
                "why": f"{round(share, 1)}% портфеля{limit_note}; {focus}",
                "search_queries": queries,
            })
            if sum(1 for t in targets if t["kind"] == "issuer") >= 6:
                break

        # --- Sectors above the limit ---
        for sector, value in sorted(sector_value.items(), key=lambda item: -item[1]):
            if sector in {"unknown", "cash"}:
                continue
            share = value / total * 100
            if share <= sector_limit:
                continue
            label = _sector_label(sector)
            targets.append({
                "entity": label,
                "kind": "sector",
                "portfolio_share_percent": round(share, 2),
                "priority": "medium",
                "why": f"{round(share, 1)}% портфеля при лимите сектора {sector_limit:.0f}%",
                "search_queries": [f"{label} сектор РФ новости 2026", f"{label} регулирование перспективы"],
            })

        # --- Macro: rate sensitivity for a bond-heavy book ---
        if bond_share > 40:
            targets.append({
                "entity": "Ключевая ставка ЦБ РФ",
                "kind": "macro",
                "portfolio_share_percent": round(bond_share, 2),
                "priority": "high" if bond_share > 60 else "medium",
                "why": f"{round(bond_share)}% портфеля в облигациях — чувствительность к ставке",
                "search_queries": ["ключевая ставка ЦБ РФ решение прогноз", "инфляция РФ динамика ожидания"],
            })

        top_issuers = [t["entity"] for t in targets if t["kind"] == "issuer"][:2]
        guidance_bits = []
        if bond_share > 50:
            guidance_bits.append(f"портфель на {round(bond_share)}% в облигациях")
        if top_issuers:
            guidance_bits.append("ключевая концентрация: " + ", ".join(top_issuers))
        guidance = (
            ("Фокус: " + "; ".join(guidance_bits) + ". ") if guidance_bits else ""
        ) + "Приоритет высоким (high) целям."

        data = {
            "period": period,
            "as_of": utc_now_iso(),
            "portfolio_value": Money(round(total, 2), self.profile.base_currency).to_dict(),
            "bond_share_percent": round(bond_share, 2),
            "research_targets": targets,
            "context_lenses": self._context_lenses(positions, total),
            "guidance": guidance,
            "events": [],  # the server does not fetch news; the client searches research_targets
            "instruction_for_assistant": (
                "Сделай веб-поиск по search_queries каждой цели (начни с priority=high), "
                "затем кратко изложи влияние на ЭТОТ портфель: какие бумаги, какая доля затронута, "
                "почему важно и что можно рассмотреть."
            ),
        }
        summary = (
            f"Бриф для поиска новостей: {len(targets)} целей. {guidance} "
            "Найди по ним свежие новости в вебе и оцени влияние на портфель."
        )
        return ok_response(summary, data, data_status=self._positions_status)

    def _context_lenses(self, positions: list[Position], total: float) -> list[dict[str, Any]]:
        """External factors to research, DERIVED from the portfolio's exposures.

        The server doesn't judge macro/geopolitics/cycles — it tells the assistant which
        of them matter for THIS book (and why), so the assistant researches the current
        picture and folds it into advice. Everything here is data-driven, not hardcoded.
        """
        if not total:
            return []
        by_class: dict[str, float] = defaultdict(float)
        by_sector: dict[str, float] = defaultdict(float)
        issuer_value: dict[str, float] = defaultdict(float)
        issuer_classes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        foreign = 0.0
        for position in positions:
            value = position.current_value.amount
            instrument = position.instrument
            by_class[instrument.asset_class] += value
            by_sector[instrument.sector] += value
            issuer_value[instrument.issuer] += value
            issuer_classes[instrument.issuer][instrument.asset_class] += value
            if instrument.currency != self.profile.base_currency:
                foreign += value

        def pct(value: float) -> float:
            return round(value / total * 100, 1)

        bond_share = pct(by_class.get("bond", 0))
        stock_share = pct(by_class.get("stock", 0))
        energy_materials = pct(by_sector.get("energy", 0) + by_sector.get("materials", 0))
        foreign_share = pct(foreign)
        fin_re = pct(by_sector.get("financial", 0) + by_sector.get("real_estate", 0))
        issuer_limit = float(self.profile.limits.get("max_single_issuer_percent", 15))
        sector_limit = float(self.profile.limits.get("max_single_sector_percent", 30))
        over_bond_issuers = [
            issuer for issuer, value in sorted(issuer_value.items(), key=lambda item: -item[1])
            if issuer not in _NON_ISSUER and value / total * 100 > issuer_limit
            and max(issuer_classes[issuer], key=issuer_classes[issuer].get) == "bond"
        ]

        lenses: list[dict[str, Any]] = []
        if bond_share > 30:
            lenses.append({
                "lens": "Ставка и ДКП",
                "priority": "high" if bond_share > 50 else "medium",
                "why": f"{bond_share}% в облигациях — цены бумаг, реинвест купонов и рефинанс эмитентов зависят от ключевой ставки",
                "research": ["ключевая ставка ЦБ РФ решение и траектория", "инфляция РФ ожидания", "кривая доходности ОФЗ"],
            })
        if over_bond_issuers:
            names = ", ".join(over_bond_issuers[:3])
            lenses.append({
                "lens": "Кредитный риск / ВДО",
                "priority": "high",
                "why": f"облигационные эмитенты выше лимита {issuer_limit:.0f}%: {names} — кредитные спреды, риск дефолта/реструктуризации, рефинанс при высокой ставке",
                "research": [f"{n} кредитный рейтинг и новости" for n in over_bond_issuers[:3]] + ["спреды ВДО к ОФЗ динамика"],
            })
        if energy_materials > 8:
            lenses.append({
                "lens": "Сырьевой цикл",
                "priority": "medium",
                "why": f"{energy_materials}% в нефтегазе/металлах — выручка эмитентов зависит от цен на сырьё",
                "research": ["нефть Brent и Urals прогноз цены", "цены на сталь и цветные металлы", "спрос Китая на сырьё"],
            })
        if foreign_share > 5:
            lenses.append({
                "lens": "Рубль и валюта",
                "priority": "medium",
                "why": f"{foreign_share}% в валютных активах — переоценка зависит от курса рубля; есть риск инфраструктурных ограничений",
                "research": ["курс рубля прогноз", "ограничения и санкции на валютные активы РФ"],
            })
        lenses.append({
            "lens": "Геополитика / санкции / политика",
            "priority": "high" if (foreign_share > 5 or fin_re > 40) else "medium",
            "why": "российский рынок чувствителен к санкциям, геополитике и регулированию"
            + ("; иностранные/валютные бумаги несут риск блокировок" if foreign_share > 0 else ""),
            "research": ["санкции против РФ свежие новости", "геополитическая ситуация и российский рынок", "регулирование банков и застройщиков РФ"],
        })
        for sector, value in sorted(by_sector.items(), key=lambda item: -item[1]):
            if sector in {"unknown", "cash", "government"}:
                continue
            share = value / total * 100
            if share <= sector_limit:
                continue
            label = _sector_label(sector)
            hint = {
                "real_estate": "льготная ипотека, цикл цен на недвижимость, спрос и долговая нагрузка застройщиков",
                "financial": "ключевая ставка, регулирование банков, качество кредитных портфелей",
            }.get(sector, "отраслевое регулирование и спрос")
            lenses.append({
                "lens": f"Сектор: {label}",
                "priority": "medium",
                "why": f"{round(share, 1)}% портфеля выше лимита сектора — {hint}",
                "research": [f"{label} РФ новости и регулирование 2026"],
            })
        if stock_share > 15:
            lenses.append({
                "lens": "Рынок акций и оценки",
                "priority": "medium",
                "why": f"{stock_share}% в акциях для долгосрочного роста — важны фаза рынка, оценки и дивполитика",
                "research": ["индекс МосБиржи прогноз 2026", "дивиденды российских компаний 2026"],
            })
        return lenses
