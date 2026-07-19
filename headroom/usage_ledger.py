"""Full usage picture + wasted-allotment tracking.

For every account window we track:

* what you HAVE (limit / allotment)
* what you've USED
* what you have LEFT
* countdown to reset
* WASTE: remaining capacity that disappeared when a window reset
  (use-it-or-lose-it loss), accumulated since tracking started

State file: ``~/.headroom/state/usage-ledger.json`` (private).
"""
import json
import math
import os
import time
from datetime import datetime, timezone

from . import paths

LEDGER_VERSION = 1


def ledger_path():
    return os.path.join(paths.state_dir(), "usage-ledger.json")


def _finite(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _empty_ledger():
    return {
        "schema_version": LEDGER_VERSION,
        "tracking_started": int(time.time()),
        "tracking_started_iso": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"),
        "accounts": {},   # name -> per-window last observation + waste totals
        "fleet": {
            "lifetime_wasted_percent_points": 0.0,
            "lifetime_wasted_units": 0.0,
            "lifetime_used_units": 0.0,
            "reset_events": 0,
        },
    }


def load_ledger():
    raw = paths.load_json(ledger_path())
    if not isinstance(raw, dict):
        return _empty_ledger()
    base = _empty_ledger()
    base.update({k: raw[k] for k in base if k in raw and k != "accounts"
                 and k != "fleet"})
    if isinstance(raw.get("tracking_started"), (int, float)):
        base["tracking_started"] = int(raw["tracking_started"])
    if isinstance(raw.get("tracking_started_iso"), str):
        base["tracking_started_iso"] = raw["tracking_started_iso"]
    if isinstance(raw.get("accounts"), dict):
        base["accounts"] = raw["accounts"]
    if isinstance(raw.get("fleet"), dict):
        base["fleet"].update(raw["fleet"])
    return base


def save_ledger(document):
    paths.ensure_private(paths.state_dir())
    paths.write_json_atomic(ledger_path(), document, mode=0o600)


def enrich_window(window, now=None):
    """Add remaining %, units, and human countdown onto a window dict."""
    if not isinstance(window, dict):
        return None
    now = int(time.time() if now is None else now)
    out = dict(window)
    used = out.get("used_percent")
    if _finite(used) and 0 <= used <= 100:
        out["used_percent"] = round(float(used), 1)
        out["remaining_percent"] = round(100.0 - float(used), 1)
    else:
        out["remaining_percent"] = None

    used_u = out.get("used_units")
    limit_u = out.get("limit_units")
    rem_u = out.get("remaining_units")
    if _finite(limit_u) and limit_u > 0:
        out["limit_units"] = float(limit_u)
        if _finite(used_u):
            out["used_units"] = float(used_u)
            if not _finite(rem_u):
                out["remaining_units"] = max(0.0, float(limit_u) - float(used_u))
        elif _finite(out.get("used_percent")):
            out["used_units"] = round(float(limit_u) * float(used) / 100.0, 2)
            out["remaining_units"] = round(
                float(limit_u) - out["used_units"], 2)
    if _finite(rem_u) and not _finite(out.get("remaining_units")):
        out["remaining_units"] = float(rem_u)

    resets = out.get("resets_at")
    if _finite(resets):
        resets = int(resets)
        out["resets_at"] = resets
        left = resets - now
        out["seconds_to_reset"] = left
        out["countdown"] = _countdown(left)
        out["resets_iso"] = datetime.fromtimestamp(
            resets, timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        out["seconds_to_reset"] = None
        out["countdown"] = None
        out["resets_iso"] = None
    return out


def _countdown(seconds_left):
    if seconds_left is None:
        return None
    if seconds_left <= 0:
        return "reset due"
    days, rem = divmod(int(seconds_left), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _window_snapshot(window, now):
    enriched = enrich_window(window, now=now) or {}
    return {
        "used_percent": enriched.get("used_percent"),
        "remaining_percent": enriched.get("remaining_percent"),
        "used_units": enriched.get("used_units"),
        "limit_units": enriched.get("limit_units"),
        "remaining_units": enriched.get("remaining_units"),
        "resets_at": enriched.get("resets_at"),
        "observed_at": enriched.get("observed_at") or now,
    }


def _detect_waste(prev, curr):
    """Return (wasted_percent_points, wasted_units, is_reset) comparing prev→curr.

    A reset is when remaining jumps up (used dropped) or resets_at moves later
    while remaining increases. Waste = remaining capacity that vanished.
    """
    if not isinstance(prev, dict) or not isinstance(curr, dict):
        return 0.0, 0.0, False
    prev_used = prev.get("used_percent")
    curr_used = curr.get("used_percent")
    prev_left = prev.get("remaining_percent")
    curr_left = curr.get("remaining_percent")
    prev_reset = prev.get("resets_at")
    curr_reset = curr.get("resets_at")
    prev_rem_u = prev.get("remaining_units")
    curr_rem_u = curr.get("remaining_units")
    prev_lim = prev.get("limit_units")

    is_reset = False
    if _finite(prev_used) and _finite(curr_used):
        # Sharp drop in used% ⇒ new window (allotment refreshed)
        if prev_used - curr_used >= 12.0:
            is_reset = True
    if _finite(prev_reset) and _finite(curr_reset) and curr_reset > prev_reset + 300:
        if _finite(prev_left) and _finite(curr_left) and curr_left > prev_left + 5:
            is_reset = True
    if _finite(prev_rem_u) and _finite(curr_rem_u) and curr_rem_u > prev_rem_u * 1.2 + 1:
        if _finite(prev_used) and _finite(curr_used) and curr_used < prev_used:
            is_reset = True

    if not is_reset:
        return 0.0, 0.0, False

    # Waste = what was left unused when the window rolled
    waste_pct = float(prev_left) if _finite(prev_left) else 0.0
    waste_units = 0.0
    if _finite(prev_rem_u):
        waste_units = float(prev_rem_u)
    elif _finite(prev_lim) and _finite(prev_left):
        waste_units = float(prev_lim) * float(prev_left) / 100.0
    return max(0.0, waste_pct), max(0.0, waste_units), True


def update_from_snapshot(snapshot):
    """Enrich snapshot accounts in-place-ish and update waste ledger.

    Returns (enriched_accounts_public_fields, fleet_usage_summary).
    """
    now = int(time.time())
    if not isinstance(snapshot, dict):
        return [], _empty_summary(now)
    ledger = load_ledger()
    fleet = ledger.setdefault("fleet", _empty_ledger()["fleet"])
    accounts_out = []

    for account in snapshot.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        name = account.get("name")
        provider = account.get("provider")
        if not name:
            continue
        entry = ledger["accounts"].setdefault(name, {
            "provider": provider,
            "windows": {},
            "lifetime_wasted_percent_points": 0.0,
            "lifetime_wasted_units": 0.0,
            "lifetime_peak_used_percent": 0.0,
            "lifetime_used_units_seen": 0.0,
            "reset_events": 0,
            "first_seen": now,
            "last_seen": now,
        })
        entry["provider"] = provider
        entry["last_seen"] = now
        if not entry.get("first_seen"):
            entry["first_seen"] = now

        windows_out = {}
        for key, window in (account.get("windows") or {}).items():
            if not isinstance(window, dict):
                continue
            curr = _window_snapshot(window, now)
            prev = entry["windows"].get(key)
            waste_pct, waste_units, is_reset = _detect_waste(prev, curr)
            if is_reset:
                entry["lifetime_wasted_percent_points"] = round(
                    float(entry.get("lifetime_wasted_percent_points") or 0)
                    + waste_pct, 2)
                entry["lifetime_wasted_units"] = round(
                    float(entry.get("lifetime_wasted_units") or 0)
                    + waste_units, 2)
                entry["reset_events"] = int(entry.get("reset_events") or 0) + 1
                fleet["lifetime_wasted_percent_points"] = round(
                    float(fleet.get("lifetime_wasted_percent_points") or 0)
                    + waste_pct, 2)
                fleet["lifetime_wasted_units"] = round(
                    float(fleet.get("lifetime_wasted_units") or 0)
                    + waste_units, 2)
                fleet["reset_events"] = int(fleet.get("reset_events") or 0) + 1

            used = curr.get("used_percent")
            if _finite(used):
                entry["lifetime_peak_used_percent"] = max(
                    float(entry.get("lifetime_peak_used_percent") or 0),
                    float(used))
            used_u = curr.get("used_units")
            if _finite(used_u):
                # Track highest cumulative used_units observed (best-effort)
                prev_u = (prev or {}).get("used_units")
                if _finite(prev_u) and used_u > prev_u:
                    delta = float(used_u) - float(prev_u)
                    entry["lifetime_used_units_seen"] = round(
                        float(entry.get("lifetime_used_units_seen") or 0) + delta, 2)
                    fleet["lifetime_used_units"] = round(
                        float(fleet.get("lifetime_used_units") or 0) + delta, 2)
                elif not _finite(prev_u):
                    entry["lifetime_used_units_seen"] = round(
                        max(float(entry.get("lifetime_used_units_seen") or 0),
                            float(used_u)), 2)

            entry["windows"][key] = curr
            enriched = enrich_window(window, now=now)
            if is_reset and waste_pct > 0:
                enriched["last_waste_percent"] = round(waste_pct, 1)
                enriched["last_waste_units"] = round(waste_units, 2)
            windows_out[key] = enriched

        usage = {
            "have": {},
            "used": {},
            "left": {},
            "countdowns": {},
            "lifetime_wasted_percent_points": entry.get(
                "lifetime_wasted_percent_points", 0.0),
            "lifetime_wasted_units": entry.get("lifetime_wasted_units", 0.0),
            "lifetime_used_units_seen": entry.get("lifetime_used_units_seen", 0.0),
            "lifetime_peak_used_percent": entry.get(
                "lifetime_peak_used_percent", 0.0),
            "reset_events": entry.get("reset_events", 0),
            "tracking_since": ledger.get("tracking_started"),
            "tracking_since_iso": ledger.get("tracking_started_iso"),
        }
        for key, w in windows_out.items():
            if not w:
                continue
            usage["have"][key] = {
                "limit_units": w.get("limit_units"),
                "allotment_percent": 100.0,
            }
            usage["used"][key] = {
                "used_percent": w.get("used_percent"),
                "used_units": w.get("used_units"),
            }
            usage["left"][key] = {
                "remaining_percent": w.get("remaining_percent"),
                "remaining_units": w.get("remaining_units"),
            }
            usage["countdowns"][key] = {
                "resets_at": w.get("resets_at"),
                "seconds_to_reset": w.get("seconds_to_reset"),
                "countdown": w.get("countdown"),
            }

        accounts_out.append({
            "name": name,
            "provider": provider,
            "email": account.get("email"),
            "plan": account.get("plan"),
            "ok": account.get("ok"),
            "windows": windows_out,
            "usage": usage,
            "monthly_cost_usd": account.get("monthly_cost_usd"),
            "annual_cost_usd": account.get("annual_cost_usd"),
        })

    try:
        save_ledger(ledger)
    except OSError:
        pass

    summary = {
        "tracking_started": ledger.get("tracking_started"),
        "tracking_started_iso": ledger.get("tracking_started_iso"),
        "tracking_age_seconds": now - int(ledger.get("tracking_started") or now),
        "fleet_lifetime_wasted_percent_points": fleet.get(
            "lifetime_wasted_percent_points", 0.0),
        "fleet_lifetime_wasted_units": fleet.get("lifetime_wasted_units", 0.0),
        "fleet_lifetime_used_units": fleet.get("lifetime_used_units", 0.0),
        "fleet_reset_events": fleet.get("reset_events", 0),
        "accounts": accounts_out,
        "generated": now,
    }
    return accounts_out, summary


def _empty_summary(now):
    return {
        "tracking_started": now,
        "tracking_started_iso": datetime.fromtimestamp(
            now, timezone.utc).isoformat().replace("+00:00", "Z"),
        "tracking_age_seconds": 0,
        "fleet_lifetime_wasted_percent_points": 0.0,
        "fleet_lifetime_wasted_units": 0.0,
        "fleet_lifetime_used_units": 0.0,
        "fleet_reset_events": 0,
        "accounts": [],
        "generated": now,
    }


def summary():
    """Read-only fleet usage summary from ledger + current public snapshot."""
    now = int(time.time())
    snapshot = paths.load_json(paths.public_snapshot_path())
    if not snapshot:
        snapshot = paths.load_json(paths.private_snapshot_path()) or {}
    # Re-run update so windows are enriched (also persists waste if new)
    _, fleet = update_from_snapshot(snapshot)
    return fleet
