"""Broker adapters: the BrokerAdapter protocol, the env-based factory, and re-exports."""

from __future__ import annotations

import logging
import os
from typing import Protocol

from ..models import Account, Instrument, Operation, Position
from .mock import MockBrokerAdapter
from .tinkoff import (
    DEFAULT_TIMEOUT_SECONDS,
    TinkoffInvestAdapter,
    _date,
    _money,
    map_account,
    map_instrument,
    map_operation,
    map_position,
)

logger = logging.getLogger(__name__)


class BrokerAdapter(Protocol):
    def list_accounts(self) -> list[Account]:
        ...

    def get_positions(self, account_ids: list[str] | None = None) -> list[Position]:
        ...

    def get_operations(self, account_ids: list[str] | None = None) -> list[Operation]:
        ...


def build_broker_adapter() -> BrokerAdapter:
    """Pick the broker adapter from environment.

    Uses the real Tinkoff adapter when ``TINKOFF_INVEST_TOKEN`` is set, otherwise the
    read-only mock for local development. Optional transport knobs:

    * ``TINKOFF_INVEST_TARGET``          — gRPC endpoint ``host[:port]``
      (e.g. ``invest-public-api.tbank.ru:443``); default: the SDK constant;
    * ``TINKOFF_INVEST_CA_BUNDLE``       — PEM bundle of root CAs to trust (Минцифры);
    * ``TINKOFF_INVEST_TIMEOUT_SECONDS`` — per-RPC deadline, default 20; 0 disables.
    """
    token = os.getenv("TINKOFF_INVEST_TOKEN")
    if not token:
        return MockBrokerAdapter()
    sandbox = os.getenv("TINKOFF_INVEST_SANDBOX", "").lower() in {"1", "true", "yes"}
    target = os.getenv("TINKOFF_INVEST_TARGET", "").strip() or None
    ca_bundle = os.getenv("TINKOFF_INVEST_CA_BUNDLE", "").strip() or None
    if ca_bundle and not os.path.isfile(os.path.expanduser(ca_bundle)):
        logger.error("TINKOFF_INVEST_CA_BUNDLE points to a missing file: %s", ca_bundle)
    raw_timeout = os.getenv("TINKOFF_INVEST_TIMEOUT_SECONDS", "").strip()
    try:
        timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        logger.error("Invalid TINKOFF_INVEST_TIMEOUT_SECONDS=%r, using %s", raw_timeout, DEFAULT_TIMEOUT_SECONDS)
        timeout = DEFAULT_TIMEOUT_SECONDS
    return TinkoffInvestAdapter(
        token,
        sandbox=sandbox,
        target=target,
        ca_bundle=ca_bundle,
        timeout_seconds=timeout or None,
    )


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
