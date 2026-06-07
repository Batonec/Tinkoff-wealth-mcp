from __future__ import annotations

import os
import unittest
from datetime import datetime
from types import SimpleNamespace

from investor_mcp.adapters import (
    MockBrokerAdapter,
    TinkoffInvestAdapter,
    _money,
    build_broker_adapter,
    map_account,
    map_instrument,
    map_operation,
    map_position,
)


def money(units: int, nano: int = 0, currency: str = "rub") -> SimpleNamespace:
    return SimpleNamespace(units=units, nano=nano, currency=currency)


def quotation(units: int, nano: int = 0) -> SimpleNamespace:
    return SimpleNamespace(units=units, nano=nano)


class FakeClient:
    def __init__(self, accounts=None, portfolios=None, op_pages=None) -> None:
        self._accounts = accounts or []
        self._portfolios = portfolios or {}
        self._op_pages = list(op_pages or [])
        self._op_index = 0
        self.users = SimpleNamespace(get_accounts=lambda: SimpleNamespace(accounts=self._accounts))
        self.operations = SimpleNamespace(
            get_portfolio=lambda account_id: self._portfolios[account_id],
            get_operations_by_cursor=self._next_ops_page,
        )
        self.instruments = SimpleNamespace(
            get_instrument_by=lambda **kwargs: SimpleNamespace(instrument=None)
        )

    def _next_ops_page(self, request):
        page = self._op_pages[self._op_index]
        self._op_index += 1
        return page

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class MappingTest(unittest.TestCase):
    def test_money_units_and_nano(self) -> None:
        self.assertEqual(_money(money(250, 500_000_000)), 250.5)
        self.assertEqual(_money(None), 0.0)

    def test_map_account_iis(self) -> None:
        raw = SimpleNamespace(
            id="acc-1",
            name="IIS",
            type=SimpleNamespace(name="ACCOUNT_TYPE_TINKOFF_IIS"),
            status=SimpleNamespace(name="ACCOUNT_STATUS_OPEN"),
        )
        account = map_account(raw)
        self.assertEqual(account.account_id, "acc-1")
        self.assertEqual(account.type, "iis")
        self.assertEqual(account.status, "open")

    def test_map_instrument_uses_position_fallback_when_no_metadata(self) -> None:
        pos = SimpleNamespace(instrument_uid="uid-sber", figi="BBG", instrument_type="share", ticker="SBER")
        instrument = map_instrument(pos, None)
        self.assertEqual(instrument.ticker, "SBER")
        self.assertEqual(instrument.asset_class, "stock")

    def test_map_instrument_rub_currency_becomes_cash(self) -> None:
        pos = SimpleNamespace(instrument_uid="uid-rub", figi="RUB", instrument_type="currency", ticker="RUB")
        instrument = map_instrument(pos, None)
        self.assertEqual(instrument.asset_class, "cash")

    def test_map_operation(self) -> None:
        # OperationItem (cursor API): enum is in `type`, no `operation_type`.
        item = SimpleNamespace(
            id="op-1",
            date=datetime(2026, 6, 1, 12, 0, 0),
            type=SimpleNamespace(name="OPERATION_TYPE_COUPON"),
            instrument_uid="uid-ofz",
            figi="OFZ",
            payment=money(1250),
            quantity=0,
            name="Купон",
        )
        operation = map_operation("acc-1", item)
        self.assertEqual(operation.operation_type, "coupon")
        self.assertEqual(operation.description, "Купон")
        self.assertEqual(operation.date, "2026-06-01")
        self.assertEqual(operation.amount.amount, 1250.0)


