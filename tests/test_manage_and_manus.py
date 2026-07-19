"""Shipped-module tests: manage reorder/renew + Manus tank math.

Drives real headroom.manage / headroom.collect / headroom.dashboard_api
entry points — no reimplementation of the code under test.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock


class ManusTankMathTest(unittest.TestCase):
    """Pure Manus credit/tank functions on shipped collect module."""

    def test_total_available_is_free_plus_periodic(self):
        from headroom import collect
        credits = {
            "free": 4598,
            "periodic": 861,
            "pro_monthly": 4000,
            "spendable": 5459,
            "total": 5759,
        }
        self.assertEqual(collect.manus_total_available(credits), 5459.0)

    def test_total_available_without_spendable_field(self):
        from headroom import collect
        credits = {"free": 100, "periodic": 50, "pro_monthly": 200}
        self.assertEqual(collect.manus_total_available(credits), 150.0)

    def test_tank_full_prefers_renew_amount(self):
        from headroom import collect
        credits = {"pro_monthly": 4000}
        self.assertEqual(collect.manus_tank_full(credits, renew_amount=5000), 5000.0)
        self.assertEqual(collect.manus_tank_full(credits, renew_amount=None), 4000.0)

    def test_fill_percent_can_exceed_100_past_full(self):
        from headroom import collect
        # Live Manus Pro shape: free+monthly left > monthly allotment
        credits = {
            "free": 4598,
            "periodic": 861,
            "pro_monthly": 4000,
            "spendable": 5459,
        }
        pct = collect.manus_tank_fill_percent(credits)
        self.assertIsNotNone(pct)
        self.assertGreater(pct, 100.0)
        self.assertAlmostEqual(pct, 5459 / 4000 * 100.0, places=4)

    def test_live_pro_windows_still_map(self):
        from headroom import collect
        windows, credits, plan = collect.manus_credit_windows({
            "free_credits": 4598,
            "max_refresh_credits": 300,
            "next_refresh_time": "1784264400",
            "ok": True,
            "periodic_credits": 861,
            "pro_monthly_credits": 4000,
            "refresh_credits": 300,
            "refresh_interval": "daily",
            "total_credits": 5759,
        }, now=1784195530)
        self.assertEqual(plan, "Manus Pro")
        self.assertEqual(credits["spendable"], 5459)
        self.assertEqual(windows["month"]["remaining_units"], 861.0)
        self.assertEqual(windows["5h"]["resets_at"], 1784264400)
        fill = collect.manus_tank_fill_percent(credits, renew_amount=4000)
        self.assertGreater(fill, 100.0)


class ManageRenewAndReorderTest(unittest.TestCase):
    """Real manage.py against an isolated HEADROOM_DIR."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="headroom-test-")
        self.home = self._tmpdir.name
        self._env = mock.patch.dict(os.environ, {"HEADROOM_DIR": self.home})
        self._env.start()
        # Fresh import paths base after env set
        from headroom import paths
        paths.ensure_private(paths.base_dir())
        # Seed a minimal config with two accounts of same provider + one other
        config = {
            "schema_version": 1,
            "dashboard": {
                "theme": "midnight",
                "title": "test",
                "redact_emails": True,
                "port": 8377,
                "provider_order": ["claude", "codex", "grok", "manus", "nvidia"],
            },
            "accounts": [
                {
                    "name": "manus-a",
                    "provider": "manus",
                    "home": os.path.join(self.home, "homes", "manus-a"),
                    "monthly_cost_usd": 39.0,
                },
                {
                    "name": "manus-b",
                    "provider": "manus",
                    "home": os.path.join(self.home, "homes", "manus-b"),
                    "monthly_cost_usd": 39.0,
                },
                {
                    "name": "claude-a",
                    "provider": "claude",
                    "home": os.path.join(self.home, "homes", "claude-a"),
                    "monthly_cost_usd": 200.0,
                },
            ],
        }
        for a in config["accounts"]:
            os.makedirs(a["home"], mode=0o700, exist_ok=True)
        from headroom import manage
        manage.save_config(config)

    def tearDown(self):
        self._env.stop()
        self._tmpdir.cleanup()

    def test_update_renew_fields(self):
        from headroom import manage
        result = manage.update_account(
            "manus-a", renews_on="2026-08-13", renew_amount=4000)
        self.assertEqual(result["renews_on"], "2026-08-13")
        self.assertEqual(result["renew_amount"], 4000)
        state = manage.list_state()
        row = next(a for a in state["accounts"] if a["name"] == "manus-a")
        self.assertEqual(row["renews_on"], "2026-08-13")
        self.assertEqual(row["renew_amount"], 4000)

    def test_renews_on_rejects_bad_date(self):
        from headroom import manage
        with self.assertRaises(manage.ManageError):
            manage.update_account("manus-a", renews_on="not-a-date")

    def test_reorder_accounts_permutation(self):
        from headroom import manage
        order = ["claude-a", "manus-b", "manus-a"]
        result = manage.reorder_accounts(order)
        self.assertEqual(result["order"], order)
        names = [a["name"] for a in manage.list_state()["accounts"]]
        self.assertEqual(names, order)

    def test_reorder_rejects_incomplete(self):
        from headroom import manage
        with self.assertRaises(manage.ManageError):
            manage.reorder_accounts(["manus-a"])  # missing peers

    def test_move_account_up_same_provider(self):
        from headroom import manage
        # manus-b is after manus-a; move manus-b up within provider
        manage.move_account("manus-b", "up", scope="provider")
        names = [a["name"] for a in manage.list_state()["accounts"]]
        # manus-b should now appear before manus-a among manus slots
        self.assertLess(names.index("manus-b"), names.index("manus-a"))

    def test_reorder_providers(self):
        from headroom import manage
        dash = manage.reorder_providers(["manus", "claude", "codex", "grok", "nvidia"])
        self.assertEqual(dash["provider_order"][0], "manus")
        self.assertEqual(dash["provider_order"][1], "claude")

    def test_move_provider_up(self):
        from headroom import manage
        manage.reorder_providers(["claude", "codex", "grok", "manus", "nvidia"])
        dash = manage.move_provider("manus", "up")
        # manus moves before grok
        order = dash["provider_order"]
        self.assertLess(order.index("manus"), order.index("grok"))


