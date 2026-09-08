"""Read-only Tinkoff Invest API adapter and the Tinkoff->domain mapping helpers."""

from __future__ import annotations

import collections
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from ..errors import describe_error
from ..models import Account, Instrument, Money, Operation, Position

logger = logging.getLogger(__name__)

# Per-RPC deadline (seconds). Tinkoff RPCs normally finish well under a second, so
# this only bites when the API is unreachable (network / TLS): it bounds how long a
# tool call can hang, so the MCP client (~60 s budget) gets an honest stale answer
# instead of a client-side timeout. 0 / None disables it.
DEFAULT_TIMEOUT_SECONDS = 20.0

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


def _uid_id_type() -> Any:
    """The UID InstrumentIdType enum, or None when the SDK isn't installed.

    None is fine: the real typed lookups only run with the SDK present; without it
    (unit tests / CI) an injected fake client ignores the id_type argument.
    """
    try:
        from tinkoff.invest import InstrumentIdType

        return InstrumentIdType.INSTRUMENT_ID_TYPE_UID
    except ImportError:
        return None


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


def _deadline_interceptor(timeout: float) -> Any:
    """gRPC client interceptor that puts ``timeout`` on every unary call lacking one.

    The SDK issues its calls without a deadline, so with the API unreachable a call
    can hang for as long as gRPC keeps retrying the connection.
    """
    import grpc

    class _Details(
        collections.namedtuple("_Details", "method timeout metadata credentials wait_for_ready compression"),
        grpc.ClientCallDetails,
    ):
        pass

    class _Deadline(grpc.UnaryUnaryClientInterceptor):
        def intercept_unary_unary(self, continuation, client_call_details, request):  # type: ignore[override]
            details = client_call_details
            if details.timeout is None:
                details = _Details(
                    details.method,
                    timeout,
                    details.metadata,
                    details.credentials,
                    details.wait_for_ready,
                    getattr(details, "compression", None),
                )
            return continuation(details, request)

    return _Deadline()


class _TrustedClient:
    """``tinkoff.invest.Client`` look-alike with an explicit root-CA bundle.

    The SDK builds its channel with gRPC's default roots, which (like most trust
    stores outside Russia) do not include the Ministry of Digital Development
    (Минцифры) root that T-Bank moved its API certificates to in August 2026. This
    client hands the PEM bundle from ``TINKOFF_INVEST_CA_BUNDLE`` straight to gRPC.
    """

    def __init__(self, token: str, *, target: str, root_certificates: bytes, interceptors: list[Any]) -> None:
        import grpc

        options = [("grpc.max_receive_message_length", 10 * 1024 * 1024)]  # same as the SDK
        credentials = grpc.ssl_channel_credentials(root_certificates=root_certificates)
        channel = grpc.secure_channel(target, credentials, options)
        for interceptor in interceptors:
            channel = grpc.intercept_channel(channel, interceptor)
        self._channel = channel
        self._token = token

    def __enter__(self) -> Any:
        from tinkoff.invest.services import Services

        return Services(self._channel.__enter__(), token=self._token)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._channel.__exit__(exc_type, exc, tb)
        return False


