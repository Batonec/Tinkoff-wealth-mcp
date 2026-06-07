from __future__ import annotations

import unittest

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

