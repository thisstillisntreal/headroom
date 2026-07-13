"""Widget contract, refresh gate, integrations, and release artifact tests."""
import io
import json
import math
import os
import re
import struct
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout
from unittest import mock

from headroom import __main__, dashboard, paths, widget


NOW = 2_000_000_000
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "integrations", "swiftbar", "headroom.1m.sh")
WINDOWS_SCRIPT = os.path.join(ROOT, "experimental", "windows",
                              "headroom-tray.ps1")
WINDOWS_ICONS = os.path.join(ROOT, "experimental", "windows", "icons")


def usage_account(name="alpha", used5=20.0, used7=40.0, **overrides):
    account = {
        "name": name,
        "provider": "claude",
        "ok": True,
        "stale": False,
        "trust_state": "verified",
        "captured_at": NOW - 20,
        "windows": {
            "5h": {"used_percent": used5, "resets_at": NOW + 1800,
                   "observed_at": NOW - 20},
            "7d": {"used_percent": used7, "resets_at": NOW + 86400,
                   "observed_at": NOW - 20},
        },
    }
    account.update(overrides)
    return account


def usage_snapshot(*accounts, generated=None):
    return {"schema_version": 1, "generated": NOW - 30 if generated is None
            else generated, "accounts": list(accounts)}


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


def memory_get(handler_class, directory, route, host="127.0.0.1:8377",
               server_port=None):
    """Drive the real request handler without opening a sandbox-blocked socket."""
    handler = object.__new__(handler_class)
    handler.directory = directory
    handler.path = route
    handler.headers = {"Host": host}
    handler.command = "GET"
    handler.request_version = "HTTP/1.1"
    handler.requestline = "GET %s HTTP/1.1" % route
    handler.client_address = ("127.0.0.1", 1)
    if server_port is not None:
        server = object.__new__(dashboard.http.server.ThreadingHTTPServer)
        server.server_address = ("127.0.0.1", server_port)
        handler.server = server
    handler.close_connection = True
    handler.wfile = io.BytesIO()
    handler.do_GET()
    raw = handler.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
    return status, headers, body


