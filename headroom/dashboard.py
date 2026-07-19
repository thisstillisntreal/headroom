"""Build and serve the themed usage dashboard.

`build` renders ``dashboard/template.html`` with the user's settings injected
into one JSON block and writes it next to the public snapshot, so the whole
dashboard is two static files: ``index.html`` + ``usage.json``. Host them
anywhere — or don't: `serve` runs a tiny local server whose ``/usage.json``
transparently re-collects when the snapshot is stale, so the page is always
current with zero cron setup.
"""
import http.server
import ipaddress
import json
import math
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass

from . import burn as burn_mod
from . import collect as collector
from . import history as history_mod
from . import insights as insights_mod
from . import manage, paths, registry, usage_ledger, widget


# Web pages for providers that use API keys instead of CLI OAuth
PROVIDER_LOGIN_URLS = {
    "manus": "https://manus.im/app?show_settings=integrations&app_name=api",
    "nvidia": "https://build.nvidia.com/settings/api-keys",
}


def _shell_quote(value):
    import shlex
    return shlex.quote(str(value or ""))


def _owned_slot_home(name, home):
    """True when home is headroom's isolated homes/<name> (safe to refresh)."""
    if not name or not home:
        return False
    try:
        owned = os.path.realpath(os.path.join(paths.homes_dir(), name))
        return os.path.realpath(home) == owned
    except (OSError, ValueError):
        return False


