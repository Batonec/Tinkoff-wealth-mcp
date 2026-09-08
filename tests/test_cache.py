from __future__ import annotations

import unittest

from investor_mcp.adapters import MockBrokerAdapter
from investor_mcp.service import InvestorService
from investor_mcp.storage import Storage

ALL_SECTIONS = {"accounts", "positions", "operations"}


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
        if self.fail:
            raise RuntimeError("broker down")
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
        self.assertEqual(second["warnings"], [])

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

        first = service.get_portfolio()  # populate cache
        broker.fail = True
        result = service.get_portfolio()  # ttl=0 -> tries broker -> fails -> stale cache

        self.assertTrue(result["ok"])
        self.assertEqual(result["data_status"], "stale")
        self.assertGreater(result["data"]["total_value"]["amount"], 0)
        # A stale answer dates itself by the cache and says what happened.
        self.assertEqual(result["as_of"], first["as_of"])
        self.assertTrue(result["warnings"])
        self.assertIn("broker down", result["warnings"][0])
        self.assertIn("ОТСУТСТВУЕТ", result["warnings"][0])

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


class StaleReportingTest(unittest.TestCase):
    """A broker outage must be visible: warnings, real as_of, honest sync status."""

    def test_stale_operations_carry_warning_and_cache_as_of(self) -> None:
        broker = CountingBroker()
        service = InvestorService(broker=broker, cache_ttl_seconds=0)
        first = service.get_operations("2026-06-01", "2026-06-30")
        broker.fail = True

        result = service.get_operations("2026-06-01", "2026-06-30")

        self.assertEqual(result["data_status"], "stale")
        self.assertEqual(result["as_of"], first["as_of"])
        self.assertEqual(result["data"]["total_count"], first["data"]["total_count"])
        self.assertIn("Операции", result["warnings"][0])
        self.assertIn("broker down", result["warnings"][0])

    def test_broker_not_retried_during_cooldown(self) -> None:
        broker = CountingBroker()
        service = InvestorService(broker=broker, cache_ttl_seconds=0, broker_retry_cooldown_seconds=300)
        service.get_portfolio()
        broker.fail = True
        service.get_portfolio()  # failing broker call -> stale, starts the cooldown
        calls = broker.positions_calls

        result = service.get_portfolio()  # within cooldown: stale without a broker call

        self.assertEqual(broker.positions_calls, calls)
        self.assertEqual(result["data_status"], "stale")
        self.assertIn("broker down", result["warnings"][0])

        service.get_portfolio(refresh=True)  # a forced refresh bypasses the cooldown
        self.assertEqual(broker.positions_calls, calls + 1)

    def test_recovery_clears_cooldown_and_warnings(self) -> None:
        broker = CountingBroker()
        service = InvestorService(broker=broker, cache_ttl_seconds=0, broker_retry_cooldown_seconds=300)
        service.get_portfolio()
        broker.fail = True
        service.get_portfolio()
        broker.fail = False

        result = service.get_portfolio(refresh=True)

        self.assertEqual(result["data_status"], "fresh")
        self.assertEqual(result["warnings"], [])
        calls = broker.positions_calls
        service.get_portfolio()  # cooldown lifted: ttl=0 goes to the broker again
        self.assertEqual(broker.positions_calls, calls + 1)
        self.assertEqual(service.get_sync_status()["data"]["sections"]["positions"]["last_error"], None)

    def test_sync_reports_partial_when_broker_down(self) -> None:
        broker = CountingBroker()
        service = InvestorService(broker=broker)
        service.sync_data("full")
        broker.fail = True

        result = service.sync_data("full")

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["status"], "partial")
        self.assertEqual(set(result["data"]["stale_sections"]), ALL_SECTIONS)
        self.assertEqual(result["data_status"], "stale")
        self.assertTrue(any("broker down" in warning for warning in result["warnings"]))
        self.assertIsNotNone(result["data"]["last_success_at"])  # kept from the good sync

    def test_sync_skips_positions_and_operations_when_accounts_fail(self) -> None:
        broker = CountingBroker()
        service = InvestorService(broker=broker)
        service.sync_data("full")
        broker.fail = True
        positions_calls, operations_calls = broker.positions_calls, broker.operations_calls

        service.sync_data("full")

        # One failed call is enough to know the broker is down: no extra timeouts.
        self.assertEqual(broker.positions_calls, positions_calls)
        self.assertEqual(broker.operations_calls, operations_calls)

    def test_sync_status_reflects_stale_sections_and_error(self) -> None:
        broker = CountingBroker()
        service = InvestorService(broker=broker)
        service.sync_data("full")
        good = service.get_sync_status()
        self.assertEqual(good["data_status"], "fresh")
        self.assertEqual(good["data"]["stale_sections"], [])
        broker.fail = True
        service.sync_data("full")

        status = service.get_sync_status()

        self.assertEqual(status["data_status"], "stale")
        self.assertEqual(status["data"]["status"], "partial")
        self.assertEqual(set(status["data"]["stale_sections"]), ALL_SECTIONS)
        positions = status["data"]["sections"]["positions"]
        self.assertEqual(positions["status"], "stale")
        self.assertIn("broker down", positions["last_error"])
        self.assertIsNotNone(positions["fetched_at"])
        self.assertEqual(status["data"]["last_success_at"], good["data"]["last_success_at"])
        self.assertTrue(status["warnings"])

    def test_sync_without_cache_returns_broker_error(self) -> None:
        broker = CountingBroker()
        broker.fail = True
        service = InvestorService(broker=broker)

        result = service.sync_data("full")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "BROKER_API_UNAVAILABLE")
        self.assertIn("broker down", result["summary"])
        status = service.get_sync_status()
        self.assertEqual(status["data"]["status"], "failed")
        self.assertEqual(status["data_status"], "stale")
        self.assertIsNone(status["data"]["last_success_at"])

    def test_sync_status_after_restart_uses_persisted_cache_age(self) -> None:
        storage = Storage(":memory:")
        InvestorService(broker=CountingBroker(), storage=storage).sync_data("full")

        restarted = InvestorService(broker=CountingBroker(), storage=storage, cache_ttl_seconds=0)
        status = restarted.get_sync_status()

        self.assertEqual(status["data"]["status"], "success")
        self.assertEqual(status["data_status"], "stale")  # ttl=0 -> older than the freshness window
        self.assertEqual(set(status["data"]["stale_sections"]), ALL_SECTIONS)
        self.assertIsNotNone(status["data"]["sections"]["operations"]["fetched_at"])

    def test_broker_error_survives_restart(self) -> None:
        storage = Storage(":memory:")
        broker = CountingBroker()
        service = InvestorService(broker=broker, storage=storage, cache_ttl_seconds=0)
        service.get_portfolio()
        broker.fail = True
        service.get_portfolio()

        restarted = InvestorService(broker=CountingBroker(), storage=storage)
        positions = restarted.get_sync_status()["data"]["sections"]["positions"]

        self.assertEqual(positions["status"], "stale")
        self.assertIn("broker down", positions["last_error"])

    def test_accounts_fall_back_to_persisted_cache(self) -> None:
        storage = Storage(":memory:")
        InvestorService(broker=CountingBroker(), storage=storage).list_accounts()
        broker = CountingBroker()
        broker.fail = True
        service = InvestorService(broker=broker, storage=storage, cache_ttl_seconds=0)

        result = service.list_accounts()

        self.assertTrue(result["ok"])
        self.assertEqual(result["data_status"], "stale")
        self.assertEqual(len(result["data"]["accounts"]), 2)
        self.assertIn("Счета", result["warnings"][0])


if __name__ == "__main__":
    unittest.main()