class WidgetContractTests(unittest.TestCase):
    def test_widget_contract_has_exact_versioned_shape(self):
        value = widget.project(usage_snapshot(usage_account()), NOW)
        self.assertEqual(set(value), {"schema", "freshness", "accounts",
                                      "headline"})
        self.assertEqual(value["schema"], "headroom_widget@1")
        self.assertEqual(set(value["freshness"]),
                         {"state", "age_seconds", "reason", "evaluated_at"})
        self.assertEqual(set(value["accounts"][0]["windows"]), {"5h", "7d"})
        self.assertEqual(set(value["accounts"][0]),
                         {"name", "provider", "state", "windows"})
        for window in value["accounts"][0]["windows"].values():
            self.assertEqual(set(window), {"left_percent", "resets_at",
                                           "observed_at", "state",
                                           "last_observed_left_percent"})

    def test_widget_projection_covers_all_account_states(self):
        accounts = [
            usage_account("current"),
            usage_account("limited", used5=100),
            usage_account("stale", stale=True),
            usage_account("held", ok=False, trust_state="held"),
        ]
        states = {row["name"]: row["state"]
                  for row in widget.project(usage_snapshot(*accounts), NOW)[
                      "accounts"]}
        self.assertEqual(states, {"current": "current", "limited": "limited",
                                  "stale": "stale", "held": "held"})

    def test_current_window_exposes_left_percent(self):
        window = widget.project(usage_snapshot(usage_account(used5=12.5)), NOW)[
            "accounts"][0]["windows"]["5h"]
        self.assertEqual(window["state"], "current")
        self.assertEqual(window["left_percent"], 87.5)
        self.assertIsNone(window["last_observed_left_percent"])

    def test_noncurrent_window_hides_live_value(self):
        window = widget.project(
            usage_snapshot(usage_account(stale=True, used5=25)), NOW)[
                "accounts"][0]["windows"]["5h"]
        self.assertEqual(window["state"], "stale")
        self.assertIsNone(window["left_percent"])
        self.assertEqual(window["last_observed_left_percent"], 75.0)

    def test_missing_windows_are_explicitly_held(self):
        account = usage_account()
        del account["windows"]["7d"]
        projected = widget.project(usage_snapshot(account), NOW)["accounts"][0]
        self.assertEqual(projected["state"], "held")
        self.assertEqual(projected["windows"]["5h"]["state"], "held")
        self.assertIsNone(projected["windows"]["5h"]["left_percent"])
        self.assertEqual(projected["windows"]["5h"][
            "last_observed_left_percent"], 80.0)
        self.assertEqual(projected["windows"]["7d"]["state"], "held")
        self.assertIsNone(projected["windows"]["7d"]["left_percent"])

    def test_one_stale_window_demotes_every_child_window(self):
        account = usage_account()
        account["windows"]["7d"]["observed_at"] = (
            NOW - widget.OBSERVATION_MAX_AGE - 1)
        projected = widget.project(usage_snapshot(account), NOW)["accounts"][0]
        self.assertEqual(projected["state"], "stale")
        for key, last in (("5h", 80.0), ("7d", 60.0)):
            self.assertEqual(projected["windows"][key]["state"], "stale")
            self.assertIsNone(projected["windows"][key]["left_percent"])
            self.assertEqual(projected["windows"][key][
                "last_observed_left_percent"], last)

    def test_widget_projection_rejects_out_of_range_values(self):
        bad_values = [-0.1, 100.1, float("inf"), float("nan"), "20", True]
        for bad in bad_values:
            with self.subTest(value=bad):
                account = usage_account()
                account["windows"]["5h"]["used_percent"] = bad
                window = widget.project(usage_snapshot(account), NOW)[
                    "accounts"][0]["windows"]["5h"]
                self.assertEqual(window["state"], "held")
                self.assertIsNone(window["left_percent"])
                self.assertIsNone(window["last_observed_left_percent"])

    def test_widget_projection_rejects_clock_skew(self):
        future_snapshot = widget.project(
            usage_snapshot(usage_account(), generated=NOW + 1), NOW)
        self.assertEqual(future_snapshot["freshness"]["state"], "held")
        account = usage_account()
        account["windows"]["5h"]["observed_at"] = NOW + 1
        future_window = widget.project(usage_snapshot(account), NOW)[
            "accounts"][0]["windows"]["5h"]
        self.assertEqual(future_window["state"], "held")
        self.assertIsNone(future_window["left_percent"])

    def test_freshness_age_uses_evaluated_at(self):
        value = widget.project(
            usage_snapshot(usage_account(), generated=NOW - 25), NOW)
        self.assertEqual(value["freshness"], {
            "state": "current", "age_seconds": 25,
            "reason": "snapshot_current", "evaluated_at": NOW})

    def test_widget_contract_omits_routing_claims(self):
        rendered = json.dumps(widget.project(
            usage_snapshot(usage_account()), NOW)).lower()
        for forbidden in ("best", "accounts_ok", "routable", "eligibility",
                          "eligible", "reserve", "recommendation"):
            self.assertNotIn(forbidden, rendered)

    def test_headline_uses_fullest_current_5h_tank(self):
        value = widget.project(usage_snapshot(
            usage_account("a", used5=55), usage_account("b", used5=8)), NOW)
        self.assertEqual(value["headline"], {
            "current_accounts": 2, "total_accounts": 2,
            "fullest_5h_left_percent": 92.0})

    def test_headline_excludes_noncurrent_candidates(self):
        value = widget.project(usage_snapshot(
            usage_account("current", used5=60),
            usage_account("limited", used5=100),
            usage_account("stale", used5=1, stale=True),
            usage_account("held", used5=0, ok=False, trust_state="held")), NOW)
        self.assertEqual(value["headline"]["current_accounts"], 1)
        self.assertEqual(value["headline"]["fullest_5h_left_percent"], 40.0)

    def test_headline_without_candidate_is_gray_placeholder(self):
        value = usage_snapshot(usage_account(stale=True, used5=1))
        rendered = widget.render_swiftbar(value, NOW)
        self.assertIn("hr 0/1 · -- | color=gray", rendered.splitlines()[1])


