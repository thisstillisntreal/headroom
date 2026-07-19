"""Local NVIDIA API usage ledger + soft free-tier windows.

NVIDIA Build / integrate.api does not expose a Claude-style subscription
meter. We therefore:

1. identify the account via NGC ``/v2/users/me`` (email + org),
2. record every headroom-originated NVIDIA call (insights, probes) into a
   private JSONL ledger under ``~/.headroom/state/nvidia-usage.jsonl``,
3. project soft 5h / 7d / month windows from rolling request + token totals
   against configurable free-tier caps (defaults match generous free quotas).

The ledger is fail-closed and never stores the API key.
"""
import json
import math
import os
import time
import urllib.error
import urllib.request

from . import paths

NGC_USER_URL = "https://api.ngc.nvidia.com/v2/users/me"
DEFAULT_CAPS = {
    # Soft caps — operator can override in secrets.json insights.nvidia_caps
    "requests_5h": 200,
    "tokens_7d": 500_000,
    "tokens_month": 2_000_000,
}


def usage_path():
    return os.path.join(paths.state_dir(), "nvidia-usage.jsonl")


def _finite(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def record_call(*, model=None, purpose="api", prompt_tokens=0,
                completion_tokens=0, total_tokens=None, ok=True, now=None):
    """Append one NVIDIA API usage sample (best-effort, never raises)."""
    now = int(time.time() if now is None else now)
    if total_tokens is None:
        total_tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)
    row = {
        "t": now,
        "model": model,
        "purpose": purpose,
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "ok": bool(ok),
    }
    try:
        paths.ensure_private(paths.state_dir())
        path = usage_path()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":"),
                                    allow_nan=False))
            handle.write("\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


def load_calls(since=None, until=None, limit=100000):
    path = usage_path()
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = row.get("t")
                if not _finite(t):
                    continue
                t = int(t)
                if since is not None and t < since:
                    continue
                if until is not None and t > until:
                    continue
                rows.append(row)
                if len(rows) > limit:
                    rows = rows[-limit:]
    except OSError:
        return []
    rows.sort(key=lambda r: r["t"])
    return rows


def rolling_totals(now=None, windows=None):
    """Return request/token totals for 5h, 7d, and ~30d windows."""
    now = int(time.time() if now is None else now)
    spans = windows or {
        "5h": 5 * 3600,
        "7d": 7 * 86400,
        "month": 30 * 86400,
    }
    since = now - max(spans.values())
    calls = load_calls(since=since, until=now + 60)
    out = {}
    for key, seconds in spans.items():
        cut = now - seconds
        subset = [c for c in calls if int(c["t"]) >= cut]
        out[key] = {
            "requests": len(subset),
            "tokens": sum(int(c.get("total_tokens") or 0) for c in subset),
            "prompt_tokens": sum(int(c.get("prompt_tokens") or 0) for c in subset),
            "completion_tokens": sum(int(c.get("completion_tokens") or 0)
                                     for c in subset),
            "ok_requests": sum(1 for c in subset if c.get("ok")),
        }
    return out


def resolve_caps(overrides=None):
    caps = dict(DEFAULT_CAPS)
    if isinstance(overrides, dict):
        for key in DEFAULT_CAPS:
            if key in overrides and _finite(overrides[key]) and overrides[key] > 0:
                caps[key] = int(overrides[key])
    return caps


def windows_from_totals(totals, caps=None, now=None):
    """Map rolling totals + soft caps to headroom window dicts."""
    now = int(time.time() if now is None else now)
    caps = resolve_caps(caps)
    windows = {}

    # 5h: request burst against soft 5h request cap
    req = (totals.get("5h") or {}).get("requests") or 0
    cap5 = max(1, caps["requests_5h"])
    used5 = min(100.0, round(100.0 * req / cap5, 1))
    windows["5h"] = {
        "used_percent": used5,
        "resets_at": now + 5 * 3600,
        "window_minutes": 300,
        "observed_at": now,
        "freshness": "fresh",
        "used_units": float(req),
        "limit_units": float(cap5),
        "remaining_units": float(max(0, cap5 - req)),
    }

    # 7d: tokens against weekly soft cap
    tok7 = (totals.get("7d") or {}).get("tokens") or 0
    cap7 = max(1, caps["tokens_7d"])
    used7 = min(100.0, round(100.0 * tok7 / cap7, 1))
    windows["7d"] = {
        "used_percent": used7,
        "resets_at": now + 7 * 86400,
        "window_minutes": 10080,
        "observed_at": now,
        "freshness": "fresh",
        "used_units": float(tok7),
        "limit_units": float(cap7),
        "remaining_units": float(max(0, cap7 - tok7)),
    }

    # month: tokens against monthly soft cap
    tokm = (totals.get("month") or {}).get("tokens") or 0
    capm = max(1, caps["tokens_month"])
    usedm = min(100.0, round(100.0 * tokm / capm, 1))
    windows["month"] = {
        "used_percent": usedm,
        "resets_at": now + 30 * 86400,
        "window_minutes": 43200,
        "observed_at": now,
        "freshness": "fresh",
        "used_units": float(tokm),
        "limit_units": float(capm),
        "remaining_units": float(max(0, capm - tokm)),
    }
    return windows


def fetch_identity(api_key, timeout=15):
    """Live NGC identity for the API key (email, username, org)."""
    if not api_key:
        raise ValueError("nvidia_auth_missing")
    request = urllib.request.Request(
        NGC_USER_URL,
        headers={
            "authorization": "Bearer " + api_key,
            "accept": "application/json",
            "user-agent": "headroom",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise ValueError("nvidia_usage_token_rejected") from error
        raise
    user = data.get("user") if isinstance(data, dict) else None
    if not isinstance(user, dict):
        raise ValueError("nvidia_identity_unrecognized")
    email = user.get("email")
    name = user.get("name") or user.get("clientId")
    user_id = user.get("id") or user.get("starfleetId") or name
    org = None
    roles = user.get("roles") or data.get("userRoles") or []
    if isinstance(roles, list) and roles:
        first = roles[0] if isinstance(roles[0], dict) else {}
        org_obj = first.get("org") if isinstance(first, dict) else None
        if isinstance(org_obj, dict):
            org = org_obj.get("displayName") or org_obj.get("name")
    if not email and not name:
        raise ValueError("nvidia_identity_email_missing")
    return {
        "verified": True,
        "email": email or (f"nvidia:{user_id}"),
        "username": name,
        "account_id": str(user_id),
        "org": org,
        "method": "ngc_users_me",
        "plan_type": "NVIDIA Free" if not org else f"NVIDIA · {org}",
    }