def login_plan(provider, name=None, home=None, mode="reconnect"):
    """Describe how to log in for a provider (CLI + browser URL when useful).

    Claude/Codex/Grok use headroom's connect / auth-refresh handshake so the
    provider CLI opens its own browser OAuth — not a hand-rolled shell script.
    Returns a dict with command, browser_url, kind, instructions — never secrets.
    """
    provider = (provider or "claude").lower()
    name = (name or provider).strip()
    home = home or ""
    mode = (mode or "reconnect").lower()
    is_fresh = mode in ("fresh", "add", "new")

    if provider == "claude":
        # Fresh slot → headroom connect (isolated home + claude auth login +
        # identity verify + register). Reconnect owned → auth refresh.
        # Keychain / adopted → bare claude auth login in that home.
        if is_fresh:
            cmd = (
                f'headroom connect {_shell_quote(name)} --provider claude; '
                f'echo; headroom collect; '
                f'echo; echo "Done — refresh the dashboard for {name}."'
            )
            return {
                "kind": "cli_oauth",
                "provider": provider,
                "name": name,
                "home": home or os.path.join(paths.homes_dir(), name),
                "command": cmd,
                "browser_url": None,
                "uses_connect": True,
                "instructions": (
                    "Terminal opens headroom connect → Claude sign-in "
                    "(browser OAuth handshake)."
                ),
            }
        owned = _owned_slot_home(name, home)
        cred_file = os.path.join(home, ".credentials.json") if home else ""
        if owned and home and os.path.isfile(cred_file):
            cmd = (
                f'headroom auth refresh {_shell_quote(name)}; '
                f'echo; headroom collect; '
                f'echo; echo "Done — refresh the dashboard for {name}."'
            )
            return {
                "kind": "cli_oauth",
                "provider": provider,
                "name": name,
                "home": home,
                "command": cmd,
                "browser_url": None,
                "uses_connect": True,
                "instructions": (
                    "Terminal opens headroom auth refresh → Claude re-sign-in "
                    "(browser OAuth handshake)."
                ),
            }
        if not home:
            home = os.path.expanduser("~/.claude")
        # Default ~/.claude must NOT set CLAUDE_CONFIG_DIR — that makes the
        # CLI miss the shared macOS Keychain token and appear logged out.
        from .collect import is_default_claude_home
        if is_default_claude_home(home):
            cmd = (
                f'claude auth login; '
                f'echo; headroom collect; '
                f'echo; echo "Done — refresh the dashboard for {name}."'
            )
        else:
            cmd = (
                f'export CLAUDE_CONFIG_DIR={_shell_quote(home)}; '
                f'claude auth login; '
                f'echo; headroom collect; '
                f'echo; echo "Done — refresh the dashboard for {name}."'
            )
        return {
            "kind": "cli_oauth",
            "provider": provider,
            "name": name,
            "home": home,
            "command": cmd,
            "browser_url": None,
            "instructions": (
                "Terminal opens Claude sign-in (browser OAuth). "
                "Complete it, then the dashboard will pick up usage."
            ),
        }
    if provider == "codex":
        if is_fresh:
            cmd = (
                f'headroom connect {_shell_quote(name)} --provider codex; '
                f'echo; headroom collect; '
                f'echo; echo "Done — refresh the dashboard for {name}."'
            )
            return {
                "kind": "cli_oauth",
                "provider": provider,
                "name": name,
                "home": home or os.path.join(paths.homes_dir(), name),
                "command": cmd,
                "browser_url": None,
                "uses_connect": True,
                "instructions": (
                    "Terminal opens headroom connect → Codex / ChatGPT sign-in "
                    "(browser OAuth handshake)."
                ),
            }
        if not home:
            home = os.path.expanduser("~/.codex")
        cmd = (
            f'export CODEX_HOME={_shell_quote(home)}; '
            f'codex login; '
            f'echo; headroom collect; '
            f'echo; echo "Done — refresh the dashboard for {name}."'
        )
        return {
            "kind": "cli_oauth",
            "provider": provider,
            "name": name,
            "home": home,
            "command": cmd,
            "browser_url": None,
            "instructions": (
                "Terminal opens Codex sign-in (browser OAuth). "
                "Complete it, then the dashboard will pick up usage."
            ),
        }
    if provider == "grok":
        if is_fresh:
            cmd = (
                f'headroom connect {_shell_quote(name)} --provider grok; '
                f'echo; headroom collect; '
                f'echo; echo "Done — refresh the dashboard for {name}."'
            )
            return {
                "kind": "cli_oauth",
                "provider": provider,
                "name": name,
                "home": home or os.path.join(paths.homes_dir(), name),
                "command": cmd,
                "browser_url": None,
                "uses_connect": True,
                "instructions": (
                    "Terminal opens headroom connect → Grok sign-in "
                    "(browser OAuth handshake)."
                ),
            }
        if not home:
            home = os.path.expanduser("~/.grok")
        cmd = (
            f'export GROK_HOME={_shell_quote(home)}; '
            f'grok login; '
            f'echo; headroom collect; '
            f'echo; echo "Done — refresh the dashboard for {name}."'
        )
        return {
            "kind": "cli_oauth",
            "provider": provider,
            "name": name,
            "home": home,
            "command": cmd,
            "browser_url": None,
            "instructions": (
                "Terminal opens Grok sign-in (browser OAuth). "
                "Complete it, then the dashboard will pick up usage."
            ),
        }
    if provider == "manus":
        url = PROVIDER_LOGIN_URLS["manus"]
        return {
            "kind": "api_key",
            "provider": provider,
            "name": name,
            "home": home or "",
            "command": None,
            "browser_url": url,
            "instructions": (
                "Browser opens Manus API settings — create a key, then paste it "
                "in headroom → + Accounts → Manus."
            ),
            "open_manage": "manus",
        }
    if provider == "nvidia":
        url = PROVIDER_LOGIN_URLS["nvidia"]
        return {
            "kind": "api_key",
            "provider": provider,
            "name": name,
            "home": home or "",
            "command": None,
            "browser_url": url,
            "instructions": (
                "Browser opens NVIDIA Build API keys — copy a free key, then "
                "paste it in headroom → Insights / NVIDIA."
            ),
            "open_manage": "nvidia",
        }
    return {
        "kind": "cli_oauth",
        "provider": provider,
        "name": name,
        "home": home,
        "command": f"headroom connect {_shell_quote(name)} --provider {provider}",
        "browser_url": None,
        "instructions": "Terminal will open headroom connect.",
    }


def reconnect_command(provider, name, home):
    """Back-compat helper — returns the shell command only."""
    return login_plan(provider, name=name, home=home).get("command") or ""

TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dashboard", "template.html")
SERVE_MAX_AGE = paths.env_int("HEADROOM_SERVE_MAX_AGE", 300)
FAILURE_BACKOFF_BASE = paths.env_int("HEADROOM_SERVE_FAILURE_BACKOFF_BASE", 5)
FAILURE_BACKOFF_CAP = paths.env_int("HEADROOM_SERVE_FAILURE_BACKOFF_CAP", 300)


