from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Protocol

from .models import Account, Instrument, Money, Operation, Position


class BrokerAdapter(Protocol):
    def list_accounts(self) -> list[Account]:
        ...

    def get_positions(self, account_ids: list[str] | None = None) -> list[Position]:
        ...

    def get_operations(self, account_ids: list[str] | None = None) -> list[Operation]:
        ...


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


# Tinkoff instrument_type -> our asset_class.
_ASSET_CLASS = {
    "share": "stock",
    "bond": "bond",
    "etf": "fund",
    "currency": "currency",
    "futures": "other",
    "option": "other",
    "sp": "other",
}
_RISK_BY_CLASS = {"stock": "medium", "bond": "low", "fund": "low", "currency": "low", "cash": "low"}
# Adapter converts all monetary values to this base currency.
_BASE_CURRENCY = "RUB"


def _money(value: Any) -> float:
    """Convert a Tinkoff MoneyValue/Quotation (units+nano) to float."""
    if value is None:
        return 0.0
    return float(getattr(value, "units", 0)) + float(getattr(value, "nano", 0)) / 1e9


def _date(value: Any) -> str | None:
    """Convert a Tinkoff datetime to an ISO date string; None for unset (epoch 1970)."""
    if value is None:
        return None
    try:
        if getattr(value, "year", 0) <= 1970:
            return None
        return value.date().isoformat() if hasattr(value, "date") else str(value)[:10]
    except Exception:
        return None


def _currency_of(value: Any, fallback: str) -> str:
    currency = getattr(value, "currency", None)
    return (currency or fallback).upper()


def _enum_short(value: Any, prefix: str) -> str:
    """Turn an enum member (e.g. OPERATION_TYPE_BUY) into a short tag ('buy')."""
    name = getattr(value, "name", None) or str(value)
    if name.startswith(prefix):
        name = name[len(prefix):]
    return name.lower().strip("_")


def map_account(raw: Any) -> Account:
    account_type = _enum_short(getattr(raw, "type", ""), "ACCOUNT_TYPE_")
    type_map = {"tinkoff": "brokerage", "tinkoff_iis": "iis", "invest_box": "brokerage"}
    status = _enum_short(getattr(raw, "status", ""), "ACCOUNT_STATUS_") or "open"
    return Account(
        account_id=str(getattr(raw, "id", "")),
        name=getattr(raw, "name", "") or str(getattr(raw, "id", "")),
        type=type_map.get(account_type, "brokerage"),
        status="open" if status in {"open", ""} else status,
    )


def map_instrument(pos: Any, instr: Any | None, issuer: str | None = None) -> Instrument:
    instrument_type = getattr(pos, "instrument_type", "") or (getattr(instr, "instrument_type", "") if instr else "")
    asset_class = _ASSET_CLASS.get(instrument_type, "other")
    if instr is not None:
        instrument_id = getattr(instr, "uid", "") or getattr(instr, "figi", "")
        ticker = getattr(instr, "ticker", "") or instrument_id
        name = getattr(instr, "name", "") or ticker
        currency = (getattr(instr, "currency", "") or "rub").upper()
        sector = getattr(instr, "sector", "") or "unknown"
    else:
        instrument_id = getattr(pos, "instrument_uid", "") or getattr(pos, "figi", "")
        ticker = getattr(pos, "ticker", "") or instrument_id
        name = ticker
        currency = "RUB"
        sector = "unknown"
    # Tinkoff reports RUB cash as a currency position; surface it as our "cash" class.
    if asset_class == "currency" and ticker.upper() in {"RUB", "RUB000UTSTOM"}:
        asset_class = "cash"
    # Bonds carry an explicit risk_level enum; everything else uses a class default.
    risk_level = _RISK_BY_CLASS.get(asset_class, "medium")
    if instr is not None:
        bond_risk = _enum_short(getattr(instr, "risk_level", None), "RISK_LEVEL_")
        risk_level = {"low": "low", "moderate": "medium", "high": "high"}.get(bond_risk, risk_level)
    # Issuer = brand/company name (shared across an issuer's instruments, e.g. all
    # ГК Самолет bond series) so concentration groups correctly. Fall back to the
    # instrument name when the brand is unavailable.
    return Instrument(
        instrument_id=instrument_id,
        ticker=ticker,
        name=name,
        asset_class=asset_class,
        currency=currency,
        issuer=issuer or name,
        sector=sector,
        risk_level=risk_level,
    )


