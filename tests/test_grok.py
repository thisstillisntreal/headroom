"""Grok provider: monthly billing window + local identity binding."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from headroom import collect, registry, widget


class GrokBillingWindowsTest(unittest.TestCase):
    def test_percent_and_period(self):
        payload = {
            "config": {
                "monthlyLimit": {"val": 1000},
                "used": {"val": 250},
                "billingPeriodStart": "2026-07-01T00:00:00+00:00",
                "billingPeriodEnd": "2026-08-01T00:00:00+00:00",
            }
        }
        windows = collect.grok_billing_windows(payload, now=1_700_000_000)
        self.assertIn("month", windows)
        self.assertEqual(windows["month"]["used_percent"], 25.0)
        self.assertEqual(windows["month"]["limit_units"], 1000.0)
        self.assertEqual(windows["month"]["used_units"], 250.0)
        self.assertEqual(windows["month"]["window_minutes"], 44640)

    def test_bare_numbers(self):
        payload = {"config": {"monthlyLimit": 200, "used": 200}}
        windows = collect.grok_billing_windows(payload, now=1)
        self.assertEqual(windows["month"]["used_percent"], 100.0)

    def test_missing_payload_held(self):
        with self.assertRaises(collect.IdentityBindingError):
            collect.grok_billing_windows({})


class GrokLocalIdentityTest(unittest.TestCase):
    def test_reads_auth_json(self):
        with tempfile.TemporaryDirectory() as home:
            auth = {
                "https://auth.x.ai::client": {
                    "key": _fake_jwt({"sub": "user-1", "tier": 5}),
                    "auth_mode": "oidc",
                    "email": "test@example.com",
                    "user_id": "user-1",
                    "expires_at": "2099-01-01T00:00:00Z",
                }
            }
            with open(os.path.join(home, "auth.json"), "w") as handle:
                json.dump(auth, handle)
            identity = collect.grok_local_identity(home)
            self.assertEqual(identity["email"], "test@example.com")
            self.assertEqual(identity["plan_type"], "SuperGrok Heavy")
            self.assertFalse(identity["verified"])
            self.assertEqual(
                identity["account_fingerprint"],
                collect.fingerprint("user-1"),
            )


class GrokRegistryTest(unittest.TestCase):
    def test_provider_and_family(self):
        self.assertIn("grok", registry.PROVIDERS)
        self.assertEqual(registry.family("grok-4.5"), "grok")
        self.assertEqual(registry.family_provider("grok"), "grok")


class GrokWidgetProjectionTest(unittest.TestCase):
    def test_month_only_projects_current(self):
        now = time.time()
        snapshot = {
            "schema_version": 1,
            "generated": now,
            "accounts": [{
                "name": "g1",
                "provider": "grok",
                "ok": True,
                "trust_state": "verified",
                "stale": False,
                "captured_at": now - 10,
                "windows": {
                    "month": {
                        "used_percent": 40.0,
                        "resets_at": now + 86400,
                        "observed_at": now - 10,
                        "freshness": "fresh",
                    }
                },
            }],
        }
        projected = widget.project(snapshot, evaluated_at=now)
        account = projected["accounts"][0]
        self.assertEqual(account["state"], "current")
        self.assertIn("month", account["windows"])
        self.assertNotIn("5h", account["windows"])
        self.assertAlmostEqual(account["windows"]["month"]["left_percent"], 60.0)


def _fake_jwt(claims):
    import base64
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(
        json.dumps(claims).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


if __name__ == "__main__":
    unittest.main()


class CostsCatalogTest(unittest.TestCase):
    def test_defaults_and_override(self):
        from headroom import costs
        self.assertEqual(costs.default_monthly_cost("claude", "Max 20x"), 200.0)
        self.assertEqual(costs.default_monthly_cost("manus", "pro"), 39.0)
        self.assertEqual(
            costs.resolve_monthly_cost(
                {"provider": "claude", "monthly_cost_usd": 42}, "Max 20x"),
            42.0)


class ManusCreditsTest(unittest.TestCase):
    def test_periodic_quota_percent(self):
        windows, credits, plan = collect.manus_credit_windows({
            "ok": True,
            "data": {
                "total_credits": 100,
                "periodic_credits": 25,
                "pro_monthly_credits": 100,
                "free_credits": 0,
                "addon_credits": 0,
                "refresh_credits": 0,
                "max_refresh_credits": 0,
                "next_refresh_time": 0,
                "refresh_interval": "",
            }
        }, now=10)
        self.assertEqual(windows["month"]["used_percent"], 75.0)
        self.assertEqual(plan, "Manus")
        self.assertEqual(credits["total"], 100)