class TinkoffAdapterTest(unittest.TestCase):
    def test_list_accounts_via_fake_client(self) -> None:
        client = FakeClient(
            accounts=[
                SimpleNamespace(
                    id="acc-1",
                    name="Brokerage",
                    type=SimpleNamespace(name="ACCOUNT_TYPE_TINKOFF"),
                    status=SimpleNamespace(name="ACCOUNT_STATUS_OPEN"),
                )
            ]
        )
        adapter = TinkoffInvestAdapter("token", client_factory=lambda: client)
        accounts = adapter.list_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].type, "brokerage")

    def test_get_positions_maps_and_caches(self) -> None:
        pos = SimpleNamespace(
            instrument_uid="uid-sber",
            figi="BBG",
            instrument_type="share",
            ticker="SBER",
            quantity=quotation(10),
            average_position_price=money(250),
            current_price=money(312),
        )
        client = FakeClient(portfolios={"acc-1": SimpleNamespace(positions=[pos])})
        adapter = TinkoffInvestAdapter("token", client_factory=lambda: client)

        positions = adapter.get_positions(["acc-1"])

        self.assertEqual(len(positions), 1)
        position = positions[0]
        self.assertEqual(position.account_id, "acc-1")
        self.assertEqual(position.instrument.ticker, "SBER")
        self.assertEqual(position.quantity, 10.0)
        self.assertEqual(position.current_value.amount, 3120.0)

    def test_map_position_converts_foreign_currency(self) -> None:
        instrument = SimpleNamespace(currency="USD")
        pos = SimpleNamespace(
            instrument_type="share",
            quantity=quotation(1),
            average_position_price=money(200, currency="usd"),
            current_price=money(253, 790_000_000, currency="usd"),
        )
        from investor_mcp.adapters import Instrument

        instr = Instrument(
            instrument_id="amzn", ticker="AMZN", name="Amazon", asset_class="stock",
            currency="USD", issuer="Amazon", sector="unknown", risk_level="medium",
        )
        position = map_position("acc-1", pos, instr, fx={"USD": 80.0})
        # 253.79 USD * 80 RUB/USD = 20303.2
        self.assertAlmostEqual(position.current_value.amount, 253.79 * 80.0, places=2)
        self.assertEqual(position.current_price.currency, "RUB")
        # original currency preserved on the instrument for exposure analysis
        self.assertEqual(position.instrument.currency, "USD")

    def test_map_instrument_reads_sector_and_bond_risk(self) -> None:
        bond_instr = SimpleNamespace(
            uid="u-ofz", figi="f", ticker="OFZ", name="OFZ 26243", currency="rub",
            sector="government", instrument_type="bond",
            risk_level=SimpleNamespace(name="RISK_LEVEL_LOW"),
        )
        bond_pos = SimpleNamespace(instrument_uid="u-ofz", figi="f", instrument_type="bond", ticker="OFZ")
        bond = map_instrument(bond_pos, bond_instr)
        self.assertEqual(bond.sector, "government")
        self.assertEqual(bond.risk_level, "low")

        share_instr = SimpleNamespace(
            uid="u-sber", ticker="SBER", name="Sber", currency="rub",
            sector="financial", instrument_type="share", risk_level=None,
        )
        share_pos = SimpleNamespace(instrument_uid="u-sber", instrument_type="share", ticker="SBER")
        share = map_instrument(share_pos, share_instr)
        self.assertEqual(share.sector, "financial")
        self.assertEqual(share.risk_level, "medium")  # shares have no risk_level -> class default

    def test_map_instrument_uses_issuer_override(self) -> None:
        pos = SimpleNamespace(instrument_uid="u", figi="f", instrument_type="bond", ticker="RU000A10BW96")
        instr = SimpleNamespace(
            uid="u", ticker="RU000A10BW96", name="Самолет БО-П18", currency="rub",
            sector="real_estate", instrument_type="bond", risk_level=None, asset_uid="a1",
        )
        result = map_instrument(pos, instr, issuer="ГК Самолет")
        self.assertEqual(result.issuer, "ГК Самолет")  # not the per-issue name

    def test_issuer_grouped_by_brand_across_bond_series(self) -> None:
        ns = SimpleNamespace

        def mv(u, c="rub"):
            return ns(units=u, nano=0, currency=c)

        def pos(uid):
            return ns(instrument_uid=uid, figi=uid, instrument_type="bond", ticker=uid,
                      quantity=ns(units=1, nano=0), average_position_price=mv(1000),
                      current_price=mv(1000), current_nkd=mv(0))

        bond_asset = {"u-p18": "a1", "u-p16": "a2"}  # different assets...
        asset_brand = {"a1": "ГК Самолет", "a2": "ГК Самолет"}  # ...same brand

        class FakeAssetClient:
            instruments = ns(
                bond_by=lambda id_type=None, id=None: ns(instrument=ns(
                    uid=id, ticker=id, name=f"Самолет {id}", currency="rub",
                    sector="real_estate", instrument_type="bond", risk_level=None,
                    asset_uid=bond_asset[id])),
                get_asset_by=lambda id=None: ns(asset=ns(brand=ns(name=asset_brand[id]))),
                currencies=lambda: ns(instruments=[]),
            )
            market_data = ns(get_last_prices=lambda instrument_id=None: ns(last_prices=[]))
            operations = ns(get_portfolio=lambda account_id=None: ns(positions=[pos("u-p18"), pos("u-p16")]))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        adapter = TinkoffInvestAdapter("t", client_factory=lambda: FakeAssetClient())
        positions = adapter.get_positions(["acc-1"])
        self.assertEqual(len(positions), 2)
        self.assertEqual({p.instrument.issuer for p in positions}, {"ГК Самолет"})

    def test_map_position_adds_bond_nkd(self) -> None:
        from investor_mcp.adapters import Instrument

        instr = Instrument(
            instrument_id="ofz", ticker="OFZ", name="OFZ", asset_class="bond",
            currency="RUB", issuer="MinFin", sector="government", risk_level="low",
        )
        pos = SimpleNamespace(
            instrument_type="bond",
            quantity=quotation(10),
            average_position_price=money(900),
            current_price=money(950),
            current_nkd=money(20),
        )
        position = map_position("acc-1", pos, instr)
        # (950 + 20 ACI) * 10 = 9700
        self.assertEqual(position.current_value.amount, 9700.0)

    def test_get_operations_paginates(self) -> None:
        item = SimpleNamespace(
            id="op-1",
            date=datetime(2026, 6, 1),
            type=SimpleNamespace(name="OPERATION_TYPE_BUY"),
            instrument_uid="uid",
            figi="fg",
            payment=money(-3000),
            quantity=10,
            name="Buy",
        )
        pages = [
            SimpleNamespace(items=[item], has_next=True, next_cursor="c2"),
            SimpleNamespace(items=[item], has_next=False, next_cursor=""),
        ]
        client = FakeClient(op_pages=pages)
        adapter = TinkoffInvestAdapter("token", client_factory=lambda: client)

        operations = adapter.get_operations(["acc-1"])

        self.assertEqual(len(operations), 2)
        self.assertEqual(operations[0].operation_type, "buy")


class MockBondDataTest(unittest.TestCase):
    def test_mock_get_bond_data(self) -> None:
        data = MockBrokerAdapter().get_bond_data(["OFZ26243", "SBER"])
        self.assertTrue(data["OFZ26243"]["coupons"])
        self.assertEqual(data["OFZ26243"]["coupon_quantity_per_year"], 2)
        self.assertEqual(data["SBER"]["coupons"], [])


class BuildBrokerAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in ("TINKOFF_INVEST_TOKEN", "TINKOFF_INVEST_SANDBOX")}

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_without_token_returns_mock(self) -> None:
        os.environ.pop("TINKOFF_INVEST_TOKEN", None)
        self.assertIsInstance(build_broker_adapter(), MockBrokerAdapter)

    def test_with_token_returns_tinkoff(self) -> None:
        os.environ["TINKOFF_INVEST_TOKEN"] = "secret"
        os.environ["TINKOFF_INVEST_SANDBOX"] = "true"
        adapter = build_broker_adapter()
        self.assertIsInstance(adapter, TinkoffInvestAdapter)
        self.assertTrue(adapter.sandbox)


if __name__ == "__main__":
    unittest.main()
