"""Local account management used by the dashboard UI (and CLI helpers).

All mutating operations are fail-closed and never return secret material
(API keys, OAuth tokens). Demo mode is read-only.
"""
import os

from . import connect, costs, paths, registry  # paths used for multi-account homes_root


class ManageError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _empty_config():
    return {
        "schema_version": 1,
        "dashboard": dict(registry.DEFAULT_DASHBOARD),
        "accounts": [],
        "routing": {"reserve_percent": 0, "auto_handoff": True},
    }


def load_or_empty():
    path = paths.config_path()
    if not os.path.exists(path):
        return _empty_config()
    config = paths.load_json(path)
    if config is None:
        raise ManageError("config exists but is unreadable/corrupt", 500)
    if not isinstance(config, dict):
        raise ManageError("config is not an object", 500)
    config.setdefault("schema_version", 1)
    config.setdefault("dashboard", dict(registry.DEFAULT_DASHBOARD))
    config.setdefault("accounts", [])
    return config


def save_config(config):
    """Save only when the registry will accept the result."""
    if not config.get("accounts"):
        raise ManageError("at least one account is required", 400)
    registry.save(config)


def public_account(entry):
    """Sanitize a config account for the UI (no secrets)."""
    monthly = entry.get("monthly_cost_usd")
    if monthly is None:
        monthly = costs.default_monthly_cost(
            entry.get("provider"), entry.get("plan"))
    annual = costs.annual_cost(monthly) if monthly is not None else None
    return {
        "name": entry.get("name"),
        "provider": entry.get("provider"),
        "home": entry.get("home"),
        "expected_email": entry.get("expected_email"),
        "monthly_cost_usd": monthly,
        "annual_cost_usd": annual,
        "reserved": bool(entry.get("reserved")),
        "shared_desktop": bool(entry.get("shared_desktop")),
        "handoff_group": entry.get("handoff_group"),
        "renews_on": entry.get("renews_on"),
        "renew_amount": entry.get("renew_amount"),
    }


def list_state():
    config = load_or_empty()
    settings = dict(registry.DEFAULT_DASHBOARD)
    settings.update(config.get("dashboard") or {})
    # Always expose a complete provider_order for the dashboard UI.
    try:
        settings["provider_order"] = _normalize_provider_order(
            settings.get("provider_order") or list(registry.PROVIDERS))
    except ManageError:
        settings["provider_order"] = list(registry.PROVIDERS)
    accounts = [public_account(a) for a in config.get("accounts") or []
                if isinstance(a, dict)]
    connected_homes = {
        os.path.realpath(os.path.expanduser(a["home"]))
        for a in accounts if a.get("home")
    }
    connected_fps = set()
    for a in config.get("accounts") or []:
        if not isinstance(a, dict):
            continue
        try:
            identity = connect.slot_identity(a.get("provider"), a.get("home"))
            if identity and identity.get("account_fingerprint"):
                connected_fps.add(
                    (a.get("provider"), identity["account_fingerprint"]))
        except Exception:  # noqa: BLE001
            pass
    detected = []
    try:
        for item in connect.detect_existing():
            home = os.path.realpath(item["home"])
            fp = item.get("fingerprint")
            already = home in connected_homes or (
                fp and (item["provider"], fp) in connected_fps)
            detected.append({
                "provider": item["provider"],
                "home": item["home"],
                "email": item.get("email"),
                "source": item.get("source") or "default",
                "slot_hint": item.get("slot_hint"),
                "already_connected": already,
            })
    except Exception:  # noqa: BLE001 — discovery is best-effort for the UI
        detected = []
    defaults = {p: os.path.expanduser(h)
                for p, h in connect.DEFAULT_HOMES.items()}
    suggestions = {
        p: connect.next_slot_name(config, p)
        for p in ("claude", "codex", "grok", "manus", "nvidia")
    }
    by_provider = {}
    for a in accounts:
        by_provider.setdefault(a["provider"], []).append(a["name"])
    return {
        "accounts": accounts,
        "by_provider": by_provider,
        "dashboard": settings,
        "detected": detected,
        "providers": list(registry.PROVIDERS),
        "default_homes": defaults,
        "suggested_names": suggestions,
        "multi_account": {
            "supported": ["claude", "codex", "grok", "manus", "nvidia"],
            "homes_root": paths.homes_dir(),
            "note": (
                "Same as original headroom: each extra login gets an isolated "
                "home under ~/.headroom/homes/<slot> with its own "
                "CLAUDE_CONFIG_DIR / CODEX_HOME / GROK_HOME."
            ),
        },
        "cost_hints": {
            "claude": {"Pro": 20, "Max 5x": 100, "Max 20x": 200},
            "codex": {"ChatGPT Plus": 20, "ChatGPT Pro": 200},
            "grok": {"SuperGrok": 30, "SuperGrok Heavy": 300},
            "manus": {"Starter": 19, "Pro": 39, "Team": 199},
            "nvidia": {"Free": 0},
        },
    }


