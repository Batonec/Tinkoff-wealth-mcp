"""Read-only mock broker adapter with canned data for local MCP development."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..models import Account, Instrument, Money, Operation, Position


@dataclass
class MockBrokerAdapter:
    """Read-only mock broker data for local MCP development."""

    def __post_init__(self) -> None:
        self.accounts = [
            Account(account_id="mock-brokerage", name="Brokerage", type="brokerage"),
            Account(account_id="mock-iis", name="IIS", type="iis"),
        ]
        self.instruments = {
            "SBER": Instrument(
                instrument_id="SBER",
                ticker="SBER",
                name="Sber",
                asset_class="stock",
                issuer="Sber",
                sector="financials",
                risk_level="medium",
            ),
            "OFZ26243": Instrument(
                instrument_id="OFZ26243",
                ticker="SU26243RMFS4",
                name="OFZ 26243",
                asset_class="bond",
                issuer="MinFin",
                sector="government",
                risk_level="low",
            ),
            "LQDT": Instrument(
                instrument_id="LQDT",
                ticker="LQDT",
                name="Liquidity fund",
                asset_class="fund",
                issuer="T-Bank",
                sector="money_market",
                risk_level="low",
            ),
            "RUB": Instrument(
                instrument_id="RUB",
                ticker="RUB",
                name="Russian ruble cash",
                asset_class="cash",
                issuer="Cash",
                sector="cash",
                risk_level="low",
            ),
        }
        self.positions = [
            Position("mock-brokerage", self.instruments["SBER"], 120, Money(250), Money(312)),
            Position("mock-brokerage", self.instruments["OFZ26243"], 85, Money(915), Money(934)),
            Position("mock-iis", self.instruments["LQDT"], 300, Money(1_000), Money(1_003)),
            Position("mock-iis", self.instruments["RUB"], 75_000, Money(1), Money(1)),
        ]
        self.operations = [
            Operation(
                operation_id="op-001",
                account_id="mock-brokerage",
                date="2026-06-01",
                operation_type="buy",
                instrument_id="SBER",
                quantity=10,
                amount=Money(-3_000),
                description="Mock SBER buy",
            ),
            Operation(
                operation_id="op-002",
                account_id="mock-iis",
                date="2026-06-03",
                operation_type="coupon",
                instrument_id="OFZ26243",
                quantity=0,
                amount=Money(1_250),
                description="Mock coupon",
            ),
        ]

    def list_accounts(self) -> list[Account]:
        return list(self.accounts)

    def get_positions(self, account_ids: list[str] | None = None) -> list[Position]:
        if not account_ids:
            return list(self.positions)
        account_set = set(account_ids)
        return [position for position in self.positions if position.account_id in account_set]

    def get_operations(self, account_ids: list[str] | None = None) -> list[Operation]:
        if not account_ids:
            return list(self.operations)
        account_set = set(account_ids)
        return [operation for operation in self.operations if operation.account_id in account_set]

    def get_bond_data(self, instrument_uids: list[str]) -> dict[str, dict]:
        today = datetime.now(timezone.utc).date()
        result: dict[str, dict] = {}
        for uid in instrument_uids:
            if uid == "OFZ26243":
                result[uid] = {
                    "maturity_date": (today + timedelta(days=730)).isoformat(),
                    "offer_date": None,
                    "nominal": 1000.0,
                    "currency": "RUB",
                    "coupon_quantity_per_year": 2,
                    "amortization": False,
                    "perpetual": False,
                    "floating": False,
                    "coupons": [
                        {"date": (today + timedelta(days=30)).isoformat(), "amount_per_bond": 34.9},
                        {"date": (today + timedelta(days=212)).isoformat(), "amount_per_bond": 34.9},
                        {"date": (today + timedelta(days=394)).isoformat(), "amount_per_bond": 34.9},
                    ],
                }
            else:
                result[uid] = {
                    "maturity_date": None, "offer_date": None, "nominal": 0.0, "currency": "RUB",
                    "coupon_quantity_per_year": 0, "amortization": False, "perpetual": False,
                    "floating": False, "coupons": [],
                }
        return result

    def get_dividend_data(self, instrument_uids: list[str]) -> dict[str, dict]:
        return {
            uid: {"annual_dividend_per_share": 30.0 if uid == "SBER" else 0.0}
            for uid in instrument_uids
        }