class WidgetRendererTests(unittest.TestCase):
    def test_sanitizer_removes_newlines_and_controls(self):
        cleaned = widget.sanitize("a\r\nb\x00c\x1fd\x7fe\u200bf")
        self.assertFalse(any(unicodedata in cleaned for unicodedata in
                             ("\r", "\n", "\x00", "\x1f", "\x7f", "\u200b")))
        self.assertEqual(cleaned, "a b c d e f")

    def test_sanitizer_escapes_swiftbar_parameter_syntax(self):
        cleaned = widget.sanitize("name | bash=/tmp/x param1=oops")
        self.assertNotIn("|", cleaned)
        self.assertNotIn("=", cleaned)
        self.assertNotIn("bash=", cleaned)
        self.assertIn("¦", cleaned)

    def test_swiftbar_renderer_starts_with_exact_sentinel(self):
        rendered = widget.render_swiftbar(
            usage_snapshot(usage_account()), NOW)
        self.assertEqual(rendered.splitlines()[0], "headroom_widget_txt@1")

    def test_swiftbar_renderer_contains_one_headline(self):
        rendered = widget.render_swiftbar(
            usage_snapshot(usage_account(used5=12)), NOW)
        headline_lines = [line for line in rendered.splitlines()
                          if line.startswith("hr ")]
        self.assertEqual(headline_lines, ["hr 1/1 · 88% | color=green"])

    def test_swiftbar_rows_include_both_windows_and_resets(self):
        rendered = widget.render_swiftbar(
            usage_snapshot(usage_account()), NOW)
        self.assertRegex(rendered, r"(?m)^--5h: .* · resets ")
        self.assertRegex(rendered, r"(?m)^--7d: .* · resets ")

    def test_swiftbar_renderer_labels_fullest_tank(self):
        rendered = widget.render_swiftbar(
            usage_snapshot(usage_account()), NOW)
        self.assertIn("Fullest tank: 80% (current 5h)", rendered)

    def test_swiftbar_renderer_emits_no_execution_directives(self):
        account = usage_account("safe")
        account["provider"] = "bad | bash=/tmp/x shell=yes terminal=true param1=x"
        rendered = widget.render_swiftbar(usage_snapshot(account), NOW).lower()
        self.assertIsNone(re.search(r"(?:bash|shell|terminal|param\d+)=", rendered))

    def test_schema_marker_never_bypasses_projection(self):
        poisoned = {
            "schema": widget.SCHEMA,
            "headline": {"current_accounts":
                         "1 | shell=/bin/sh param1=-c",
                         "total_accounts": 1,
                         "fullest_5h_left_percent": 99},
            "accounts": [],
        }
        rendered = widget.render_swiftbar(poisoned, NOW)
        self.assertIn("hr 0/0 · -- | color=gray", rendered)
        self.assertNotIn("shell=", rendered)

    def test_dashboard_href_is_parsed_and_reconstructed(self):
        valid = widget.render_swiftbar(
            None, dashboard_href="http://localhost:49152")
        self.assertIn("href=http://127.0.0.1:49152/", valid)
        attacks = (
            "http://127.0.0.1:8377@evil.example/",
            "http://localhost:8377@evil.example/",
            "http://127.0.0.1:8377/ | shell=/bin/sh",
            "http://127.0.0.1:8377/?x=1",
            "http://127.0.0.1:8377/#x",
            "http://127.0.0.1:0/",
            "http://127.0.0.1:65536/",
        )
        for href in attacks:
            with self.subTest(href=href):
                rendered = widget.render_swiftbar(None, dashboard_href=href)
                self.assertIn("href=" + widget.DASHBOARD_HREF, rendered)
                self.assertNotIn("evil.example", rendered)
                self.assertNotIn("shell=", rendered)

    def test_aggregate_noncurrent_rows_never_retain_live_colors(self):
        account = usage_account()
        del account["windows"]["7d"]
        rendered = widget.render_swiftbar(usage_snapshot(account), NOW)
        self.assertRegex(rendered, r"(?m)^--5h: .*\(held\).* \| color=gray$")
        self.assertNotRegex(rendered, r"(?m)^--5h: .* \| color=green$")

    def test_widget_feed_without_snapshot_is_static_offline(self):
        with mock.patch.object(paths, "load_json", return_value=None):
            output = io.StringIO()
            with redirect_stdout(output):
                result = __main__._dispatch(["widget-feed", "--swiftbar"])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), widget.render_swiftbar(None))
        self.assertIn("hr OFFLINE | color=gray", output.getvalue())

    def test_local_widget_feed_never_collects(self):
        from headroom import collect
        with mock.patch.object(paths, "load_json",
                               return_value=usage_snapshot(usage_account())), \
                mock.patch.object(collect, "run_collect",
                                  side_effect=AssertionError("must not collect")):
            output = io.StringIO()
            with redirect_stdout(output):
                result = __main__._dispatch(["widget-feed", "--swiftbar"])
        self.assertEqual(result, 0)
        self.assertTrue(output.getvalue().startswith("headroom_widget_txt@1\n"))


