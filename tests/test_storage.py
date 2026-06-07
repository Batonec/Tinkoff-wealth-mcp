from __future__ import annotations

import os
import tempfile
import unittest

from investor_mcp.service import InvestorService
from investor_mcp.storage import Storage


class StorageTest(unittest.TestCase):
    def test_settings_round_trip(self) -> None:
        storage = Storage(":memory:")
        self.assertIsNone(storage.get_setting("profile"))
        storage.set_setting("profile", {"risk_profile": "aggressive"})
        self.assertEqual(storage.get_setting("profile"), {"risk_profile": "aggressive"})
        storage.set_setting("profile", {"risk_profile": "balanced"})  # upsert
        self.assertEqual(storage.get_setting("profile")["risk_profile"], "balanced")

    def test_snapshot_save_get_list(self) -> None:
        storage = Storage(":memory:")
        storage.save_snapshot("sync_1", {"total_value": {"amount": 100}}, created_at="2026-06-01T00:00:00Z")
        storage.save_snapshot("sync_2", {"total_value": {"amount": 200}}, created_at="2026-06-02T00:00:00Z")
        snap = storage.get_snapshot("sync_1")
        self.assertEqual(snap["snapshot_id"], "sync_1")
        self.assertEqual(snap["total_value"]["amount"], 100)
        ids = [s["snapshot_id"] for s in storage.list_snapshots()]
        self.assertEqual(ids, ["sync_2", "sync_1"])  # newest first

    def test_reports_and_recommendations(self) -> None:
        storage = Storage(":memory:")
        storage.save_report("weekly_2026-06-07", "weekly", "2026-06-07", "# Report")
        self.assertEqual(storage.get_report("weekly", "2026-06-07"), "# Report")
        self.assertIsNone(storage.get_report("weekly", "2000-01-01"))
        storage.save_recommendation("rec_1", {"action": "buy"})
        self.assertEqual(storage.get_recommendation("rec_1"), {"action": "buy"})

    def test_file_persistence_across_connections(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.db")
            s1 = Storage(path)
            s1.set_setting("selected_account_ids", ["a", "b"])
            s1.close()
            s2 = Storage(path)
            self.assertEqual(s2.get_setting("selected_account_ids"), ["a", "b"])


class ServicePersistenceTest(unittest.TestCase):
    def test_profile_and_selection_persist_across_instances(self) -> None:
        storage = Storage(":memory:")
        first = InvestorService(storage=storage)
        first.save_profile({"risk_profile": "aggressive", "horizon": "short_term"})
        first.select_accounts(["mock-brokerage"])

        second = InvestorService(storage=storage)  # hydrates from same storage
        self.assertEqual(second.profile.risk_profile, "aggressive")
        self.assertEqual(second.selected_account_ids, ["mock-brokerage"])

    def test_sync_creates_retrievable_snapshot(self) -> None:
        storage = Storage(":memory:")
        service = InvestorService(storage=storage)
        result = service.sync_data("full")
        snapshot_id = result["data"]["snapshot_id"]
        snapshot = service.get_snapshot(snapshot_id)
        self.assertIsNotNone(snapshot)
        self.assertGreater(snapshot["total_value"]["amount"], 0)

    def test_report_persists_and_reads_in_fresh_service(self) -> None:
        storage = Storage(":memory:")
        InvestorService(storage=storage).generate_report("weekly", to_date="2026-06-07")
        fresh = InvestorService(storage=storage)
        self.assertIsNotNone(fresh.get_report("weekly", "2026-06-07"))

    def test_without_storage_behaves_in_memory(self) -> None:
        service = InvestorService()  # no storage
        result = service.sync_data("full")
        # no snapshot_id when storage is absent, and no crash
        self.assertNotIn("snapshot_id", result["data"])
        self.assertEqual(service.list_snapshots(), [])


if __name__ == "__main__":
    unittest.main()