def display_snapshot(snapshot, evaluated_at=None, force_noncurrent_reason=None):
    """Attach the central display projection consumed by dashboard JavaScript."""
    value = dict(snapshot)
    value["_headroom_display"] = widget.project_dashboard(
        snapshot, evaluated_at, force_noncurrent_reason)
    return value


@dataclass(frozen=True)
class RefreshResult:
    snapshot: object
    refresh_failed: bool = False
    reason: object = None


def _within_freshness_window(snapshot, clock=time.time):
    """True while the snapshot's age is inside the widget freshness window
    (the same bound the projection itself demotes on)."""
    generated = RefreshGate._generated(snapshot)
    if generated is None:
        return False
    age = clock() - generated
    return 0 <= age <= widget.SNAPSHOT_MAX_AGE


class RefreshGate:
    """Single-flight collection with success TTL and bounded failure retry."""

    def __init__(self, success_ttl=SERVE_MAX_AGE,
                 failure_base=FAILURE_BACKOFF_BASE,
                 failure_cap=FAILURE_BACKOFF_CAP, clock=None):
        self.success_ttl = success_ttl
        self.failure_base = failure_base
        self.failure_cap = failure_cap
        self.clock = clock or time.time
        self.failure_count = 0
        self.retry_at = 0.0
        self.last_delay = 0.0
        self._last_success_at = None
        self._collecting = False
        self._condition = threading.Condition()

    @staticmethod
    def _generated(snapshot):
        value = snapshot.get("generated") if isinstance(snapshot, dict) else None
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value)):
            return value
        return None

    def _published_current(self, snapshot, now):
        generated = self._generated(snapshot)
        return (generated is not None and 0 <= now - generated
                <= self.success_ttl)

    def get(self, load_snapshot, collect_snapshot):
        """Return one snapshot result; only the admitted caller may collect."""
        while True:
            with self._condition:
                now = self.clock()
                snapshot = load_snapshot()
                if self._last_success_at is None \
                        and self._published_current(snapshot, now):
                    self._last_success_at = self._generated(snapshot)
                if (self._last_success_at is not None
                        and now - self._last_success_at < self.success_ttl):
                    return RefreshResult(snapshot)
                if now < self.retry_at:
                    return RefreshResult(snapshot, True, "refresh_failed")
                if self._collecting:
                    self._condition.wait()
                    continue
                self._collecting = True
                break

        try:
            collect_snapshot()
            completed = self.clock()
            snapshot = load_snapshot()
            if not self._published_current(snapshot, completed):
                raise RuntimeError("collector did not publish a current snapshot")
        except Exception:  # noqa: BLE001 — callers receive stale/503, never live
            with self._condition:
                self.failure_count += 1
                self.last_delay = min(
                    self.failure_base if self.last_delay <= 0
                    else self.last_delay * 2,
                    self.failure_cap)
                self.retry_at = self.clock() + self.last_delay
                self._collecting = False
                self._condition.notify_all()
                return RefreshResult(load_snapshot(), True, "refresh_failed")
        with self._condition:
            self.failure_count = 0
            self.retry_at = 0.0
            self.last_delay = 0.0
            self._last_success_at = self.clock()
            self._collecting = False
            self._condition.notify_all()
            return RefreshResult(snapshot)


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_demo(out_dir=None):
    """Render the dashboard from the bundled sample data — no accounts, no
    config, no network. Lets anyone preview it in seconds before connecting."""
    import time
    sample = os.path.join(_repo_root(), "examples", "usage.sample.json")
    with open(sample) as handle:
        data = json.load(handle)
    now = int(time.time())
    data["generated"] = now - 30
    resets = {
        "5h": now + 2 * 3600 + 11 * 60,
        "7d": now + 3 * 86400,
        "month": now + 15 * 86400,
    }
    for account in data.get("accounts", []):
        account["captured_at"] = now - 30
        for key, window in (account.get("windows") or {}).items():
            window["resets_at"] = resets.get(key, resets["7d"])
            if "observed_at" in window:
                window["observed_at"] = now - 30
        sub = account.get("subscription")
        if sub and sub.get("status") == "active_through":
            sub["active_until"] = now + 21 * 86400
            sub["checked_at"] = now - 3600
    out_dir = out_dir or os.path.join(paths.base_dir(), "demo")
    os.makedirs(out_dir, exist_ok=True)
    demo_config = {"schema_version": 1,
                   "dashboard": {"theme": "midnight", "title": "headroom (demo)"},
                   "accounts": [{"name": a["name"], "provider": a["provider"],
                                 "home": "/tmp/demo/" + a["name"]}
                                for a in data["accounts"]]}
    build(demo_config, out_dir)
    with open(os.path.join(out_dir, "usage.json"), "w") as handle:
        json.dump(display_snapshot(data), handle, allow_nan=False)
    return out_dir


