from __future__ import annotations

import time
from typing import Any

from .models import Account, Operation, Position, utc_now_iso


class CacheMixin:
    def _all_positions(self, force: bool = False) -> list[Position]:
        """Return all positions, served from cache within ``cache_ttl_seconds``.

        Sets ``_positions_status`` to fresh/cached/stale. On broker failure falls
        back to any cache (even expired) so the server stays useful offline.
        """
        now = time.time()
        if not force:
            cached = self._read_positions_cache(now)
            if cached is not None:
                self._positions_status = "cached"
                return cached
        try:
            positions = self.broker.get_positions(None)
        except Exception:
            stale = self._read_positions_cache(now, ignore_ttl=True)
            if stale is not None:
                self._positions_status = "stale"
                return stale
            raise
        self._positions_cache = (now, positions)
        self._positions_status = "fresh"
        if self.storage is not None:
            self.storage.set_setting(
                "positions_cache",
                {"fetched_at": utc_now_iso(), "fetched_at_epoch": now,
                 "positions": [p.to_dict() for p in positions]},
            )
        return positions

    def _read_positions_cache(self, now: float, ignore_ttl: bool = False) -> list[Position] | None:
        if self._positions_cache is not None:
            ts, positions = self._positions_cache
            if ignore_ttl or now - ts < self.cache_ttl_seconds:
                return positions
        if self.storage is not None:
            cached = self.storage.get_setting("positions_cache")
            if cached:
                ts = float(cached.get("fetched_at_epoch", 0))
                if ignore_ttl or now - ts < self.cache_ttl_seconds:
                    positions = [Position.from_dict(d) for d in cached.get("positions", [])]
                    self._positions_cache = (ts, positions)
                    return positions
        return None

    def _all_operations(self, force: bool = False) -> list[Operation]:
        """All operations, served from cache within TTL (same policy as positions)."""
        now = time.time()
        if not force:
            cached = self._read_operations_cache(now)
            if cached is not None:
                self._operations_status = "cached"
                return cached
        try:
            operations = self.broker.get_operations(None)
        except Exception:
            stale = self._read_operations_cache(now, ignore_ttl=True)
            if stale is not None:
                self._operations_status = "stale"
                return stale
            raise
        self._operations_cache = (now, operations)
        self._operations_status = "fresh"
        if self.storage is not None:
            self.storage.set_setting(
                "operations_cache",
                {"fetched_at": utc_now_iso(), "fetched_at_epoch": now,
                 "operations": [o.to_dict() for o in operations]},
            )
        return operations

    def _read_operations_cache(self, now: float, ignore_ttl: bool = False) -> list[Operation] | None:
        if self._operations_cache is not None:
            ts, operations = self._operations_cache
            if ignore_ttl or now - ts < self.cache_ttl_seconds:
                return operations
        if self.storage is not None:
            cached = self.storage.get_setting("operations_cache")
            if cached:
                ts = float(cached.get("fetched_at_epoch", 0))
                if ignore_ttl or now - ts < self.cache_ttl_seconds:
                    operations = [Operation.from_dict(d) for d in cached.get("operations", [])]
                    self._operations_cache = (ts, operations)
                    return operations
        return None

    def _cached_accounts(self, force: bool = False) -> list[Account]:
        now = time.time()
        if not force and self._accounts_cache is not None:
            ts, accounts = self._accounts_cache
            if now - ts < self.cache_ttl_seconds:
                return accounts
        if not force and self._accounts_cache is None and self.storage is not None:
            cached = self.storage.get_setting("accounts_cache")
            if cached and now - float(cached.get("fetched_at_epoch", 0)) < self.cache_ttl_seconds:
                accounts = [Account.from_dict(d) for d in cached.get("accounts", [])]
                self._accounts_cache = (float(cached["fetched_at_epoch"]), accounts)
                return accounts
        try:
            accounts = self.broker.list_accounts()
        except Exception:
            if self._accounts_cache is not None:
                return self._accounts_cache[1]
            raise
        self._accounts_cache = (now, accounts)
        if self.storage is not None:
            self.storage.set_setting(
                "accounts_cache",
                {"fetched_at": utc_now_iso(), "fetched_at_epoch": now,
                 "accounts": [a.to_dict() for a in accounts]},
            )
        return accounts

    def _positions(self, account_ids: list[str] | None, force: bool = False) -> list[Position]:
        positions = self._all_positions(force=force)
        wanted = set(account_ids or self._effective_account_ids())
        return [position for position in positions if position.account_id in wanted]