class TinkoffInvestAdapter:
    """Read-only Tinkoff Invest API adapter built on the ``invest-python`` SDK.

    Only read methods are ever called (accounts, portfolio, operations,
    instruments, prices). No order-placing methods are used. ``client_factory``
    is injectable so the mapping can be unit-tested without the SDK or network.

    Transport knobs: ``target`` overrides the gRPC endpoint (``host[:port]``),
    ``ca_bundle`` is a PEM file of root CAs to trust instead of gRPC's defaults,
    ``timeout_seconds`` is the per-RPC deadline (``None``/0 = no deadline).
    """

    def __init__(
        self,
        token: str,
        *,
        sandbox: bool = False,
        client_factory: Callable[[], Any] | None = None,
        target: str | None = None,
        ca_bundle: str | None = None,
        timeout_seconds: float | None = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.token = token
        self.sandbox = sandbox
        self.target = target
        self.ca_bundle = ca_bundle
        self.timeout_seconds = timeout_seconds
        self._client_factory = client_factory
        self._root_certificates: bytes | None = None
        self._instrument_cache: dict[str, Instrument] = {}
        self._brand_cache: dict[str, str | None] = {}
        self._bond_cache: dict[str, dict[str, Any]] = {}
        self._dividend_cache: dict[str, dict[str, Any]] = {}

    def _open(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        from tinkoff.invest import Client
        from tinkoff.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX

        target = self.target or (INVEST_GRPC_API_SANDBOX if self.sandbox else INVEST_GRPC_API)
        interceptors = [_deadline_interceptor(self.timeout_seconds)] if self.timeout_seconds else []
        if self.ca_bundle:
            if self._root_certificates is None:
                self._root_certificates = Path(self.ca_bundle).expanduser().read_bytes()
            return _TrustedClient(
                self.token, target=target, root_certificates=self._root_certificates, interceptors=interceptors
            )
        return Client(self.token, target=target, interceptors=interceptors)

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
        except Exception as exc:  # noqa: BLE001 — FX is best effort; positions still load
            logger.warning("FX rates unavailable, foreign positions valued 1:1: %s", describe_error(exc))
        return rates

    def _instrument_meta(self, client: Any, pos: Any) -> Instrument:
        uid = getattr(pos, "instrument_uid", "") or getattr(pos, "figi", "")
        if uid in self._instrument_cache:
            return self._instrument_cache[uid]
        instr = None
        try:
            uid_type = _uid_id_type()
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
        except Exception as exc:  # noqa: BLE001 — fall back to the bare position data
            logger.debug("Instrument lookup failed for %s: %s", uid, describe_error(exc))
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
        except Exception as exc:  # noqa: BLE001 — issuer falls back to the instrument name
            logger.debug("Brand lookup failed for asset %s: %s", asset_uid, describe_error(exc))
            name = None
        self._brand_cache[asset_uid] = name
        return name

    def list_accounts(self) -> list[Account]:
        with self._open() as client:
            return [map_account(account) for account in self._raw_accounts(client)]

    def get_positions(self, account_ids: list[str] | None = None) -> list[Position]:
        with self._open() as client:
            # Accounts first: when the API is unreachable this fails fast (one deadline)
            # instead of first burning a deadline inside the best-effort FX lookup.
            ids = account_ids or [str(getattr(a, "id", "")) for a in self._raw_accounts(client)]
            fx = self._fx_rates(client)
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
            uid_type = _uid_id_type()
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
                    except Exception as exc:  # noqa: BLE001 — one bad bond must not sink the calendar
                        logger.warning("Bond data unavailable for %s: %s", uid, describe_error(exc))
                        self._bond_cache[uid] = {
                            "maturity_date": None, "offer_date": None, "nominal": 0.0,
                            "currency": _BASE_CURRENCY, "coupon_quantity_per_year": 0,
                            "amortization": False, "perpetual": False, "floating": False, "coupons": [],
                        }
        return {uid: self._bond_cache[uid] for uid in instrument_uids}

    def get_dividend_data(self, instrument_uids: list[str]) -> dict[str, dict[str, Any]]:
        """Trailing-12m dividends per share, converted to base currency (cached).

        A reasonable proxy for forward annual dividend income for the goal tracker.
        """
        missing = [uid for uid in instrument_uids if uid not in self._dividend_cache]
        if missing:
            now = datetime.now(timezone.utc)
            since = now - timedelta(days=380)
            with self._open() as client:
                fx = self._fx_rates(client)
                for uid in missing:
                    annual = 0.0
                    try:
                        response = client.instruments.get_dividends(
                            instrument_id=uid, from_=since, to=now + timedelta(days=5)
                        )
                        for dividend in response.dividends:
                            net = getattr(dividend, "dividend_net", None)
                            currency = (getattr(net, "currency", "") or _BASE_CURRENCY).upper()
                            annual += _money(net) * fx.get(currency, 1.0)
                    except Exception as exc:  # noqa: BLE001 — missing dividends just count as 0
                        logger.warning("Dividend data unavailable for %s: %s", uid, describe_error(exc))
                        annual = 0.0
                    self._dividend_cache[uid] = {"annual_dividend_per_share": round(annual, 4)}
        return {uid: self._dividend_cache[uid] for uid in instrument_uids}