class ConfigOnlyOverlayTest(unittest.TestCase):
    """overlay_public_from_config updates usage.json without network collect."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="headroom-overlay-")
        self.home = self._tmpdir.name
        self._env = mock.patch.dict(os.environ, {"HEADROOM_DIR": self.home})
        self._env.start()
        from headroom import paths
        paths.ensure_private(paths.base_dir())
        from headroom import manage
        config = {
            "schema_version": 1,
            "dashboard": {
                "theme": "midnight", "title": "t", "redact_emails": True,
                "port": 8377,
                "provider_order": ["manus", "claude", "codex", "grok", "nvidia"],
            },
            "accounts": [
                {"name": "m1", "provider": "manus",
                 "home": os.path.join(self.home, "h1"), "monthly_cost_usd": 39},
                {"name": "m2", "provider": "manus",
                 "home": os.path.join(self.home, "h2"), "monthly_cost_usd": 39},
            ],
        }
        for a in config["accounts"]:
            os.makedirs(a["home"], exist_ok=True)
        manage.save_config(config)
        # Seed a public snapshot with reverse order
        public = paths.public_dir()
        os.makedirs(public, exist_ok=True)
        snap = {
            "schema_version": 1,
            "run_id": "test",
            "generated": 1,
            "generated_iso": "2026-01-01T00:00:00Z",
            "accounts": [
                {"name": "m2", "provider": "manus", "ok": True, "windows": {}},
                {"name": "m1", "provider": "manus", "ok": True, "windows": {}},
            ],
        }
        with open(paths.public_snapshot_path(), "w") as handle:
            json.dump(snap, handle)

    def tearDown(self):
        self._env.stop()
        self._tmpdir.cleanup()

    def test_overlay_reorders_and_applies_renew(self):
        from headroom import manage, dashboard_api, paths
        manage.update_account("m1", renews_on="2026-08-13", renew_amount=4000)
        manage.reorder_accounts(["m1", "m2"])
        ok = dashboard_api.overlay_public_from_config()
        self.assertTrue(ok)
        with open(paths.public_snapshot_path()) as handle:
            snap = json.load(handle)
        names = [a["name"] for a in snap["accounts"]]
        self.assertEqual(names, ["m1", "m2"])
        m1 = snap["accounts"][0]
        self.assertEqual(m1.get("renews_on"), "2026-08-13")
        self.assertEqual(m1.get("renew_amount"), 4000)

    def test_after_config_change_without_collect(self):
        from headroom import manage, dashboard_api, paths
        manage.update_account("m2", renew_amount=1000)
        # Should not raise even with no network
        dashboard_api.after_config_change(collect=False)
        with open(paths.public_snapshot_path()) as handle:
            snap = json.load(handle)
        m2 = next(a for a in snap["accounts"] if a["name"] == "m2")
        self.assertEqual(m2.get("renew_amount"), 1000)
        # Shell should exist after build
        index = os.path.join(paths.public_dir(), "index.html")
        self.assertTrue(os.path.isfile(index))
        # Static assets copied
        self.assertTrue(os.path.isfile(
            os.path.join(paths.public_dir(), "static", "js", "app.js")))
        self.assertTrue(os.path.isfile(
            os.path.join(paths.public_dir(), "static", "css", "dashboard.css")))


if __name__ == "__main__":
    unittest.main()