class RefreshGateTests(unittest.TestCase):
    def gate_fixture(self, failure_base=5, failure_cap=300):
        clock = MutableClock()
        state = {"snapshot": usage_snapshot(
            usage_account(), generated=clock.value - 301), "attempts": 0}

        def load():
            return state["snapshot"]

        def collect():
            state["attempts"] += 1
            state["snapshot"] = usage_snapshot(
                usage_account(), generated=clock.value)

        gate = dashboard.RefreshGate(300, failure_base, failure_cap, clock)
        return gate, clock, state, load, collect

    def test_refresh_gate_shares_success_across_all_feeds(self):
        gate, clock, state, load, collect = self.gate_fixture()
        results = [gate.get(load, collect) for route in
                   ("/usage.json", "/widget.json", "/widget.txt")]
        self.assertEqual(state["attempts"], 1)
        self.assertTrue(all(not result.refresh_failed for result in results))

    def test_refresh_gate_honors_300_second_success_ttl(self):
        gate, clock, state, load, collect = self.gate_fixture()
        gate.get(load, collect)
        clock.value += 299
        gate.get(load, collect)
        self.assertEqual(state["attempts"], 1)

    def test_refresh_gate_recollects_after_success_ttl(self):
        gate, clock, state, load, collect = self.gate_fixture()
        gate.get(load, collect)
        clock.value += 300
        gate.get(load, collect)
        self.assertEqual(state["attempts"], 2)

    def test_refresh_gate_failure_backoff_is_exponential_and_bounded(self):
        gate, clock, state, load, _ = self.gate_fixture(2, 5)
        delays = []

        def fail():
            state["attempts"] += 1
            raise OSError("offline")

        for expected in (2, 4, 5, 5):
            gate.get(load, fail)
            delays.append(gate.last_delay)
            self.assertEqual(gate.retry_at, clock.value + expected)
            clock.value += expected
        self.assertEqual(delays, [2, 4, 5, 5])

    def test_failed_publication_100_requests_attempt_once(self):
        gate, clock, state, load, _ = self.gate_fixture()

        def fail():
            state["attempts"] += 1
            raise OSError("offline")

        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(lambda _: gate.get(load, fail), range(100)))
        self.assertEqual(state["attempts"], 1)
        self.assertTrue(all(result.refresh_failed for result in results))

    def test_refresh_gate_opens_once_at_retry_boundary(self):
        gate, clock, state, load, _ = self.gate_fixture()

        def fail():
            state["attempts"] += 1
            raise OSError("offline")

        gate.get(load, fail)
        clock.value = gate.retry_at
        with ThreadPoolExecutor(max_workers=32) as pool:
            list(pool.map(lambda _: gate.get(load, fail), range(100)))
        self.assertEqual(state["attempts"], 2)

    def test_failed_refresh_serves_last_good_as_noncurrent(self):
        gate, clock, state, load, _ = self.gate_fixture()

        def fail():
            raise OSError("offline")

        result = gate.get(load, fail)
        projected = widget.project(
            result.snapshot, clock.value,
            force_noncurrent_reason=result.reason)
        self.assertTrue(result.refresh_failed)
        self.assertEqual(projected["freshness"]["state"], "stale")
        self.assertEqual(projected["accounts"][0]["state"], "stale")
        self.assertIsNone(projected["accounts"][0]["windows"]["5h"][
            "left_percent"])

    def test_failed_refresh_without_snapshot_returns_503(self):
        class LiveHandler(dashboard.Handler):
            demo = False
            refresh_gate = dashboard.RefreshGate(failure_base=60)

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(paths, "load_json", return_value=None), \
                mock.patch.object(dashboard.collector, "run_collect",
                                  side_effect=OSError("offline")):
            status, headers, body = memory_get(
                LiveHandler, directory, "/widget.json")
        self.assertEqual(status, 503)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertIn(b"no usage snapshot", body)


