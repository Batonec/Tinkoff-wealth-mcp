from __future__ import annotations

import unittest

from investor_mcp.adapters import MockBrokerAdapter
from investor_mcp.service import InvestorService
from investor_mcp.storage import Storage


class CountingBroker:
    """Wraps the mock broker and counts calls; can simulate an outage."""

    def __init__(self) -> None:
        self._inner = MockBrokerAdapter()
        self.positions_calls = 0
        self.accounts_calls = 0
        self.fail = False

    def list_accounts(self):
        self.accounts_calls += 1
        return self._inner.list_accounts()

    def get_positions(self, account_ids=None):
        self.positions_calls += 1
        if self.fail:
            raise RuntimeError("broker down")
        return self._inner.get_positions(account_ids)

    def get_operations(self, account_ids=None):
        return self._inner.get_operations(account_ids)


class CacheTest(unittest.TestCase):
    def test_positions_served_from_cache(self) -> None:
        broker = CountingBroker()
        service = InvestorService(broker=broker)

        first = service.get_portfolio()
        second = service.get_portfolio()

        self.assertEqual(broker.positions_calls, 1)  # only one broker hit
        self.assertEqual(first["data_status"], "fresh")
        self.assertEqual(second["data_status"], "cached")

    def test_refresh_bypasses_cache(self) -> None:
        broker = CountingBroker()
        service = InvestorService(broker=broker)

        service.get_portfolio()
        result = service.get_portfolio(refresh=True)

        self.assertEqual(broker.positions_calls, 2)
        self.assertEqual(result["data_status"], "fresh")

    def test_ttl_zero_always_refetches(self) -> None:
        broker = CountingBroker()
        service = InvestorService(broker=broker, cache_ttl_seconds=0)

        service.get_portfolio()
        service.get_portfolio()

        self.assertEqual(broker.positions_calls, 2)

    def test_sync_forces_refresh(self) -> None:
        broker = CountingBroker()
        service = InvestorService(broker=broker)

        service.get_portfolio()  # call 1, caches
        service.sync_data("full")  # forces refresh -> call 2

        self.assertEqual(broker.positions_calls, 2)

    def test_offline_fallback_returns_stale(self) -> None:
        broker = CountingBroker()
        service = InvestorService(broker=broker, cache_ttl_seconds=0)

        service.get_portfolio()  # populate cache
        broker.fail = True
        result = service.get_portfolio()  # ttl=0 -> tries broker -> fails -> stale cache

        self.assertTrue(result["ok"])
        self.assertEqual(result["data_status"], "stale")
        self.assertGreater(result["data"]["total_value"]["amount"], 0)

    def test_persisted_cache_reused_by_fresh_service(self) -> None:
        storage = Storage(":memory:")
        broker1 = CountingBroker()
        InvestorService(broker=broker1, storage=storage).get_portfolio()
        self.assertEqual(broker1.positions_calls, 1)

        broker2 = CountingBroker()
        result = InvestorService(broker=broker2, storage=storage).get_portfolio()

        self.assertEqual(broker2.positions_calls, 0)  # served from persisted cache
        self.assertEqual(result["data_status"], "cached")
        self.assertGreater(result["data"]["total_value"]["amount"], 0)


if __name__ == "__main__":
    unittest.main()