def _require_name(name):
    if not isinstance(name, str) or not registry.NAME_RE.fullmatch(name):
        raise ManageError(
            "slot name must be lowercase letters/digits/_/- (max 32)", 400)
    return name


def _parse_cost(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ManageError("monthly_cost_usd must be a number", 400)
    try:
        cost = float(value)
    except (TypeError, ValueError) as error:
        raise ManageError("monthly_cost_usd must be a number", 400) from error
    if cost < 0 or cost != cost:
        raise ManageError("monthly_cost_usd must be non-negative", 400)
    return cost


def adopt_account(name, provider, home=None, monthly_cost_usd=None,
                  expected_email=None):
    name = _require_name(name)
    if provider not in registry.PROVIDERS:
        raise ManageError(f"unknown provider {provider!r}", 400)
    if provider == "manus" and not home:
        # Manus without a home uses API-key connect instead
        raise ManageError("use manus API-key connect for new Manus slots", 400)
    home = os.path.expanduser(
        home or connect.DEFAULT_HOMES.get(provider, "~/.headroom/homes/" + name))
    config = load_or_empty()
    if any(a.get("name") == name for a in config.get("accounts") or []):
        raise ManageError(f"account {name!r} already exists", 409)
    # connect_adopt mutates config via registry; work against live config
    # Quiet path: call lower-level pieces for clean errors
    identity = connect.slot_identity(provider, home)
    if not identity or not identity.get("email"):
        raise ManageError(
            f"no {provider} login found at {home}. Log in with the provider "
            f"CLI first, then adopt.", 400)
    duplicates = connect.existing_fingerprints(config, provider)
    fingerprint = identity.get("account_fingerprint")
    if fingerprint and fingerprint in duplicates:
        raise ManageError(
            f"that login is already connected as "
            f"'{duplicates[fingerprint]}'", 409)
    email = expected_email or identity.get("email")
    cost = _parse_cost(monthly_cost_usd)
    if cost is None:
        cost = costs.default_monthly_cost(provider, identity.get("plan_type"))
    entry = connect.add_account(config, name, provider, home, email,
                                monthly_cost_usd=cost)
    return public_account(entry)


def prepare_fresh(name, provider, monthly_cost_usd=None):
    """Prepare an isolated multi-account home (Claude/Codex/Grok).

    Same model as original headroom: second logins use
    ``~/.headroom/homes/<name>`` + provider env var, never overwriting the
    default ~/.claude / ~/.codex / ~/.grok home.
    """
    name = _require_name(name)
    provider = (provider or "").lower()
    if provider not in ("claude", "codex", "grok"):
        raise ManageError(
            "fresh login slots are for claude, codex, and grok "
            "(manus/nvidia use API keys)", 400)
    config = load_or_empty()
    if any(a.get("name") == name for a in config.get("accounts") or []):
        raise ManageError(f"account {name!r} already exists", 409)
    try:
        prepared = connect.prepare_fresh_home(name, provider)
    except ValueError as error:
        raise ManageError(str(error), 400) from error
    cost = _parse_cost(monthly_cost_usd)
    prepared["suggested_cost"] = cost
    prepared["ready"] = False
    # If they already logged in before clicking prepare, surface that
    identity = connect.slot_identity(provider, prepared["home"])
    if identity and identity.get("email"):
        prepared["ready"] = True
        prepared["email"] = identity["email"]
    return prepared


def finish_fresh(name, provider, monthly_cost_usd=None, expected_email=None):
    """After the user completes CLI login into an isolated home, register it."""
    name = _require_name(name)
    provider = (provider or "").lower()
    try:
        home = connect.isolated_home_path(name)
    except ValueError as error:
        raise ManageError(str(error), 400) from error
    return adopt_account(
        name, provider, home=home,
        monthly_cost_usd=monthly_cost_usd,
        expected_email=expected_email)


def suggest_name(provider):
    config = load_or_empty()
    return connect.next_slot_name(config, (provider or "claude").lower())


def add_manus(name, api_key, email=None, monthly_cost_usd=None):
    name = _require_name(name)
    if not api_key or not str(api_key).strip():
        raise ManageError("Manus API key is required", 400)
    config = load_or_empty()
    if any(a.get("name") == name for a in config.get("accounts") or []):
        raise ManageError(f"account {name!r} already exists", 409)
    cost = _parse_cost(monthly_cost_usd)
    if cost is None:
        cost = costs.default_monthly_cost("manus", "pro")
    entry = connect.connect_manus_key(
        config, name, api_key.strip(), email=email or None,
        monthly_cost_usd=cost, quiet=True)
    if entry is None:
        raise ManageError(
            "could not connect Manus key (invalid, duplicate, or write failed)",
            400)
    return public_account(entry)


def add_nvidia(name, api_key=None, email=None, monthly_cost_usd=0,
               use_insights_key=False):
    name = _require_name(name)
    if not use_insights_key and (not api_key or not str(api_key).strip()):
        raise ManageError(
            "NVIDIA API key required (or set use_insights_key true)", 400)
    config = load_or_empty()
    if any(a.get("name") == name for a in config.get("accounts") or []):
        raise ManageError(f"account {name!r} already exists", 409)
    cost = _parse_cost(monthly_cost_usd)
    if cost is None:
        cost = 0.0
    entry = connect.connect_nvidia_key(
        config, name,
        api_key=(api_key.strip() if api_key else None),
        email=email or None,
        monthly_cost_usd=cost,
        use_insights_key=bool(use_insights_key),
        quiet=True)
    if entry is None:
        raise ManageError(
            "could not connect NVIDIA key (invalid, duplicate, or write failed)",
            400)
    return public_account(entry)


def _parse_renews_on(value):
    """Accept YYYY-MM-DD or empty; store as YYYY-MM-DD."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ManageError("renews_on must be a date string (YYYY-MM-DD)", 400)
    text = value.strip()
    if not text:
        return None
    # Allow full ISO datetime — keep calendar date only
    if "T" in text:
        text = text.split("T", 1)[0]
    parts = text.split("-")
    if len(parts) != 3:
        raise ManageError("renews_on must be YYYY-MM-DD", 400)
    try:
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        if not (1 <= month <= 12 and 1 <= day <= 31 and year >= 2000):
            raise ValueError("out of range")
    except ValueError as error:
        raise ManageError("renews_on must be a valid YYYY-MM-DD date", 400) from error
    return f"{year:04d}-{month:02d}-{day:02d}"


def _parse_renew_amount(value):
    """Credits/units the allotment renews to (e.g. 4000 monthly)."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ManageError("renew_amount must be a number", 400)
    try:
        amount = float(value)
    except (TypeError, ValueError) as error:
        raise ManageError("renew_amount must be a number", 400) from error
    if amount < 0 or amount != amount:
        raise ManageError("renew_amount must be non-negative", 400)
    # Prefer ints when whole
    if amount == int(amount):
        return int(amount)
    return amount


def update_account(name, **fields):
    name = _require_name(name)
    allowed = {"monthly_cost_usd", "expected_email", "reserved",
               "shared_desktop", "handoff_group", "renews_on", "renew_amount"}
    unknown = set(fields) - allowed
    if unknown:
        raise ManageError(f"unsupported fields: {sorted(unknown)}", 400)

    updated = []

    def _mutate(cfg):
        match = next((a for a in cfg["accounts"] if a.get("name") == name), None)
        if match is None:
            raise ManageError(f"no account named {name!r}", 404)
        if "monthly_cost_usd" in fields:
            cost = _parse_cost(fields["monthly_cost_usd"])
            if cost is None:
                match.pop("monthly_cost_usd", None)
            else:
                match["monthly_cost_usd"] = cost
        if "expected_email" in fields:
            email = fields["expected_email"]
            if email in (None, ""):
                match.pop("expected_email", None)
            elif isinstance(email, str):
                match["expected_email"] = email.strip()
            else:
                raise ManageError("expected_email must be a string", 400)
        if "reserved" in fields:
            match["reserved"] = bool(fields["reserved"])
        if "shared_desktop" in fields:
            match["shared_desktop"] = bool(fields["shared_desktop"])
        if "handoff_group" in fields:
            group = fields["handoff_group"]
            if group in (None, ""):
                match.pop("handoff_group", None)
            elif isinstance(group, str) and group.strip():
                match["handoff_group"] = group.strip()
            else:
                raise ManageError("handoff_group must be a non-empty string", 400)
        if "renews_on" in fields:
            date = _parse_renews_on(fields["renews_on"])
            if date is None:
                match.pop("renews_on", None)
            else:
                match["renews_on"] = date
        if "renew_amount" in fields:
            amount = _parse_renew_amount(fields["renew_amount"])
            if amount is None:
                match.pop("renew_amount", None)
            else:
                match["renew_amount"] = amount
        updated.append(public_account(match))

    try:
        registry.mutate(_mutate)
    except registry.RegistryError as error:
        # no config yet or corrupt
        config = load_or_empty()
        if not any(a.get("name") == name for a in config.get("accounts") or []):
            raise ManageError(f"no account named {name!r}", 404) from error
        _mutate(config)
        save_config(config)
    return updated[0]


def remove_account(name):
    name = _require_name(name)
    try:
        removed = registry.remove_account(name)
    except registry.RegistryError as error:
        msg = str(error)
        if "final" in msg:
            raise ManageError(
                "cannot remove the last account — add another first", 400
            ) from error
        raise ManageError(msg, 404 if "no connected" in msg else 400) from error
    return {"removed": removed.get("name"), "home": removed.get("home")}


def reorder_accounts(order):
    """Rewrite account preference order (routing + dashboard display).

    ``order`` is a list of account names — must be a permutation of the
    currently registered accounts (no extras, no missing).
    """
    if not isinstance(order, (list, tuple)) or not order:
        raise ManageError("order must be a non-empty list of account names", 400)
    names = []
    for item in order:
        if not isinstance(item, str) or not item.strip():
            raise ManageError("order entries must be account names", 400)
        names.append(item.strip())
    if len(set(names)) != len(names):
        raise ManageError("order has duplicate account names", 400)

    result = []

    def _mutate(cfg):
        accounts = list(cfg.get("accounts") or [])
        by_name = {}
        for entry in accounts:
            if isinstance(entry, dict) and entry.get("name"):
                by_name[entry["name"]] = entry
        current = set(by_name)
        wanted = set(names)
        if wanted != current:
            missing = sorted(current - wanted)
            extra = sorted(wanted - current)
            detail = []
            if missing:
                detail.append("missing: " + ", ".join(missing))
            if extra:
                detail.append("unknown: " + ", ".join(extra))
            raise ManageError(
                "order must list every account exactly once ("
                + "; ".join(detail) + ")", 400)
        cfg["accounts"] = [by_name[n] for n in names]
        result.append([public_account(a) for a in cfg["accounts"]])

    try:
        registry.mutate(_mutate)
    except registry.RegistryError as error:
        config = load_or_empty()
        if not config.get("accounts"):
            raise ManageError("no accounts configured", 404) from error
        _mutate(config)
        save_config(config)
    return {"order": [a["name"] for a in result[0]], "accounts": result[0]}


def move_account(name, direction, scope="provider"):
    """Move one account up/down in preference order.

    scope="provider" (default): swap with previous/next account of the same
    provider. scope="all": swap with the previous/next account overall.
    """
    name = _require_name(name)
    direction = (direction or "").lower().strip()
    if direction not in ("up", "down", "top", "bottom"):
        raise ManageError("direction must be up, down, top, or bottom", 400)
    scope = (scope or "provider").lower().strip()
    if scope not in ("provider", "all"):
        raise ManageError("scope must be provider or all", 400)

    result = []

    def _mutate(cfg):
        accounts = list(cfg.get("accounts") or [])
        index = next((i for i, a in enumerate(accounts)
                      if isinstance(a, dict) and a.get("name") == name), None)
        if index is None:
            raise ManageError(f"no account named {name!r}", 404)
        if direction == "top":
            entry = accounts.pop(index)
            if scope == "provider":
                provider = entry.get("provider")
                insert_at = next(
                    (i for i, a in enumerate(accounts)
                     if isinstance(a, dict) and a.get("provider") == provider),
                    0)
                accounts.insert(insert_at, entry)
            else:
                accounts.insert(0, entry)
        elif direction == "bottom":
            entry = accounts.pop(index)
            if scope == "provider":
                provider = entry.get("provider")
                # find last same-provider after pop
                insert_at = len(accounts)
                for i in range(len(accounts) - 1, -1, -1):
                    if (isinstance(accounts[i], dict)
                            and accounts[i].get("provider") == provider):
                        insert_at = i + 1
                        break
                accounts.insert(insert_at, entry)
            else:
                accounts.append(entry)
        else:
            provider = accounts[index].get("provider")
            if scope == "provider":
                peers = [i for i, a in enumerate(accounts)
                         if isinstance(a, dict) and a.get("provider") == provider]
                pos = peers.index(index)
                if direction == "up":
                    if pos == 0:
                        result.append([public_account(a) for a in accounts
                                       if isinstance(a, dict)])
                        return
                    swap_with = peers[pos - 1]
                else:
                    if pos == len(peers) - 1:
                        result.append([public_account(a) for a in accounts
                                       if isinstance(a, dict)])
                        return
                    swap_with = peers[pos + 1]
            else:
                if direction == "up":
                    if index == 0:
                        result.append([public_account(a) for a in accounts
                                       if isinstance(a, dict)])
                        return
                    swap_with = index - 1
                else:
                    if index == len(accounts) - 1:
                        result.append([public_account(a) for a in accounts
                                       if isinstance(a, dict)])
                        return
                    swap_with = index + 1
            accounts[index], accounts[swap_with] = accounts[swap_with], accounts[index]
        cfg["accounts"] = accounts
        result.append([public_account(a) for a in accounts if isinstance(a, dict)])

    try:
        registry.mutate(_mutate)
    except registry.RegistryError as error:
        config = load_or_empty()
        if not any(a.get("name") == name for a in config.get("accounts") or []):
            raise ManageError(f"no account named {name!r}", 404) from error
        _mutate(config)
        save_config(config)
    return {"order": [a["name"] for a in result[0]], "accounts": result[0]}


def _normalize_provider_order(order):
    """Return a full provider list with known providers first in the given order."""
    if not isinstance(order, (list, tuple)):
        raise ManageError("provider_order must be a list", 400)
    seen = []
    for item in order:
        if not isinstance(item, str):
            raise ManageError("provider_order entries must be strings", 400)
        name = item.strip().lower()
        if name not in registry.PROVIDERS:
            raise ManageError(
                f"unknown provider {item!r} — use one of "
                f"{', '.join(registry.PROVIDERS)}", 400)
        if name not in seen:
            seen.append(name)
    for name in registry.PROVIDERS:
        if name not in seen:
            seen.append(name)
    return seen


def reorder_providers(order):
    """Set dashboard provider section order (Claude / Codex / Grok / …)."""
    normalized = _normalize_provider_order(order)
    return update_dashboard(provider_order=normalized)


def move_provider(provider, direction):
    """Move one provider section up/down in the dashboard order."""
    provider = (provider or "").lower().strip()
    if provider not in registry.PROVIDERS:
        raise ManageError(f"unknown provider {provider!r}", 400)
    direction = (direction or "").lower().strip()
    if direction not in ("up", "down", "top", "bottom"):
        raise ManageError("direction must be up, down, top, or bottom", 400)

    settings = list_state()["dashboard"]
    order = _normalize_provider_order(
        settings.get("provider_order") or list(registry.PROVIDERS))
    try:
        index = order.index(provider)
    except ValueError as error:
        raise ManageError(f"unknown provider {provider!r}", 400) from error

    if direction == "top":
        order.insert(0, order.pop(index))
    elif direction == "bottom":
        order.append(order.pop(index))
    elif direction == "up":
        if index > 0:
            order[index], order[index - 1] = order[index - 1], order[index]
    else:  # down
        if index < len(order) - 1:
            order[index], order[index + 1] = order[index + 1], order[index]
    return reorder_providers(order)


def update_dashboard(**fields):
    allowed = {"theme", "title", "redact_emails", "port", "provider_order"}
    unknown = set(fields) - allowed
    if unknown:
        raise ManageError(f"unsupported fields: {sorted(unknown)}", 400)

    def _mutate(cfg):
        dash = cfg.setdefault("dashboard", dict(registry.DEFAULT_DASHBOARD))
        if "theme" in fields:
            theme = fields["theme"]
            if theme not in ("midnight", "minimal", "chrome", "paper", "terminal"):
                raise ManageError("unknown theme", 400)
            dash["theme"] = theme
        if "title" in fields:
            title = fields["title"]
            if not isinstance(title, str) or not title.strip():
                raise ManageError("title must be a non-empty string", 400)
            dash["title"] = title.strip()[:80]
        if "redact_emails" in fields:
            dash["redact_emails"] = bool(fields["redact_emails"])
        if "port" in fields:
            try:
                port = int(fields["port"])
            except (TypeError, ValueError) as error:
                raise ManageError("port must be an integer", 400) from error
            if not 1 <= port <= 65535:
                raise ManageError("port out of range", 400)
            dash["port"] = port
        if "provider_order" in fields:
            dash["provider_order"] = _normalize_provider_order(
                fields["provider_order"])

    try:
        registry.mutate(_mutate)
    except registry.RegistryError as error:
        raise ManageError(
            "no config yet — add an account first", 400) from error
    return list_state()["dashboard"]
