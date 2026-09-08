"""CacheMixin: TTL-based caching of accounts, positions, and operations with fallback.

Each section (``accounts`` / ``positions`` / ``operations``) tracks its own freshness,
the timestamp of the data it holds, and the last broker error, so every tool can say
honestly what it returns:

* ``fresh``  — just fetched from the broker;
* ``cached`` — served from cache within ``cache_ttl_seconds``;
* ``stale``  — the broker call FAILED and the last known data (however old) is served
  instead. Stale answers carry an explicit warning with the cache date and the broker
  error, and their ``as_of`` is the cache timestamp, not "now".

After a failure the broker is not retried for ``broker_retry_cooldown_seconds`` unless
the call is forced (sync / refresh), so a chain of tool calls during an outage stays
fast instead of costing one timeout each.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..errors import BrokerUnavailableError, describe_error, error_hint
from ..models import Account, Operation, Position, utc_now_iso

logger = logging.getLogger(__name__)

SECTIONS = ("accounts", "positions", "operations")
_LABELS = {"accounts": "Счета", "positions": "Позиции портфеля", "operations": "Операции"}
_MODELS = {"accounts": Account, "positions": Position, "operations": Operation}
_GENERIC_ERROR = "брокер недоступен"


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    stamp = datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0)
    return stamp.isoformat().replace("+00:00", "Z")


class CacheMixin:
    # ── accessors used by the other mixins ───────────────────────────────────
    def _all_positions(self, force: bool = False) -> list[Position]:
        """All positions, served from cache within TTL; stale cache if the broker fails."""
        return self._section_items("positions", force)

    def _all_operations(self, force: bool = False) -> list[Operation]:
        """All operations (same policy as positions)."""
        return self._section_items("operations", force)

    def _cached_accounts(self, force: bool = False) -> list[Account]:
        """Broker accounts (same policy as positions)."""
        return self._section_items("accounts", force)

    def _positions(self, account_ids: list[str] | None, force: bool = False) -> list[Position]:
        positions = self._all_positions(force=force)
        wanted = set(account_ids or self._effective_account_ids())
        return [position for position in positions if position.account_id in wanted]

    # ── core: fetch-or-fallback ──────────────────────────────────────────────
    def _section_items(self, section: str, force: bool) -> list[Any]:
        now = time.time()
        if not force:
            cached = self._read_cache(section, now)
            if cached is not None:
                self._set_status(section, "cached")
                return cached
            if now < self._broker_retry_after:
                # A broker call failed moments ago: don't hammer a dead API.
                error = self._last_broker_error(section)
                self._record_error(section, error)
                return self._serve_stale(section, now, BrokerUnavailableError(error))
        try:
            items = self._fetch_from_broker(section)
        except Exception as exc:  # noqa: BLE001 — any broker failure degrades to cache
            error = describe_error(exc)
            logger.warning("Broker %s fetch failed, falling back to cache: %s", section, error)
            self._record_error(section, error)
            self._broker_retry_after = now + self.broker_retry_cooldown_seconds
            return self._serve_stale(section, now, exc)
        self._store_cache(section, now, items)
        self._clear_error(section)
        self._broker_retry_after = 0.0
        self._set_status(section, "fresh")
        return items

    def _fetch_from_broker(self, section: str) -> list[Any]:
        if section == "accounts":
            return self.broker.list_accounts()
        if section == "positions":
            return self.broker.get_positions(None)
        return self.broker.get_operations(None)

    def _serve_stale(self, section: str, now: float, exc: BaseException) -> list[Any]:
        stale = self._read_cache(section, now, ignore_ttl=True)
        if stale is None:
            raise exc
        self._set_status(section, "stale")
        return stale

    def _degrade(self, section: str, error: str) -> list[Any]:
        """Mark ``section`` stale with ``error`` without calling the broker.

        Used by sync when the broker is already known to be down: returns the last
        known items (or ``[]``) instead of spending another timeout.
        """
        self._record_error(section, error)
        items = self._read_cache(section, time.time(), ignore_ttl=True) or []
        self._set_status(section, "stale")
        return items

    # ── cache storage (memory + SQLite) ──────────────────────────────────────
    def _read_cache(self, section: str, now: float, ignore_ttl: bool = False) -> list[Any] | None:
        memory = getattr(self, f"_{section}_cache")
        if memory is not None:
            ts, items = memory
            return items if ignore_ttl or now - ts < self.cache_ttl_seconds else None
        if self.storage is None:
            return None
        cached = self.storage.get_setting(f"{section}_cache")
        if not cached:
            return None
        ts = float(cached.get("fetched_at_epoch", 0))
        if not (ignore_ttl or now - ts < self.cache_ttl_seconds):
            return None
        model = _MODELS[section]
        items = [model.from_dict(item) for item in cached.get(section, [])]
        setattr(self, f"_{section}_cache", (ts, items))
        return items

    def _store_cache(self, section: str, now: float, items: list[Any]) -> None:
        setattr(self, f"_{section}_cache", (now, items))
        if self.storage is not None:
            self.storage.set_setting(
                f"{section}_cache",
                {"fetched_at": _iso(now), "fetched_at_epoch": now, section: [item.to_dict() for item in items]},
            )

    # ── status / error bookkeeping ───────────────────────────────────────────
    def _set_status(self, section: str, status: str) -> None:
        setattr(self, f"_{section}_status", status)

    def _record_error(self, section: str, error: str) -> None:
        self._broker_errors[section] = {"error": error, "at": utc_now_iso()}
        if self.storage is not None:
            self.storage.set_setting("broker_errors", self._broker_errors)

    def _clear_error(self, section: str) -> None:
        if self._broker_errors.pop(section, None) is not None and self.storage is not None:
            self.storage.set_setting("broker_errors", self._broker_errors)

    def _last_broker_error(self, section: str) -> str:
        entry = self._broker_errors.get(section) or next(iter(self._broker_errors.values()), None)
        return entry["error"] if entry else _GENERIC_ERROR

    def _section_state(self, section: str) -> dict[str, Any]:
        """Health of one cache section (fresh / stale / unavailable) for sync status.

        Derived from the data itself — cache age vs TTL and the last broker error —
        not from what the last sync claimed. Never hits the broker.
        """
        now = time.time()
        if getattr(self, f"_{section}_cache") is None:
            self._read_cache(section, now, ignore_ttl=True)  # hydrate the persisted cache, if any
        cache = getattr(self, f"_{section}_cache")
        ts = cache[0] if cache is not None else None
        entry = self._broker_errors.get(section)
        if ts is None:
            status = "unavailable"
        elif entry is not None or now - ts >= self.cache_ttl_seconds:
            # The last broker attempt failed, or the data is older than the freshness window.
            status = "stale"
        else:
            status = "fresh"
        return {
            "status": status,
            "fetched_at": _iso(ts),
            "age_seconds": int(now - ts) if ts is not None else None,
            "last_error": entry["error"] if entry else None,
            "last_error_at": entry["at"] if entry else None,
        }

    def _stale_warning(self, section: str) -> str:
        state = self._section_state(section)
        error = state["last_error"] or _GENERIC_ERROR
        text = (
            f"{_LABELS[section]}: брокер недоступен ({error}). Отданы данные из кэша от "
            f"{state['fetched_at'] or 'неизвестной даты'}; всё, что произошло после этой даты, "
            "в ответе ОТСУТСТВУЕТ."
        )
        hint = error_hint(error)
        return f"{text} {hint}" if hint else text

    def _meta(self, *sections: str) -> dict[str, Any]:
        """Envelope fields (data_status / as_of / warnings) for data built from ``sections``.

        ``as_of`` is the timestamp of the data itself (the oldest cache involved), so a
        stale answer visibly dates itself instead of claiming to be current.
        """
        statuses = [getattr(self, f"_{section}_status") for section in sections]
        if "stale" in statuses:
            data_status = "stale"
        elif "cached" in statuses:
            data_status = "cached"
        else:
            data_status = "fresh"
        timestamps = []
        for section in sections:
            cache = getattr(self, f"_{section}_cache")
            if cache is not None:
                timestamps.append(cache[0])
        return {
            "data_status": data_status,
            "as_of": _iso(min(timestamps)) if timestamps else None,
            "warnings": [self._stale_warning(s) for s, status in zip(sections, statuses) if status == "stale"],
        }
