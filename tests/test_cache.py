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
        self.operations_calls = 0
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
        self.operations_calls += 1
        if self.fail:
            raise RuntimeError("broker down")
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

    def test_operations_served_from_cache(self) -> None:
        broker = CountingBroker()
        service = InvestorService(broker=broker)

        first = service.get_operations("2026-06-01", "2026-06-30")
        second = service.get_operations("2026-06-01", "2026-06-30")

        self.assertEqual(broker.operations_calls, 1)  # only one broker hit
        self.assertEqual(first["data_status"], "fresh")
        self.assertEqual(second["data_status"], "cached")
        self.assertGreaterEqual(first["data"]["total_count"], 1)

    def test_sync_refreshes_operations(self) -> None:
        broker = CountingBroker()
        service = InvestorService(broker=broker)

        service.get_operations("2026-06-01", "2026-06-30")  # call 1, caches
        service.sync_data("full")  # forces refresh -> call 2

        self.assertEqual(broker.operations_calls, 2)

    def test_operations_persist_across_instances(self) -> None:
        storage = Storage(":memory:")
        broker1 = CountingBroker()
        InvestorService(broker=broker1, storage=storage).get_operations("2026-06-01", "2026-06-30")
        self.assertEqual(broker1.operations_calls, 1)

        broker2 = CountingBroker()
        result = InvestorService(broker=broker2, storage=storage).get_operations("2026-06-01", "2026-06-30")
        self.assertEqual(broker2.operations_calls, 0)  # served from persisted cache
        self.assertEqual(result["data_status"], "cached")

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


class SeedingBroker(CountingBroker):
    """Broker that accepts metadata seeding (like the Tinkoff adapter)."""

    def __init__(self) -> None:
        super().__init__()
        self.seeded: list[str] = []
        self.resets = 0

    def seed_instrument_cache(self, instruments):
        self.seeded = [i.instrument_id for i in instruments]
        return len(self.seeded)

    def reset_instrument_cache(self):
        self.resets += 1


class MetadataSeedingTest(unittest.TestCase):
    def test_restart_seeds_broker_metadata_from_persisted_positions(self) -> None:
        storage = Storage(":memory:")
        first = InvestorService(broker=CountingBroker(), storage=storage)
        expected = sorted({p.instrument.instrument_id for p in first.broker.get_positions()})
        first.get_portfolio()  # persists positions_cache

        broker = SeedingBroker()
        restarted = InvestorService(broker=broker, storage=storage, cache_ttl_seconds=0)
        restarted.get_portfolio()
        restarted.get_portfolio()

        self.assertEqual(sorted(broker.seeded), expected)
        self.assertEqual(broker.positions_calls, 2)  # TTL 0 -> refetch, but seeding happens once

    def test_full_sync_resets_metadata_cache(self) -> None:
        broker = SeedingBroker()
        service = InvestorService(broker=broker, storage=Storage(":memory:"))
        service.sync_data(mode="incremental")
        self.assertEqual(broker.resets, 0)
        service.sync_data(mode="full")
        self.assertEqual(broker.resets, 1)

    def test_broker_without_seeding_support_is_fine(self) -> None:
        storage = Storage(":memory:")
        InvestorService(broker=CountingBroker(), storage=storage).get_portfolio()
        broker = CountingBroker()
        InvestorService(broker=broker, storage=storage, cache_ttl_seconds=0).get_portfolio()
        self.assertEqual(broker.positions_calls, 1)