class DashboardHttpTests(unittest.TestCase):
    @contextmanager
    def demo_server(self, snapshot=None, index=None):
        snapshot = snapshot or usage_snapshot(usage_account())
        index = index or b"<!doctype html><title>same template</title>"
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "usage.json"), "w") as handle:
                json.dump(snapshot, handle)
            with open(os.path.join(directory, "index.html"), "wb") as handle:
                handle.write(index)

            class DemoHandler(dashboard.Handler):
                demo = True

            yield DemoHandler, directory

    @staticmethod
    def template_text():
        with open(dashboard.TEMPLATE) as handle:
            return handle.read()

    def test_endpoint_and_cli_use_byte_identical_renderer(self):
        snapshot = usage_snapshot(usage_account())
        with mock.patch.object(widget.time, "time", return_value=NOW):
            with self.demo_server(snapshot) as server:
                status, _, endpoint = memory_get(*server, "/widget.txt")
            output = io.StringIO()
            with mock.patch.object(paths, "load_json", return_value=snapshot), \
                    redirect_stdout(output):
                result = __main__._dispatch(["widget-feed", "--swiftbar"])
        self.assertEqual((status, result), (200, 0))
        self.assertEqual(endpoint, output.getvalue().encode("utf-8"))

    def test_widget_routes_and_content_types(self):
        with mock.patch.object(widget.time, "time", return_value=NOW):
            with self.demo_server() as server:
                json_response = memory_get(*server, "/widget.json")
                text_response = memory_get(*server, "/widget.txt")
        self.assertEqual(json_response[0], 200)
        self.assertEqual(json_response[1]["content-type"], "application/json")
        self.assertEqual(json.loads(json_response[2])["schema"],
                         "headroom_widget@1")
        self.assertEqual(text_response[0], 200)
        self.assertEqual(text_response[1]["content-type"],
                         "text/plain; charset=utf-8")
        self.assertTrue(text_response[2].startswith(b"headroom_widget_txt@1\n"))

    def test_widget_path_serves_existing_template(self):
        template = self.template_text().encode()
        with self.demo_server(index=template) as server:
            root = memory_get(*server, "/")
            widget_path = memory_get(*server, "/widget")
        self.assertEqual(root[0], 200)
        self.assertEqual(widget_path[0], 200)
        self.assertEqual(root[2], widget_path[2])

    def test_compact_query_uses_existing_template(self):
        template = self.template_text().encode()
        with self.demo_server(index=template) as server:
            normal = memory_get(*server, "/")
            compact = memory_get(*server, "/?compact=1")
        self.assertEqual(normal[2], compact[2])
        self.assertIn(b'params.get("compact")==="1"', compact[2])
        templates = [name for name in os.listdir(os.path.dirname(dashboard.TEMPLATE))
                     if name.endswith(".html")]
        self.assertEqual(templates, ["template.html"])

    def test_demo_widget_routes_never_collect(self):
        with mock.patch.object(widget.time, "time", return_value=NOW), \
                mock.patch.object(dashboard.collector, "run_collect",
                                  side_effect=AssertionError("demo collected")):
            with self.demo_server() as server:
                statuses = [memory_get(*server, route)[0] for route in
                            ("/usage.json", "/widget.json", "/widget.txt")]
        self.assertEqual(statuses, [200, 200, 200])

    def test_all_responses_have_security_headers(self):
        with mock.patch.object(widget.time, "time", return_value=NOW):
            with self.demo_server() as server:
                responses = [memory_get(*server, route) for route in
                             ("/", "/widget.json", "/missing")]
                responses.append(memory_get(*server, "/", "evil.example"))
        for _, headers, _ in responses:
            self.assertEqual(headers.get("cache-control"), "no-store")
            self.assertEqual(headers.get("x-content-type-options"), "nosniff")

    def test_no_response_enables_cors(self):
        with mock.patch.object(widget.time, "time", return_value=NOW):
            with self.demo_server() as server:
                responses = [memory_get(*server, route) for route in
                             ("/", "/usage.json", "/widget.json",
                              "/widget.txt", "/missing")]
        for _, headers, _ in responses:
            self.assertNotIn("access-control-allow-origin", headers)

    def test_nonloopback_host_is_rejected_for_every_route(self):
        with self.demo_server() as server:
            statuses = [memory_get(*server, route, "attacker.example")[0]
                        for route in ("/", "/widget", "/usage.json",
                                      "/widget.json", "/widget.txt", "/missing")]
        self.assertEqual(statuses, [403] * 6)

    def test_dashboard_dom_projection_uses_widget_trust_and_freshness(self):
        held = usage_account(routable=True, trust_state="held")
        cases = (
            (usage_snapshot(usage_account(), generated=NOW - 1000), "stale"),
            (usage_snapshot(held), "held"),
        )
        for snapshot, expected in cases:
            with self.subTest(expected=expected):
                display = dashboard.display_snapshot(snapshot, NOW)[
                    "_headroom_display"]
                central = widget.project(snapshot, NOW)
                self.assertEqual(display["accounts"][0]["state"], expected)
                self.assertEqual(display["accounts"][0]["state"],
                                 central["accounts"][0]["state"])
                for window in display["accounts"][0]["windows"].values():
                    self.assertIsNone(window["left_percent"])
                    self.assertEqual(window["tone"], "unknown")

    def test_dashboard_dom_projection_colors_and_cache_fallback(self):
        snapshot = usage_snapshot(
            usage_account("green", used5=20),
            usage_account("yellow", used5=60),
            usage_account("orange", used5=80),
            usage_account("red", used5=95))
        display = dashboard.display_snapshot(snapshot, NOW)["_headroom_display"]
        account = display["accounts"][0]
        self.assertEqual(account["state"], "current")
        self.assertEqual(account["windows"]["5h"]["left_percent"], 80.0)
        self.assertEqual(account["windows"]["5h"]["tone"], "green")
        self.assertEqual([row["windows"]["5h"]["tone"]
                          for row in display["accounts"]],
                         ["green", "yellow", "orange", "red"])
        limited = dashboard.display_snapshot(
            usage_snapshot(usage_account(used5=100)), NOW)[
                "_headroom_display"]["accounts"][0]
        self.assertEqual(limited["state"], "limited")
        self.assertEqual(limited["windows"]["5h"]["tone"], "red")
        self.assertEqual(limited["windows"]["7d"]["tone"], "unknown")
        forced = dashboard.display_snapshot(
            usage_snapshot(usage_account()), NOW, "cache_fallback")[
                "_headroom_display"]
        self.assertEqual(forced["accounts"][0]["state"], "stale")
        self.assertEqual(forced["accounts"][0]["windows"]["5h"]["tone"],
                         "unknown")
        script = self.template_text().split("<script>", 1)[1].split(
            "</script>", 1)[0]
        render_body = script.split("function render(data,forceNoncurrent){",
                                   1)[1].split("\n}", 1)[0]
        fallback = script.split("async function load(manual){", 1)[1].split(
            "/* --------------------------------------------------------------- theme */",
            1)[0]
        self.assertRegex(render_body,
                         r"sourceFailed=forceNoncurrent===true\|\|")
        self.assertRegex(fallback, r"render\(cached,true\)")

    def test_static_dashboard_injects_shared_thresholds_and_projection(self):
        config = {"schema_version": 1,
                  "dashboard": {"theme": "midnight", "title": "test"},
                  "accounts": []}
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "out")
            os.makedirs(output)
            source = os.path.join(output, "usage.json")
            with open(source, "w") as handle:
                json.dump(usage_snapshot(usage_account()), handle)
            with redirect_stdout(io.StringIO()), \
                    mock.patch.object(widget.time, "time", return_value=NOW):
                dashboard.build(config, output, source)
            with open(os.path.join(output, "index.html")) as handle:
                html = handle.read()
            with open(os.path.join(output, "usage.json")) as handle:
                payload = json.load(handle)
        match = re.search(r"const CONFIG = (\{.*?\});", html)
        self.assertIsNotNone(match)
        injected = json.loads(match.group(1))
        self.assertEqual(injected["snapshot_max_age"],
                         widget.SNAPSHOT_MAX_AGE)
        self.assertEqual(injected["observation_max_age"],
                         widget.OBSERVATION_MAX_AGE)
        self.assertEqual(payload["_headroom_display"]["accounts"][0][
            "windows"]["5h"]["tone"], "green")

    def test_widget_href_uses_actual_server_address_port(self):
        port = 49152
        with mock.patch.object(widget.time, "time", return_value=NOW), \
                self.demo_server() as server:
            status, _, body = memory_get(
                *server, "/widget.txt", server_port=port)
        body = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("Open dashboard | href=http://127.0.0.1:%d/" % port,
                      body)

    def test_compact_mode_retains_state_disclosure(self):
        # Compact may hide decorative chrome, but every element that
        # discloses state (snapshot age, per-account state, error/warning
        # statusline) must never be display:none'd in compact mode.
        template = self.template_text()
        compact_css = template.split("/* Compact mode", 1)[1].split("</style>", 1)[0]
        self.assertIn("body.is-compact .acct-identity, body.is-compact .state",
                      compact_css)
        disclosure = (".snapshot", ".state", ".acct-identity", ".account",
                      ".statusline.is-error", ".fleet-bars")
        for rule in compact_css.split("}"):
            if "display: none" not in rule:
                continue
            selectors = rule.split("{", 1)[0]
            for selector in disclosure:
                self.assertNotIn(selector + " ", selectors + " ")
            # the non-error statusline may hide; the error form must not
            self.assertNotIn(".statusline.is-error", selectors)
            self.assertNotIn(".snapshot", selectors)
        self.assertIn('class="statusline" id="status"', template)


