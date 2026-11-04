from __future__ import annotations

import unittest

from investor_mcp.models import Account, Instrument, Money, Position
from investor_mcp.service import InvestorService


class InvestorServiceTest(unittest.TestCase):
    def test_portfolio_has_total_and_positions(self) -> None:
        service = InvestorService()

        result = service.get_portfolio()

        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertGreater(data["total_value"]["amount"], 0)
        self.assertGreaterEqual(len(data["positions"]), 1)

    def test_analyze_portfolio_returns_goal_deviation(self) -> None:
        service = InvestorService()

        result = service.analyze_portfolio()

        self.assertTrue(result["ok"])
        self.assertIn("goal_deviation", result["data"])
        self.assertGreaterEqual(len(result["data"]["goal_deviation"]), 1)

    def test_risk_scan_returns_concentration_risks(self) -> None:
        service = InvestorService()

        result = service.scan_risks(severity_min="medium")

        self.assertTrue(result["ok"])
        self.assertIn("risk_signals", result["data"])

    def test_recommend_next_action_uses_cash_amount(self) -> None:
        service = InvestorService()

        result = service.recommend_next_action({"amount": 50_000, "currency": "RUB"})

        self.assertTrue(result["ok"])
        recommendations = result["data"]["recommendations"]
        self.assertGreaterEqual(len(recommendations), 1)
        self.assertIn("amount", recommendations[0])

    def test_news_digest_returns_dynamic_research_brief(self) -> None:
        service = InvestorService()

        result = service.get_news_digest()

        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertGreater(len(data["research_targets"]), 0)
        self.assertEqual(data["events"], [])  # server doesn't fetch news itself
        # Targets are data-driven: top exposures become issuer targets with tailored queries...
        entities = {t["entity"] for t in data["research_targets"]}
        self.assertIn("Sber", entities)
        # ...but federal bonds (ОФЗ / MinFin, sector government) are never framed as an
        # issuer credit/default target, however large the exposure.
        self.assertNotIn("MinFin", entities)
        self.assertFalse(any("дефолт" in q for t in data["research_targets"] for q in t["search_queries"]
                             if t.get("asset_class") == "bond"))

    def test_goal_progress_computes_capital_income_projection(self) -> None:
        from investor_mcp.storage import Storage

        storage = Storage(":memory:")
        service = InvestorService(storage=storage)
        service.save_profile({
            "risk_profile": "balanced", "horizon": "long_term",
            "monthly_contribution": {"amount": 10_000, "currency": "RUB"},
            "goals": {
                "target_capital": {"amount": 1_000_000, "currency": "RUB"},
                "target_monthly_income": {"amount": 5_000, "currency": "RUB"},
                "annual_bonus": {"amount": 0, "currency": "RUB"},
            },
        })

        result = service.get_goal_progress()

        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertGreater(data["capital"]["progress_percent"], 0)
        # income = bond coupons + share dividends (both from the mock adapter)
        self.assertGreater(data["income"]["annual_coupons"]["amount"], 0)
        self.assertGreater(data["income"]["annual_dividends"]["amount"], 0)
        self.assertIn("years_to_capital_target", data["projection"])

    def test_context_lenses_in_brief_and_recommend(self) -> None:
        service = InvestorService()

        brief = service.get_news_digest()["data"]
        self.assertIn("context_lenses", brief)
        self.assertTrue(any("Геополитика" in lens["lens"] for lens in brief["context_lenses"]))

        rec = service.recommend_next_action({"amount": 100_000})["data"]
        self.assertIn("context_to_research", rec)
        self.assertTrue(rec["context_to_research"])
        self.assertIn("instruction_for_assistant", rec)

    def test_bond_calendar_lists_coupons_and_ladder(self) -> None:
        service = InvestorService()

        result = service.get_bond_calendar(horizon_days=90)

        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertEqual(len(data["bonds"]), 1)
        self.assertTrue(any(e["type"] == "coupon" for e in data["upcoming_events"]))
        self.assertGreater(data["projected_coupon_income_12m"]["amount"], 0)
        self.assertTrue(any(bucket["bucket"] == "1-3y" for bucket in data["maturity_ladder"]))
        self.assertIsNotNone(data["bonds"][0]["next_coupon"])

    def test_bond_calendar_includes_maturity_in_long_horizon(self) -> None:
        service = InvestorService()

        result = service.get_bond_calendar(horizon_days=800)

        self.assertTrue(any(e["type"] == "maturity" for e in result["data"]["upcoming_events"]))

    def test_bond_calendar_empty_when_no_bonds(self) -> None:
        service = InvestorService()

        result = service.get_bond_calendar(account_ids=["mock-iis"])

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["bonds"], [])

    def test_scan_risks_includes_sector_concentration(self) -> None:
        service = InvestorService()

        result = service.scan_risks()

        types = {risk["type"] for risk in result["data"]["risk_signals"]}
        self.assertIn("sector", types)

    def test_get_instrument_not_found_returns_error_code(self) -> None:
        service = InvestorService()

        result = service.get_instrument("DOES_NOT_EXIST")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INSTRUMENT_NOT_FOUND")

    def test_select_unknown_account_returns_error_code(self) -> None:
        service = InvestorService()

        result = service.select_accounts(["nope"])

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ACCOUNT_NOT_FOUND")

    def test_get_operations_rejects_reversed_range(self) -> None:
        service = InvestorService()

        result = service.get_operations("2026-06-10", "2026-06-01")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")

    def test_simulate_buy_grows_total_and_shifts_metrics(self) -> None:
        service = InvestorService()

        result = service.simulate_action(
            [{"action": "buy", "instrument": {"id_type": "ticker", "id": "SBER"},
              "amount": {"amount": 10_000, "currency": "RUB"}}]
        )

        self.assertTrue(result["ok"])
        data = result["data"]
        for key in ("before", "after", "changed_metrics", "new_risks", "reduced_risks", "goal_impact"):
            self.assertIn(key, data)
        self.assertGreater(
            data["after"]["portfolio_value"]["amount"],
            data["before"]["portfolio_value"]["amount"],
        )
        portfolio_metric = next(m for m in data["changed_metrics"] if m["metric"] == "portfolio_value")
        self.assertGreater(portfolio_metric["after"]["amount"], portfolio_metric["before"]["amount"])

    def test_simulate_rejects_unknown_action(self) -> None:
        service = InvestorService()

        result = service.simulate_action(
            [{"action": "teleport", "instrument": {"id_type": "ticker", "id": "SBER"},
              "amount": {"amount": 100, "currency": "RUB"}}]
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")

    def test_simulate_sell_above_position_value_is_validation_error(self) -> None:
        service = InvestorService()

        result = service.simulate_action(
            [{"action": "sell", "instrument": {"id_type": "ticker", "id": "SBER"},
              "amount": {"amount": 10_000_000, "currency": "RUB"}}]
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")

    def test_sync_status_returns_contract_shape(self) -> None:
        service = InvestorService()
        service.sync_data("full")

        result = service.get_sync_status()

        self.assertTrue(result["ok"])
        self.assertEqual(
            set(result["data"]),
            {"last_success_at", "last_attempt_at", "status", "data_status", "stale_sections"},
        )

    def test_get_operations_filters_and_resource(self) -> None:
        service = InvestorService()

        result = service.get_operations(
            "2026-06-01", "2026-06-30", account_ids=["mock-iis"], operation_types=["coupon"]
        )

        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertTrue(all(op["operation_type"] == "coupon" for op in data["operations"]))
        self.assertTrue(data["resource"].startswith("investor://operations/mock-iis/"))

    def test_get_instrument_accepts_ref_object_and_returns_resource(self) -> None:
        service = InvestorService()

        result = service.get_instrument({"id_type": "ticker", "id": "SBER"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["resource"], "investor://positions/SBER")

    def test_generate_report_json_format_has_period_and_resource(self) -> None:
        service = InvestorService()

        result = service.generate_report("weekly", to_date="2026-06-07", format="json")

        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertEqual(data["resource"], "investor://reports/weekly/2026-06-07")
        self.assertIn("period", data)
        self.assertIn("report", data)

    def test_recommendation_includes_instrument_field(self) -> None:
        service = InvestorService()

        result = service.recommend_next_action({"amount": 50_000, "currency": "RUB"})

        self.assertTrue(all("instrument" in rec for rec in result["data"]["recommendations"]))

    def test_research_instrument_matches_contract_shape(self) -> None:
        service = InvestorService()

        result = service.research_instrument({"id_type": "ticker", "id": "SBER"}, depth="deep")

        self.assertTrue(result["ok"])
        self.assertEqual(result["data_status"], "partial")
        self.assertGreaterEqual(len(result["warnings"]), 1)
        data = result["data"]
        for key in ("research_id", "instrument", "markdown", "key_points", "risks", "portfolio_fit", "resource"):
            self.assertIn(key, data)
        self.assertTrue(data["resource"].startswith("investor://research/"))


if __name__ == "__main__":
    unittest.main()


class _StaticBroker:
    """Minimal broker returning a fixed position list."""

    def __init__(self, positions):
        self._positions = list(positions)

    def list_accounts(self):
        return [Account(account_id="a1", name="Брокерский")]

    def get_positions(self, account_ids=None):
        return list(self._positions)

    def get_operations(self, account_ids=None, from_date=None, to_date=None):
        return []


_OFZ = Instrument("u-ofz", "SU26248RMFS3", "ОФЗ 26248", "bond", issuer="ОФЗ", sector="government", risk_level="low")
_MONEY_MARKET = Instrument("u-tmon", "TMON@", "Денежный рынок", "fund", issuer="Денежный рынок", sector="other", risk_level="low")
_SBER = Instrument("u-sber", "SBER", "Сбербанк", "stock", issuer="Сбербанк", sector="financial")
_FINEX = Instrument("u-fxru", "FXRU", "FinEx Еврооблигации", "fund", issuer="FinEx", sector="other", risk_level="low")
_AGRO = Instrument("u-agro", "AGRO", "РусАгро GDR", "stock", issuer="РусАгро", sector="consumer")


class DataQualityTest(unittest.TestCase):
    def test_get_portfolio_explains_blocked_and_unpriced_positions(self) -> None:
        service = InvestorService(broker=_StaticBroker([
            Position("a1", _OFZ, 100, Money(900), Money(1000)),
            Position("a1", _FINEX, 10, Money(50), Money(40), blocked=True),
            Position("a1", _AGRO, 111, Money(900), Money(0)),  # held, but no price at the broker
        ]))

        result = service.get_portfolio()

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["total_value"]["amount"], 100 * 1000 + 10 * 40)
        self.assertEqual(len(result["warnings"]), 2)
        self.assertIn("FXRU", result["warnings"][0])
        self.assertIn("заблокирован", result["warnings"][0])
        self.assertIn("AGRO×111", result["warnings"][1])
        self.assertIn("не сбой загрузки", result["warnings"][1])
        self.assertIn("warnings", result["summary"])
        flags = {p["instrument"]["ticker"]: p["blocked"] for p in result["data"]["positions"]}
        self.assertEqual(flags, {"SU26248RMFS3": False, "FXRU": True, "AGRO": False})

    def test_get_portfolio_has_no_warnings_for_a_clean_book(self) -> None:
        service = InvestorService(broker=_StaticBroker([Position("a1", _SBER, 10, Money(250), Money(312))]))
        result = service.get_portfolio()
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["summary"], "Портфель получен.")

    def test_sovereign_and_money_market_are_not_issuer_credit_risks(self) -> None:
        service = InvestorService(broker=_StaticBroker([
            Position("a1", _OFZ, 600, Money(1000), Money(1000)),          # 60%
            Position("a1", _MONEY_MARKET, 300, Money(1000), Money(1000)),  # 30%
            Position("a1", _SBER, 100, Money(1000), Money(1000)),          # 10%
        ]))

        risks = service.scan_risks(severity_min="low")["data"]["risk_signals"]
        self.assertEqual([r for r in risks if r["type"] == "issuer"], [])

        digest = service.get_news_digest()["data"]
        self.assertEqual([t["entity"] for t in digest["research_targets"] if t["kind"] == "issuer"], ["Сбербанк"])
        lenses = {lens["lens"] for lens in digest["context_lenses"]}
        self.assertIn("Ставка и ДКП", lenses)
        self.assertNotIn("Кредитный риск / ВДО", lenses)

    def test_corporate_bond_issuer_over_limit_still_flagged(self) -> None:
        samolet = Instrument("u-smlt", "RU000A10BW96", "Самолет П18", "bond", issuer="ГК Самолет", sector="real_estate", risk_level="high")
        service = InvestorService(broker=_StaticBroker([
            Position("a1", samolet, 400, Money(1000), Money(1000)),  # 40% > 15% limit
            Position("a1", _OFZ, 600, Money(1000), Money(1000)),
        ]))
        risks = service.scan_risks(severity_min="low")["data"]["risk_signals"]
        self.assertEqual([r["id"] for r in risks if r["type"] == "issuer"], ["risk_issuer_ГК Самолет"])
        lenses = {lens["lens"] for lens in service.get_news_digest()["data"]["context_lenses"]}
        self.assertIn("Кредитный риск / ВДО", lenses)
