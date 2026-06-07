"""Broker adapters: the BrokerAdapter protocol, the env-based factory, and re-exports."""

from __future__ import annotations

import os
from typing import Protocol

from ..models import Account, Instrument, Operation, Position
from .mock import MockBrokerAdapter
from .tinkoff import (
    TinkoffInvestAdapter,
    _date,
    _money,
    map_account,
    map_instrument,
    map_operation,
    map_position,
)


class BrokerAdapter(Protocol):
    def list_accounts(self) -> list[Account]:
        ...

    def get_positions(self, account_ids: list[str] | None = None) -> list[Position]:
        ...

    def get_operations(self, account_ids: list[str] | None = None) -> list[Operation]:
        ...


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


__all__ = [
    "BrokerAdapter",
    "MockBrokerAdapter",
    "TinkoffInvestAdapter",
    "build_broker_adapter",
    "map_account",
    "map_instrument",
    "map_position",
    "map_operation",
    "_money",
    "_date",
    "Instrument",
]