def _static_src_dir():
    return os.path.join(os.path.dirname(TEMPLATE), "static")


def dashboard_js_source():
    """Full dashboard app JS (extracted from the shell monologue)."""
    path = os.path.join(_static_src_dir(), "js", "app.js")
    with open(path) as handle:
        return handle.read()


def dashboard_css_source():
    """Full dashboard CSS (extracted from the shell monologue)."""
    path = os.path.join(_static_src_dir(), "css", "dashboard.css")
    with open(path) as handle:
        return handle.read()


def _copy_dashboard_static(out_dir):
    """Publish CSS/JS next to index.html so the shell can load partials."""
    import shutil
    src = _static_src_dir()
    if not os.path.isdir(src):
        return
    dest = os.path.join(out_dir, "static")
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def build(config=None, out_dir=None, snapshot_file=None):
    config = registry.load() if config is None else config
    settings = registry.dashboard_settings(config)
    out_dir = paths.public_dir() if out_dir is None else out_dir
    os.makedirs(out_dir, exist_ok=True)
    with open(TEMPLATE) as handle:
        html = handle.read()
    provider_order = settings.get("provider_order") or list(registry.PROVIDERS)
    # Ensure complete known list for the UI
    order = []
    for name in provider_order:
        if name in registry.PROVIDERS and name not in order:
            order.append(name)
    for name in registry.PROVIDERS:
        if name not in order:
            order.append(name)
    injected = {
        "theme": settings["theme"],
        "title": settings["title"],
        "redact": bool(settings.get("redact_emails", True)),
        "snapshot_max_age": widget.SNAPSHOT_MAX_AGE,
        "observation_max_age": widget.OBSERVATION_MAX_AGE,
        "provider_order": order,
        "accounts": [{"name": account["name"], "provider": account["provider"]}
                     for account in registry.accounts(config)],
    }
    # script-safe serialization: <, >, & escaped so a hostile title/name can
    # never terminate the <script> element (stored XSS via config)
    payload = (json.dumps(injected, indent=None)
               .replace("<", "\\u003c").replace(">", "\\u003e")
               .replace("&", "\\u0026"))
    html = html.replace("/*__HEADROOM_CONFIG__*/ null", payload)
    index = os.path.join(out_dir, "index.html")
    with open(index, "w") as handle:
        handle.write(html)
    _copy_dashboard_static(out_dir)
    target = os.path.join(out_dir, "usage.json")
    if snapshot_file and os.path.exists(snapshot_file):
        with open(snapshot_file) as handle:
            snapshot = json.load(handle)
        with open(target, "w") as handle:
            json.dump(display_snapshot(snapshot), handle, allow_nan=False)
    print(f"dashboard built: {index}")
    return index