def map_position(
    account_id: str,
    pos: Any,
    instrument: Instrument,
    fx: dict[str, float] | None = None,
) -> Position:
    """Map a Tinkoff PortfolioPosition to our Position, valued in the base currency.

    Prices come from the API in the instrument's quote currency; we convert to
    ``_BASE_CURRENCY`` with ``fx`` (RUB per 1 unit). For bonds the accrued coupon
    (``current_nkd``) is added to the price so the value matches the broker total.
    The instrument keeps its original currency for currency-exposure analysis.
    """
    fx = fx or {}
    quantity = _money(getattr(pos, "quantity", None))
    average = getattr(pos, "average_position_price", None)
    current = getattr(pos, "current_price", None)
    quote_currency = _currency_of(current or average, instrument.currency)
    rate = fx.get(quote_currency, 1.0)
    nkd = _money(getattr(pos, "current_nkd", None)) if getattr(pos, "instrument_type", "") == "bond" else 0.0
    current_per_unit = (_money(current) + nkd) * rate
    average_per_unit = _money(average) * rate
    return Position(
        account_id=account_id,
        instrument=instrument,
        quantity=quantity,
        average_price=Money(average_per_unit, _BASE_CURRENCY),
        current_price=Money(current_per_unit, _BASE_CURRENCY),
    )


def map_operation(account_id: str, item: Any) -> Operation:
    payment = getattr(item, "payment", None)
    currency = _currency_of(payment, "RUB")
    raw_date = getattr(item, "date", None)
    if hasattr(raw_date, "date"):
        date = raw_date.date().isoformat()
    else:
        date = str(raw_date)[:10]
    # Cursor API (OperationItem) carries the enum in `type`; the legacy Operation
    # class uses `operation_type` (and `type` is a plain string there).
    op_enum = getattr(item, "operation_type", None)
    if op_enum is None:
        op_enum = getattr(item, "type", None)
    return Operation(
        operation_id=str(getattr(item, "id", "")),
        account_id=account_id,
        date=date,
        operation_type=_enum_short(op_enum, "OPERATION_TYPE_"),
        instrument_id=getattr(item, "instrument_uid", None) or getattr(item, "figi", None),
        quantity=float(getattr(item, "quantity", 0) or 0),
        amount=Money(_money(payment), currency),
        description=getattr(item, "name", "") or getattr(item, "description", "") or "",
    )


