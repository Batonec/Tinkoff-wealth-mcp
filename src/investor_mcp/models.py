"""Normalized domain models: Money, Account, Instrument, Position, Operation, profile."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Money:
    amount: float
    currency: str = "RUB"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Money":
        return cls(float(data["amount"]), data.get("currency", "RUB"))


@dataclass(frozen=True)
class Account:
    account_id: str
    name: str
    type: str = "brokerage"
    status: str = "open"
    included_in_analysis: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Account":
        return cls(
            account_id=data["account_id"],
            name=data.get("name", ""),
            type=data.get("type", "brokerage"),
            status=data.get("status", "open"),
            included_in_analysis=data.get("included_in_analysis", True),
        )


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    ticker: str
    name: str
    asset_class: str
    currency: str = "RUB"
    issuer: str = ""
    sector: str = "unknown"
    risk_level: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Instrument":
        return cls(
            instrument_id=data["instrument_id"],
            ticker=data.get("ticker", ""),
            name=data.get("name", ""),
            asset_class=data.get("asset_class", "other"),
            currency=data.get("currency", "RUB"),
            issuer=data.get("issuer", ""),
            sector=data.get("sector", "unknown"),
            risk_level=data.get("risk_level", "medium"),
        )


@dataclass(frozen=True)
class Position:
    account_id: str
    instrument: Instrument
    quantity: float
    average_price: Money
    current_price: Money

    @property
    def current_value(self) -> Money:
        return Money(self.quantity * self.current_price.amount, self.current_price.currency)

    @property
    def pnl(self) -> Money:
        return Money(
            self.quantity * (self.current_price.amount - self.average_price.amount),
            self.current_price.currency,
        )

    def to_dict(self, portfolio_total: float | None = None) -> dict[str, Any]:
        value = self.current_value.amount
        share = round(value / portfolio_total * 100, 4) if portfolio_total else 0.0
        return {
            "account_id": self.account_id,
            "instrument": self.instrument.to_dict(),
            "quantity": self.quantity,
            "average_price": self.average_price.to_dict(),
            "current_price": self.current_price.to_dict(),
            "current_value": self.current_value.to_dict(),
            "pnl": self.pnl.to_dict(),
            "portfolio_share_percent": share,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Position":
        return cls(
            account_id=data["account_id"],
            instrument=Instrument.from_dict(data["instrument"]),
            quantity=float(data["quantity"]),
            average_price=Money.from_dict(data["average_price"]),
            current_price=Money.from_dict(data["current_price"]),
        )


@dataclass(frozen=True)
class Operation:
    operation_id: str
    account_id: str
    date: str
    operation_type: str
    instrument_id: str | None
    quantity: float
    amount: Money
    description: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["amount"] = self.amount.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Operation":
        return cls(
            operation_id=data["operation_id"],
            account_id=data["account_id"],
            date=data["date"],
            operation_type=data.get("operation_type", ""),
            instrument_id=data.get("instrument_id"),
            quantity=float(data.get("quantity", 0)),
            amount=Money.from_dict(data["amount"]),
            description=data.get("description", ""),
        )


@dataclass
class InvestorProfile:
    base_currency: str = "RUB"
    risk_profile: str = "balanced"
    horizon: str = "long_term"
    monthly_contribution: Money = field(default_factory=lambda: Money(50_000, "RUB"))
    target_allocation: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"asset_class": "bond", "target_percent": 50},
            {"asset_class": "stock", "target_percent": 35},
            {"asset_class": "fund", "target_percent": 10},
            {"asset_class": "currency", "target_percent": 5},
        ]
    )
    limits: dict[str, Any] = field(
        default_factory=lambda: {
            "max_single_issuer_percent": 15,
            "max_single_position_percent": 10,
            "max_single_sector_percent": 30,
            "max_high_risk_percent": 20,
        }
    )
    # Long-term goals, e.g. {"target_capital": {...}, "target_monthly_income": {...},
    # "preserve_principal": true, "income_sources": [...], "principles": [...]}.
    goals: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["monthly_contribution"] = self.monthly_contribution.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InvestorProfile":
        contribution = data.get("monthly_contribution") or {}
        return cls(
            base_currency=data.get("base_currency", "RUB"),
            risk_profile=data.get("risk_profile", "balanced"),
            horizon=data.get("horizon", "long_term"),
            monthly_contribution=Money(
                float(contribution.get("amount", 50_000)),
                contribution.get("currency", "RUB"),
            ),
            target_allocation=list(data.get("target_allocation") or cls().target_allocation),
            limits=dict(data.get("limits") or cls().limits),
            goals=dict(data.get("goals") or {}),
            notes=data.get("notes", ""),
        )