class Handler(http.server.SimpleHTTPRequestHandler):
    demo = False
    refresh_gate = RefreshGate()

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format, *args):  # noqa: A002 — stdlib signature
        pass

    # Dashboard shell loads same-origin static CSS/JS (static/css, static/js)
    # plus a small inline CONFIG bootstrap. 'self' is required for those
    # partials; 'unsafe-inline' remains for the CONFIG bootstrap only.
    # No frames/objects/forms/external hosts — still safe inside webviews.
    _CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-src 'none'; object-src 'none'; "
            "form-action 'none'; base-uri 'none'")

    def end_headers(self):
        # Every response, including static errors and Host rejections, carries
        # the same browser hardening and cannot be cached as a live reading.
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("content-security-policy", self._CSP)
        super().end_headers()

    def _host_ok(self):
        # reject anything but a loopback Host, so a remote page can't reach the
        # server via DNS-rebinding and read the usage feed cross-origin.
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            return False
        if raw.startswith("["):            # [::1]:port
            host = raw[1:].split("]")[0]
        elif raw.count(":") == 1:          # host:port (IPv4 or name)
            host = raw.split(":")[0]
        else:                              # bare name or bracketless IPv6
            host = raw
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def _dashboard_href(self):
        # the port this server is actually bound to, so a tunneled client's
        # "Open dashboard" link points at the same tunnel it fetched through
        try:
            return f"http://127.0.0.1:{self.server.server_address[1]}/"
        except (AttributeError, IndexError, TypeError):
            return None

    def _reject_non_loopback(self):
        if self._host_ok():
            return False
        self.send_response(403)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"forbidden: non-loopback Host")
        return True

    def do_GET(self):
        if self._reject_non_loopback():
            return
        route = urllib.parse.urlsplit(self.path).path
        if route in ("/usage.json", "/widget.json", "/widget.txt"):
            self._serve_feed(route)
            return
        if route in ("/api/accounts", "/api/meta", "/api/history",
                     "/api/insights", "/api/insights/status",
                     "/api/burn", "/api/burn/check", "/api/usage"):
            self._api_get(route)
            return
        if route == "/widget":
            original = self.path
            self.path = "/index.html"
            try:
                super().do_GET()
            finally:
                self.path = original
            return
        super().do_GET()

    def do_POST(self):
        if self._reject_non_loopback():
            return
        route = urllib.parse.urlsplit(self.path).path
        if route.startswith("/api/"):
            self._api_mutate("POST", route)
            return
        self._send_json(404, {"error": "not found"})

    def do_PATCH(self):
        if self._reject_non_loopback():
            return
        route = urllib.parse.urlsplit(self.path).path
        if route.startswith("/api/"):
            self._api_mutate("PATCH", route)
            return
        self._send_json(404, {"error": "not found"})

    def do_DELETE(self):
        if self._reject_non_loopback():
            return
        route = urllib.parse.urlsplit(self.path).path
        if route.startswith("/api/"):
            self._api_mutate("DELETE", route)
            return
        self._send_json(404, {"error": "not found"})

    def _read_json_body(self):
        try:
            length = int(self.headers.get("content-length") or "0")
        except ValueError:
            length = 0
        if length < 0 or length > 1_000_000:
            raise manage.ManageError("request body too large", 413)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise manage.ManageError("invalid JSON body", 400) from error
        if not isinstance(value, dict):
            raise manage.ManageError("JSON body must be an object", 400)
        return value

    def _send_json(self, status, payload):
        body = json.dumps(payload, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
        self._send_body(status, "application/json; charset=utf-8", body)

    def _api_get(self, route):
        if route == "/api/history":
            # History is local + non-secret (redacted emails already in log
            # when redact_emails was on at collect). Available in demo too
            # if a log exists; otherwise empty series.
            try:
                query = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query)
                range_name = (query.get("range") or ["week"])[0]
                metric = (query.get("metric") or ["left"])[0]
                payload = history_mod.query(range_name=range_name, metric=metric)
                self._send_json(200, payload)
            except Exception as error:  # noqa: BLE001
                self._send_json(500, {
                    "error": "history error: " + type(error).__name__})
            return
        if route == "/api/insights/status":
            try:
                self._send_json(200, insights_mod.status())
            except Exception as error:  # noqa: BLE001
                self._send_json(500, {
                    "error": "insights status error: " + type(error).__name__})
            return
        if route == "/api/insights":
            try:
                query = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query)
                force = (query.get("force") or ["0"])[0] in ("1", "true", "yes")
                payload = insights_mod.generate(force=force)
                status = 200 if payload.get("ok") else 400
                if payload.get("error") == "provider_error":
                    status = 502
                self._send_json(status, payload)
            except Exception as error:  # noqa: BLE001
                self._send_json(500, {
                    "error": "insights error: " + type(error).__name__})
            return
        if route == "/api/burn":
            try:
                self._send_json(200, burn_mod.list_status())
            except Exception as error:  # noqa: BLE001
                self._send_json(500, {
                    "error": "burn error: " + type(error).__name__})
            return
        if route == "/api/burn/check":
            try:
                query = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query)
                force = (query.get("force") or ["0"])[0] in ("1", "true", "yes")
                self._send_json(200, burn_mod.check_and_notify(force=force))
            except Exception as error:  # noqa: BLE001
                self._send_json(500, {
                    "error": "burn check error: " + type(error).__name__})
            return
        if route == "/api/usage":
            try:
                self._send_json(200, usage_ledger.summary())
            except Exception as error:  # noqa: BLE001
                self._send_json(500, {
                    "error": "usage error: " + type(error).__name__})
            return
        if self.demo:
            self._send_json(403, {
                "error": "account management disabled in demo mode",
                "demo": True,
            })
            return
        try:
            state = manage.list_state()
            if route == "/api/meta":
                self._send_json(200, {
                    "providers": state["providers"],
                    "default_homes": state["default_homes"],
                    "detected": state["detected"],
                    "cost_hints": state["cost_hints"],
                    "dashboard": state["dashboard"],
                    "suggested_names": state.get("suggested_names"),
                    "multi_account": state.get("multi_account"),
                    "by_provider": state.get("by_provider"),
                })
            else:
                self._send_json(200, {
                    "accounts": state["accounts"],
                    "dashboard": state["dashboard"],
                    "by_provider": state.get("by_provider"),
                    "suggested_names": state.get("suggested_names"),
                    "detected": state.get("detected"),
                    "multi_account": state.get("multi_account"),
                })
        except manage.ManageError as error:
            self._send_json(error.status, {"error": str(error)})
        except Exception as error:  # noqa: BLE001
            self._send_json(500, {"error": "internal error: " + type(error).__name__})

    def _api_mutate(self, method, route):
        # Domain routing lives in dashboard_api (focused handlers).
        from . import dashboard_api
        dashboard_api.handle_mutate(self, method, route)

    def _api_add_account(self, body):
        mode = (body.get("mode") or body.get("action") or "adopt").lower()
        name = body.get("name")
        provider = (body.get("provider") or "").lower()
        cost = body.get("monthly_cost_usd")
        email = body.get("expected_email") or body.get("email")
        if mode in ("manus", "manus_key", "api_key"):
            return manage.add_manus(
                name, body.get("api_key") or body.get("key"),
                email=email, monthly_cost_usd=cost)
        if mode in ("nvidia", "nvidia_key", "nim"):
            return manage.add_nvidia(
                name,
                api_key=body.get("api_key") or body.get("key"),
                email=email,
                monthly_cost_usd=0 if cost is None else cost,
                use_insights_key=bool(body.get("use_insights_key")),
            )
        if mode in ("prepare", "fresh", "prepare_fresh"):
            # Multi-account: create isolated home; user logs in via CLI
            if not name:
                name = manage.suggest_name(provider)
            prepared = manage.prepare_fresh(
                name, provider, monthly_cost_usd=cost)
            return prepared
        if mode in ("finish", "finish_fresh", "complete"):
            return manage.finish_fresh(
                name, provider, monthly_cost_usd=cost,
                expected_email=email)
        if mode in ("adopt", "connect"):
            if not name and provider:
                name = manage.suggest_name(provider)
            if provider == "nvidia":
                # adopt path can still use a key-based connect when home has auth
                return manage.adopt_account(
                    name, "nvidia", home=body.get("home"),
                    monthly_cost_usd=0 if cost is None else cost,
                    expected_email=email)
            return manage.adopt_account(
                name, provider, home=body.get("home"),
                monthly_cost_usd=cost, expected_email=email)
        raise manage.ManageError(
            "mode must be 'adopt', 'prepare'/'fresh', 'finish', 'manus', or 'nvidia'",
            400)

    def _after_mutate(self):
        """Rebuild after mutations that need live capacity (add account)."""
        from . import dashboard_api
        dashboard_api.after_config_change(collect=True)

    def _api_open_terminal(self, body):
        """Start a provider login from the dashboard (loopback only).

        * Existing account (by name) → re-login that slot's home
        * New account (provider only, or mode=fresh) → prepare isolated home
          then open CLI login (browser OAuth opens automatically)
        * Manus / NVIDIA → open the API-key page in the browser + manage UI

        Commands are built server-side; the browser cannot inject shell.
        """
        import subprocess

        name = body.get("name") or body.get("account")
        provider = (body.get("provider") or "").lower().strip()
        mode = (body.get("mode") or "reconnect").lower()
        prepared = None
        home = body.get("home") or ""

        # Resolve from registered account when possible
        account = None
        if isinstance(name, str) and name.strip():
            name = name.strip()
            try:
                accounts = registry.accounts()
            except registry.RegistryError:
                accounts = []
            account = next((a for a in accounts if a.get("name") == name), None)
            if account:
                provider = account.get("provider") or provider or "claude"
                home = account.get("home") or home

        # Fresh multi-account login: headroom connect creates the isolated
        # home itself (identity handshake + register). Only pre-create a home
        # when the plan will run bare `claude/codex/grok login` into it.
        wants_fresh = mode in ("fresh", "add", "new") or (
            not account and provider in ("claude", "codex", "grok")
            and body.get("prepare", True) is not False and not home)
        if wants_fresh:
            if not provider:
                raise manage.ManageError("provider required for new login", 400)
            if provider in ("claude", "codex", "grok") and not name:
                name = manage.suggest_name(provider)
            elif not name:
                name = manage.suggest_name(provider)

        if not provider and not account:
            raise manage.ManageError("provider or account name required", 400)
        if not name:
            name = provider or "account"

        # Fast path: Claude already signed in on this Mac (default ~/.claude
        # Keychain login). Adopt it into headroom — no Terminal, no re-auth.
        if (provider == "claude" and not account
                and (wants_fresh or mode == "reconnect")):
            try:
                existing_claude = [
                    a for a in (registry.accounts() or [])
                    if a.get("provider") == "claude"
                ]
            except registry.RegistryError:
                existing_claude = []
            if not existing_claude:
                default_home = os.path.expanduser("~/.claude")
                try:
                    from . import collect as collect_mod
                    identity = collect_mod.claude_identity(default_home)
                except Exception:  # noqa: BLE001
                    identity = None
                if identity and identity.get("email"):
                    try:
                        adopted = manage.adopt_account(
                            name or manage.suggest_name("claude"),
                            "claude",
                            home=default_home,
                            monthly_cost_usd=body.get("monthly_cost_usd"),
                            expected_email=identity.get("email"),
                        )
                        try:
                            collector.run_collect(quiet=True)
                        except Exception:  # noqa: BLE001
                            pass
                        self._send_json(200, {
                            "ok": True,
                            "kind": "adopted_existing",
                            "command": None,
                            "browser_url": None,
                            "browser_launched": False,
                            "terminal_launched": False,
                            "account": adopted.get("name") or name,
                            "provider": "claude",
                            "home": default_home,
                            "prepared": None,
                            "adopted": adopted,
                            "instructions": (
                                f"Claude already signed in as "
                                f"{identity.get('email')} — adopted into "
                                f"headroom. Refresh the dashboard."
                            ),
                            "open_manage": None,
                            "error": None,
                            "hint": "Already logged in — no browser needed.",
                        })
                        return
                    except manage.ManageError:
                        pass  # fall through to Terminal login

        # Force fresh mode when opening a brand-new provider slot
        if wants_fresh and not account:
            mode = "fresh"

        plan = login_plan(provider, name=name, home=home, mode=mode)
        command = plan.get("command")
        browser_url = plan.get("browser_url")

        # Bare-CLI path (not headroom connect): pre-create isolated home so
        # CLAUDE_CONFIG_DIR / CODEX_HOME / GROK_HOME points at a real slot.
        if (wants_fresh and not account and not plan.get("uses_connect")
                and provider in ("claude", "codex", "grok")):
            try:
                prepared = manage.prepare_fresh(name, provider)
                home = prepared.get("home") or home
                name = prepared.get("name") or name
                plan = login_plan(provider, name=name, home=home, mode=mode)
                command = plan.get("command")
            except manage.ManageError:
                raise
            except Exception as error:  # noqa: BLE001
                raise manage.ManageError(str(error), 400) from error

        terminal_launched = False
        browser_launched = False
        error_note = None

        # Open browser for API-key providers (and any explicit URL)
        if browser_url:
            try:
                webbrowser.open(browser_url)
                browser_launched = True
            except Exception as error:  # noqa: BLE001
                error_note = "browser: " + str(error)[:80]

        # Open Terminal for CLI OAuth logins
        if command:
            try:
                if sys.platform == "darwin":
                    script = (
                        'tell application "Terminal"\n'
                        "  activate\n"
                        f'  do script {json.dumps(command)}\n'
                        "end tell"
                    )
                    subprocess.Popen(
                        ["osascript", "-e", script],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    terminal_launched = True
                else:
                    for term in ("x-terminal-emulator", "gnome-terminal", "xterm"):
                        try:
                            subprocess.Popen(
                                [term, "-e", "bash", "-lc",
                                 command + "; exec bash"],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True,
                            )
                            terminal_launched = True
                            break
                        except FileNotFoundError:
                            continue
            except Exception as error:  # noqa: BLE001
                error_note = ((error_note + "; ") if error_note else "") + (
                    type(error).__name__ + ": " + str(error)[:100])

        ok = terminal_launched or browser_launched
        self._send_json(200 if ok else 500, {
            "ok": ok,
            "kind": plan.get("kind"),
            "command": command,
            "browser_url": browser_url,
            "browser_launched": browser_launched,
            "terminal_launched": terminal_launched,
            "account": name,
            "provider": provider,
            "home": home or plan.get("home"),
            "prepared": prepared,
            "instructions": plan.get("instructions"),
            "open_manage": plan.get("open_manage"),
            "error": error_note,
            "hint": (
                ("Paste this in a terminal if it did not open:\n" + command)
                if command else
                ("Open this URL if the browser did not open:\n" + (browser_url or ""))
            ),
        })

    def _snapshot_result(self):
        if self.demo:
            snapshot = paths.load_json(os.path.join(self.directory, "usage.json"))
            return RefreshResult(snapshot)
        return self.refresh_gate.get(
            lambda: paths.load_json(paths.public_snapshot_path()),
            lambda: collector.run_collect(quiet=True))

    def _send_body(self, status, content_type, body):
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_feed(self, route):
        result = self._snapshot_result()
        if not isinstance(result.snapshot, dict):
            if route == "/widget.txt":
                body = widget.render_swiftbar(
                    None, dashboard_href=self._dashboard_href()).encode("utf-8")
                content_type = "text/plain; charset=utf-8"
            else:
                body = b'{"error":"no usage snapshot yet"}'
                content_type = "application/json"
            self._send_body(503, content_type, body)
            return
        # A failed refresh ATTEMPT must not invalidate a snapshot that is
        # still inside the widget freshness window: age-based demotion
        # (the projection's freshness state) already handles genuinely old
        # data, and forcing noncurrent here flashed the whole fleet to
        # "held, never promoted to live" whenever an inline refresh raced
        # another collector holding the collect lock (2026-07-14).
        stale_failed = result.refresh_failed \
            and not _within_freshness_window(result.snapshot)
        reason = result.reason if stale_failed else None
        try:
            if route == "/usage.json":
                value = display_snapshot(
                    result.snapshot, force_noncurrent_reason=reason)
                if stale_failed:
                    value["refresh_failed"] = True
                if result.refresh_failed:
                    # non-demoting diagnostic: a failing collector should be
                    # VISIBLE (warning) long before the freshness window
                    # finally demotes the data
                    value["refresh_attempt_failed"] = True
                body = json.dumps(value, allow_nan=False,
                                  separators=(",", ":")).encode("utf-8")
                content_type = "application/json"
            elif route == "/widget.json":
                value = widget.project(result.snapshot,
                                       force_noncurrent_reason=reason)
                body = json.dumps(value, allow_nan=False,
                                  separators=(",", ":")).encode("utf-8")
                content_type = "application/json"
            else:
                body = widget.render_swiftbar(
                    result.snapshot, force_noncurrent_reason=reason,
                    dashboard_href=self._dashboard_href()).encode("utf-8")
                content_type = "text/plain; charset=utf-8"
        except (TypeError, ValueError, OverflowError):
            body = (widget.render_swiftbar(
                None, dashboard_href=self._dashboard_href()).encode("utf-8")
                    if route == "/widget.txt"
                    else b'{"error":"invalid usage snapshot"}')
            content_type = ("text/plain; charset=utf-8"
                            if route == "/widget.txt" else "application/json")
            self._send_body(503, content_type, body)
            return
        self._send_body(200, content_type, body)


def serve(open_browser=False, port=None, demo=False):
    if demo:
        out_dir = build_demo()
        port = port or 8377
    else:
        config = registry.load()
        settings = registry.dashboard_settings(config)
        port = settings["port"] if port is None else port
        out_dir = paths.public_dir()
        build(config, out_dir)
    handler_cls = type("HeadroomHandler", (Handler,),
                       {"demo": demo, "refresh_gate": RefreshGate()})
    handler = lambda *args, **kwargs: handler_cls(*args, directory=out_dir, **kwargs)  # noqa: E731
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as error:
        print(f"headroom: cannot bind port {port} ({error}). "
              f"Is `headroom serve` already running? Try --port <N>.",
              file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{port}/"
    print(f"headroom dashboard: {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