class TinkoffInvestAdapter:
    """Read-only Tinkoff Invest API adapter built on the ``invest-python`` SDK.

    Only read methods are ever called (accounts, portfolio, operations,
    instruments, prices). No order-placing methods are used. ``client_factory``
    is injectable so the mapping can be unit-tested without the SDK or network.
    """

    def __init__(
        self,
        token: str,
        *,
        sandbox: bool = False,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.token = token
        self.sandbox = sandbox
        self._client_factory = client_factory
        self._instrument_cache: dict[str, Instrument] = {}
        self._brand_cache: dict[str, str | None] = {}
        self._bond_cache: dict[str, dict[str, Any]] = {}

    def _open(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        from tinkoff.invest import Client
        from tinkoff.invest.constants import INVEST_GRPC_API_SANDBOX

        if self.sandbox:
            return Client(self.token, target=INVEST_GRPC_API_SANDBOX)
        return Client(self.token)

    def _raw_accounts(self, client: Any) -> list[Any]:
        if self.sandbox:
            return list(client.sandbox.get_sandbox_accounts().accounts)
        return list(client.users.get_accounts().accounts)

    def _portfolio(self, client: Any, account_id: str) -> Any:
        if self.sandbox:
            return client.sandbox.get_sandbox_portfolio(account_id=account_id)
        return client.operations.get_portfolio(account_id=account_id)

    def _fx_rates(self, client: Any) -> dict[str, float]:
        """Build {ISO currency -> RUB per 1 unit} from the currencies catalog + last prices."""
        rates = {_BASE_CURRENCY: 1.0}
        try:
            currencies = client.instruments.currencies().instruments
            iso_by_uid: dict[str, tuple[str, float]] = {}
            uids = []
            for currency in currencies:
                iso = (getattr(currency, "iso_currency_name", "") or "").upper()
                nominal = _money(getattr(currency, "nominal", None)) or 1.0
                iso_by_uid[currency.uid] = (iso, nominal)
                uids.append(currency.uid)
            for last in client.market_data.get_last_prices(instrument_id=uids).last_prices:
                iso, nominal = iso_by_uid.get(last.instrument_uid, ("", 1.0))
                price = _money(getattr(last, "price", None))
                if iso and price:
                    rates[iso] = price / nominal
        except Exception:
            pass
        return rates

    def _instrument_meta(self, client: Any, pos: Any) -> Instrument:
        uid = getattr(pos, "instrument_uid", "") or getattr(pos, "figi", "")
        if uid in self._instrument_cache:
            return self._instrument_cache[uid]
        instr = None
        try:
            from tinkoff.invest import InstrumentIdType

            uid_type = InstrumentIdType.INSTRUMENT_ID_TYPE_UID
            instrument_type = getattr(pos, "instrument_type", "")
            instruments = client.instruments
            # Typed lookups carry `sector` (and bonds `risk_level`); the unified
            # get_instrument_by does not, so use it only as a fallback.
            if instrument_type == "share":
                instr = instruments.share_by(id_type=uid_type, id=uid).instrument
            elif instrument_type == "bond":
                instr = instruments.bond_by(id_type=uid_type, id=uid).instrument
            elif instrument_type == "etf":
                instr = instruments.etf_by(id_type=uid_type, id=uid).instrument
            else:
                instr = instruments.get_instrument_by(id_type=uid_type, id=uid).instrument
        except Exception:
            instr = None
        issuer = self._brand_name(client, getattr(instr, "asset_uid", "")) if instr is not None else None
        meta = map_instrument(pos, instr, issuer=issuer)
        self._instrument_cache[uid] = meta
        return meta

    def _brand_name(self, client: Any, asset_uid: str) -> str | None:
        """Resolve an instrument's issuer/brand name via the Asset API (cached).

        All instruments of one issuer share a brand (e.g. every ГК Самолет bond
        series), so this groups issuer concentration correctly. asset_uid would NOT
        group them — bond series have distinct assets but the same brand.
        """
        if not asset_uid:
            return None
        if asset_uid in self._brand_cache:
            return self._brand_cache[asset_uid]
        name: str | None = None
        try:
            asset = client.instruments.get_asset_by(id=asset_uid).asset
            name = (getattr(asset.brand, "name", "") or "").strip() or None
        except Exception:
            name = None
        self._brand_cache[asset_uid] = name
        return name

    def list_accounts(self) -> list[Account]:
        with self._open() as client:
            return [map_account(account) for account in self._raw_accounts(client)]

    def get_positions(self, account_ids: list[str] | None = None) -> list[Position]:
        with self._open() as client:
            fx = self._fx_rates(client)
            ids = account_ids or [str(getattr(a, "id", "")) for a in self._raw_accounts(client)]
            positions: list[Position] = []
            for account_id in ids:
                portfolio = self._portfolio(client, account_id)
                for pos in portfolio.positions:
                    instrument = self._instrument_meta(client, pos)
                    positions.append(map_position(account_id, pos, instrument, fx))
            return positions

    @staticmethod
    def _ops_request(**kwargs: Any) -> Any:
        try:
            from tinkoff.invest import GetOperationsByCursorRequest

            return GetOperationsByCursorRequest(**kwargs)
        except ImportError:
            # No SDK available (e.g. unit tests / Python 3.13): a plain object is
            # enough for an injected fake client.
            return SimpleNamespace(**kwargs)

    def get_operations(
        self,
        account_ids: list[str] | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> list[Operation]:
        to_dt = datetime.now(timezone.utc)
        from_dt = to_dt - timedelta(days=365)
        with self._open() as client:
            ids = account_ids or [str(getattr(a, "id", "")) for a in self._raw_accounts(client)]
            operations: list[Operation] = []
            for account_id in ids:
                cursor = ""
                while True:
                    request = self._ops_request(
                        account_id=account_id,
                        from_=from_dt,
                        to=to_dt,
                        cursor=cursor,
                        limit=1000,
                    )
                    if self.sandbox:
                        response = client.sandbox.get_sandbox_operations_by_cursor(request)
                    else:
                        response = client.operations.get_operations_by_cursor(request)
                    for item in response.items:
                        operations.append(map_operation(account_id, item))
                    if not getattr(response, "has_next", False):
                        break
                    cursor = response.next_cursor
            return operations

    def get_bond_data(self, instrument_uids: list[str]) -> dict[str, dict[str, Any]]:
        """Per-bond schedule for the bond calendar: maturity, offer, coupons (cached).

        Coupons come from get_bond_coupons (pay_one_bond per coupon); maturity/offer
        from the bond card. Amounts are per ONE bond; the service scales by quantity.
        """
        missing = [uid for uid in instrument_uids if uid not in self._bond_cache]
        if missing:
            from tinkoff.invest import InstrumentIdType

            uid_type = InstrumentIdType.INSTRUMENT_ID_TYPE_UID
            now = datetime.now(timezone.utc)
            horizon = now + timedelta(days=370 * 5)  # up to ~5y of coupons
            with self._open() as client:
                for uid in missing:
                    try:
                        bond = client.instruments.bond_by(id_type=uid_type, id=uid).instrument
                        coupons: list[dict[str, Any]] = []
                        try:
                            response = client.instruments.get_bond_coupons(
                                instrument_id=uid, from_=now, to=horizon
                            )
                            for event in response.events:
                                coupons.append(
                                    {"date": _date(event.coupon_date), "amount_per_bond": _money(event.pay_one_bond)}
                                )
                        except Exception:
                            coupons = []
                        nominal = getattr(bond, "nominal", None)
                        self._bond_cache[uid] = {
                            "maturity_date": _date(getattr(bond, "maturity_date", None)),
                            "offer_date": _date(getattr(bond, "call_date", None)),
                            "nominal": _money(nominal),
                            "currency": (getattr(nominal, "currency", "") or _BASE_CURRENCY).upper(),
                            "coupon_quantity_per_year": int(getattr(bond, "coupon_quantity_per_year", 0) or 0),
                            "amortization": bool(getattr(bond, "amortization_flag", False)),
                            "perpetual": bool(getattr(bond, "perpetual_flag", False)),
                            "floating": bool(getattr(bond, "floating_coupon_flag", False)),
                            "coupons": [c for c in coupons if c["date"]],
                        }
                    except Exception:
                        self._bond_cache[uid] = {
                            "maturity_date": None, "offer_date": None, "nominal": 0.0,
                            "currency": _BASE_CURRENCY, "coupon_quantity_per_year": 0,
                            "amortization": False, "perpetual": False, "floating": False, "coupons": [],
                        }
        return {uid: self._bond_cache[uid] for uid in instrument_uids}


def build_broker_adapter() -> BrokerAdapter:
    """Pick the broker adapter from environment.

    Uses the real Tinkoff adapter when ``TINKOFF_INVEST_TOKEN`` is set,
    otherwise the read-only mock for local development.
    """
    token = os.getenv("TINKOFF_INVEST_TOKEN")
    if not token:
        return MockBrokerAdapter()
    sandbox = os.getenv("TINKOFF_INVEST_SANDBOX", "").lower() in {"1", "true", "yes"}
    return TinkoffInvestAdapter(token, sandbox=sandbox)