class SwiftBarPluginTests(unittest.TestCase):
    @staticmethod
    def valid_body(port=8377):
        return ("headroom_widget_txt@1\n"
                "hr 1/1 · 80% | color=green\n"
                "---\n"
                "alpha · claude · CURRENT | color=green\n"
                "Refresh | refresh=true\n"
                f"Open dashboard | href=http://127.0.0.1:{port}/\n")

    @classmethod
    def run_plugin(cls, body, url="http://127.0.0.1:8377", local=False):
        with tempfile.TemporaryDirectory() as directory:
            body_path = os.path.join(directory, "body.txt")
            log_path = os.path.join(directory, "args.log")
            with open(body_path, "w") as handle:
                handle.write(body)
            env = os.environ.copy()
            env["HEADROOM_TEST_BODY"] = body_path
            env["HEADROOM_TEST_CURL_LOG"] = log_path
            if local:
                client = os.path.join(directory, "headroom-test")
                with open(client, "w") as handle:
                    handle.write(
                        "#!/bin/sh\n"
                        "printf '%s\\n' \"$@\" >\"$HEADROOM_TEST_CURL_LOG\"\n"
                        "cat \"$HEADROOM_TEST_BODY\"\n")
                os.chmod(client, 0o755)
                env.pop("HEADROOM_WIDGET_URL", None)
                env["HEADROOM_BIN"] = client
            else:
                client = os.path.join(directory, "curl")
                with open(client, "w") as handle:
                    handle.write(
                        "#!/bin/sh\n"
                        "printf '%s\\n' \"$@\" >\"$HEADROOM_TEST_CURL_LOG\"\n"
                        "[ -z \"${HEADROOM_TEST_CURL_EXIT:-}\" ] || exit \"$HEADROOM_TEST_CURL_EXIT\"\n"
                        "out=\n"
                        "seen=0\n"
                        "while [ \"$#\" -gt 0 ]; do\n"
                        "  case \"$1\" in\n"
                        "    --output) shift; out=$1 ;;\n"
                        "    --) shift; [ \"$#\" -eq 1 ] || exit 91; seen=1; break ;;\n"
                        "  esac\n"
                        "  shift\n"
                        "done\n"
                        "[ \"$seen\" -eq 1 ] && [ -n \"$out\" ] || exit 92\n"
                        "cp \"$HEADROOM_TEST_BODY\" \"$out\"\n")
                os.chmod(client, 0o755)
                env["PATH"] = directory + os.pathsep + env.get("PATH", "")
                env["HEADROOM_WIDGET_URL"] = url
                env.pop("HEADROOM_BIN", None)
            result = subprocess.run(
                [PLUGIN], env=env, text=True, capture_output=True,
                timeout=5, check=False)
            arguments = []
            if os.path.exists(log_path):
                with open(log_path) as handle:
                    arguments = handle.read().splitlines()
            return result, arguments

    @classmethod
    def run_failed_curl(cls):
        with mock.patch.dict(os.environ, {"HEADROOM_TEST_CURL_EXIT": "22"}):
            return cls.run_plugin(cls.valid_body())

    def test_plugin_filename_requests_one_minute_polling(self):
        self.assertEqual(os.path.basename(PLUGIN), "headroom.1m.sh")
        self.assertTrue(os.access(PLUGIN, os.X_OK))

    def test_plugin_local_mode_runs_installed_binary(self):
        result, arguments = self.run_plugin(self.valid_body(), local=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(arguments, ["widget-feed", "--swiftbar"])
        self.assertIn("hr 1/1 · 80% | color=green", result.stdout)

    def test_plugin_remote_mode_uses_bounded_curl(self):
        result, arguments = self.run_plugin(self.valid_body())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(arguments[-2:],
                         ["--", "http://127.0.0.1:8377/widget.txt"])
        self.assertIn("--fail", arguments)
        self.assertIn("--silent", arguments)
        self.assertEqual(arguments[arguments.index("--max-time") + 1], "3")
        self.assertEqual(arguments[arguments.index("--max-filesize") + 1],
                         "65536")
        self.assertNotIn("headroom_widget_txt@1", result.stdout)

    def test_plugin_rejects_missing_or_wrong_sentinel(self):
        for body in ("PWN | color=green\n", "wrong\nPWN | color=green\n"):
            with self.subTest(body=body):
                result, _ = self.run_plugin(body)
                self.assertIn("hr OFFLINE | color=gray", result.stdout)
                self.assertNotIn("PWN", result.stdout)

    def test_plugin_rejects_oversized_response(self):
        result, _ = self.run_plugin(
            "headroom_widget_txt@1\n" + "x" * 65536 + "\n")
        self.assertIn("hr OFFLINE | color=gray", result.stdout)
        self.assertNotIn("x" * 100, result.stdout)

    def test_plugin_curl_failure_is_visible_offline(self):
        result, arguments = self.run_failed_curl()
        self.assertNotEqual(arguments, [])
        self.assertEqual(result.returncode, 0)
        self.assertIn("hr OFFLINE | color=gray", result.stdout)

    def test_plugin_rejects_hostile_fetched_parameter_sections(self):
        attacks = (
            "headroom_widget_txt@1\nPWN | shell=/bin/sh param1=-c\n",
            self.valid_body().replace(
                "color=green\n", "color=green shell=/bin/sh\n", 1),
            self.valid_body().replace(
                "href=http://127.0.0.1:8377/",
                "href=http://127.0.0.1:8377@evil.example/"),
        )
        for body in attacks:
            with self.subTest(body=body):
                result, _ = self.run_plugin(body)
                self.assertEqual(result.returncode, 0)
                self.assertIn("hr OFFLINE | color=gray", result.stdout)
                self.assertNotIn("PWN", result.stdout)
                self.assertNotIn("shell=", result.stdout)
                self.assertNotIn("evil.example", result.stdout)

    def test_plugin_rejects_hostile_url_before_curl(self):
        attacks = (
            "http://127.0.0.1:8377 | shell=/bin/sh",
            "http://localhost:8377@evil.example/widget.txt",
            "http://127.0.0.1:8377/widget.txt?x=1",
            "http://127.0.0.1:0/widget.txt",
            "https://127.0.0.1:8377/widget.txt",
        )
        for url in attacks:
            with self.subTest(url=url):
                result, arguments = self.run_plugin(self.valid_body(), url)
                self.assertEqual(arguments, [])
                self.assertIn("hr OFFLINE | color=gray", result.stdout)
                self.assertIn("href=http://127.0.0.1:8377/", result.stdout)
                self.assertNotIn("shell=", result.stdout)
                self.assertNotIn("evil.example", result.stdout)

    def test_plugin_canonicalizes_localhost_origin(self):
        result, arguments = self.run_plugin(
            self.valid_body(49152), "http://localhost:49152/widget.txt")
        self.assertEqual(arguments[-1],
                         "http://127.0.0.1:49152/widget.txt")
        self.assertIn("href=http://127.0.0.1:49152/", result.stdout)


class ExperimentalWindowsTests(unittest.TestCase):
    @staticmethod
    def script():
        with open(WINDOWS_SCRIPT) as handle:
            return handle.read()

    def test_windows_script_uses_application_context(self):
        script = self.script()
        self.assertIn("New-Object System.Windows.Forms.ApplicationContext", script)
        self.assertIn("[System.Windows.Forms.Application]::Run($script:Context)",
                      script)
        self.assertIn("System.Windows.Forms.NotifyIcon", script)

    def test_windows_script_maps_all_four_states_to_static_icons(self):
        script = self.script()
        expected = {"green", "amber", "red", "gray"}
        for state in expected:
            name = "headroom-%s.ico" % state
            self.assertIn(name, script)
            path = os.path.join(WINDOWS_ICONS, name)
            self.assertTrue(os.path.isfile(path))
            with open(path, "rb") as handle:
                header = struct.unpack("<HHH", handle.read(6))
            self.assertEqual(header, (0, 1, 3))

    def test_windows_tooltip_is_capped_at_63_characters(self):
        script = self.script()
        assignments = [line.strip() for line in script.splitlines()
                       if "$script:Tray.Text =" in line]
        self.assertEqual(len(assignments), 1)
        self.assertIn(".Substring(0, [Math]::Min(63, $Tooltip.Length))",
                      assignments[0])

    def test_windows_context_menu_has_refresh_and_open_dashboard(self):
        script = self.script()
        self.assertIn('ToolStripMenuItem("Refresh")', script)
        self.assertIn("$refreshItem.add_Click({ Refresh-Headroom })", script)
        self.assertIn('ToolStripMenuItem("Open dashboard")', script)
        self.assertIn("$openItem.add_Click({ Start-Process $DashboardUrl })", script)

    def test_windows_failure_always_selects_gray_offline(self):
        script = self.script()
        refresh = script.split("function Refresh-Headroom {", 1)[1].split(
            "\n}\n\n$menu", 1)[0]
        self.assertEqual(refresh.count("\n    catch {"), 1)
        attempt, failure = refresh.split("\n    catch {", 1)
        self.assertRegex(failure, r'^\s*Set-TrayStatus "gray" '
                         r'"headroom OFFLINE"\s*\}\s*$')
        thrown = set(re.findall(r'throw "([^"]+)"', attempt))
        self.assertEqual(thrown, {
            "widget response too large", "widget schema mismatch",
            "widget is not current", "widget clock invalid",
            "widget fields missing", "widget counts invalid",
            "widget percentage invalid",
        })
        guards = (
            r'if \(\[Text\.Encoding\]::UTF8\.GetByteCount\('
            r'\$response\.Content\) -gt 65536\) \{\s*'
            r'throw "widget response too large"\s*\}',
            r'if \(\$data\.schema -ne "headroom_widget@1"\) '
            r'\{ throw "widget schema mismatch" \}',
            r'if \(\$null -eq \$data\.freshness -or '
            r'\$data\.freshness\.state -ne "current"\) \{\s*'
            r'throw "widget is not current"\s*\}',
            r'if \(\$null -eq \$data\.accounts -or '
            r'\$null -eq \$data\.headline\) \{\s*'
            r'throw "widget fields missing"\s*\}',
            r'if \(\$evaluatedAt -gt \$now -or '
            r'\(\$now - \$evaluatedAt\) -gt 300 -or\s*'
            r'\$ageSeconds -lt 0 -or \$ageSeconds -gt 900\) \{\s*'
            r'throw "widget clock invalid"\s*\}',
            r'if \(\$current -lt 0 -or \$total -lt \$current -or\s*'
            r'\$total -ne \$accountCount\) \{ throw "widget counts invalid" \}',
            r'if \(\[Double\]::IsNaN\(\$percent\) -or '
            r'\[Double\]::IsInfinity\(\$percent\) -or\s*'
            r'\$percent -lt 0 -or \$percent -gt 100\) \{\s*'
            r'throw "widget percentage invalid"\s*\}',
        )
        for guard in guards:
            self.assertRegex(attempt, guard)

    def test_windows_script_has_no_gdi_or_rotation_actions(self):
        script = self.script().lower()
        for forbidden in ("system.drawing.bitmap", "graphics", "drawicon",
                          "rotate", "headroom mark", "headroom clear",
                          "headroom pick", "headroom env"):
            self.assertNotIn(forbidden, script)


class WidgetDocumentationTests(unittest.TestCase):
    @staticmethod
    def readme():
        with open(os.path.join(ROOT, "README.md")) as handle:
            return handle.read()

    def test_readme_documents_widget_security_and_ssh_only_remote_path(self):
        readme = self.readme()
        widgets = readme.split("## Widgets", 1)[1].split("## The commands", 1)[0]
        self.assertIn("ssh -N -L 8377:127.0.0.1:8377", widgets)
        self.assertIn("only supported remote pattern", widgets)
        for constraint in ("loopback-only", "Host", "no CORS", "no-store",
                           "nosniff", "never evaluates", "64 KB"):
            self.assertIn(constraint, widgets)

    def test_readme_labels_windows_experimental(self):
        readme = self.readme()
        windows = readme.split("### Windows tray — EXPERIMENTAL", 1)[1].split(
            "## The commands", 1)[0]
        self.assertIn("not stable or supported", windows)
        self.assertIn("powershell -ExecutionPolicy Bypass -File experimental/windows/headroom-tray.ps1",
                      windows)
        self.assertIn("Windows 10/11 PowerShell 5.1", windows)

    def test_widgets_hero_capture_exists(self):
        readme = self.readme()
        reference = ("![Menu bar widget and compact dashboard, rendered from "
                     "live fleet data](marketing/hr-widgets.png)")
        self.assertIn(reference, readme)
        self.assertTrue(os.path.exists(os.path.join(ROOT,
                                                    "marketing/hr-widgets.png")))


if __name__ == "__main__":
    unittest.main()
