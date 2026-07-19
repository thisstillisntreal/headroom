"""Local usage history log + series aggregation for dashboard charts.

Every successful collect appends one compact sample (JSONL under
``~/.headroom/state/history.jsonl``). Charts never call providers — they only
read this local log. Retention defaults to 90 days.
"""
import hashlib
import json
import math
import os
import time

from . import paths, registry

SCHEMA = 1
DEFAULT_RETENTION_DAYS = paths.env_int("HEADROOM_HISTORY_RETENTION_DAYS", 90)
RANGES = {
    "day": 24 * 3600,
    "week": 7 * 24 * 3600,
    "month": 30 * 24 * 3600,
}
# Bucket widths for aggregation (seconds)
BUCKETS = {
    "day": 15 * 60,       # 15 minutes
    "week": 60 * 60,      # 1 hour
    "month": 6 * 3600,    # 6 hours
}

# Provider brand hues (degrees on the HSL wheel)
PROVIDER_HUE = {
    "claude": 28,
    "codex": 152,
    "grok": 268,
    "manus": 192,
    "nvidia": 78,  # NVIDIA green-gold
}


def history_path():
    return os.path.join(paths.state_dir(), "history.jsonl")


def _finite(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def primary_left_percent(account):
    """Single headroom % for charting: min remaining across usable windows."""
    windows = account.get("windows") if isinstance(account, dict) else None
    if not isinstance(windows, dict):
        return None
    lefts = []
    provider = account.get("provider")
    keys = list(windows.keys())
    # Prefer standard order; include month for grok/manus
    preferred = ["5h", "7d", "month"]
    ordered = [k for k in preferred if k in windows] + [
        k for k in keys if k not in preferred and not str(k).startswith("scoped:")
    ]
    for key in ordered:
        window = windows.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent")
        if not _finite(used) or not 0 <= used <= 100:
            continue
        lefts.append(100.0 - float(used))
    if not lefts:
        return None
    return round(min(lefts), 2)


def sample_from_snapshot(snapshot):
    """Build one history row from a private or public snapshot."""
    if not isinstance(snapshot, dict):
        return None
    generated = snapshot.get("generated")
    if not _finite(generated):
        generated = time.time()
    accounts = []
    for account in snapshot.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        name = account.get("name")
        provider = account.get("provider")
        if not isinstance(name, str) or not isinstance(provider, str):
            continue
        left = primary_left_percent(account)
        used = None if left is None else round(100.0 - left, 2)
        windows = {}
        for key, window in (account.get("windows") or {}).items():
            if not isinstance(window, dict):
                continue
            pct = window.get("used_percent")
            if _finite(pct) and 0 <= pct <= 100:
                entry = {
                    "used": round(float(pct), 2),
                    "left": round(100.0 - float(pct), 2),
                }
                for field in ("used_units", "limit_units", "remaining_units",
                              "resets_at", "window_minutes"):
                    if _finite(window.get(field)):
                        entry[field] = window[field]
                if window.get("countdown"):
                    entry["countdown"] = window["countdown"]
                windows[str(key)] = entry
        accounts.append({
            "name": name,
            "provider": provider,
            "email": account.get("email") if isinstance(account.get("email"), str)
                     else None,
            "ok": bool(account.get("ok")),
            "left": left,
            "used": used,
            "windows": windows,
            "monthly_cost_usd": (account.get("monthly_cost_usd")
                                 if _finite(account.get("monthly_cost_usd"))
                                 else None),
            "plan": account.get("plan") if isinstance(account.get("plan"), str)
                    else None,
        })
    if not accounts:
        return None
    return {
        "schema": SCHEMA,
        "t": int(generated),
        "accounts": accounts,
    }


def record_snapshot(snapshot, retention_days=None):
    """Append a sample and prune old lines. Failures are silent (non-fatal)."""
    sample = sample_from_snapshot(snapshot)
    if sample is None:
        return False
    path = history_path()
    paths.ensure_private(paths.state_dir())
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, separators=(",", ":"),
                                    allow_nan=False))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        prune(retention_days=retention_days)
        return True
    except OSError:
        return False


def prune(retention_days=None, now=None):
    retention_days = (DEFAULT_RETENTION_DAYS if retention_days is None
                      else int(retention_days))
    now = int(time.time() if now is None else now)
    cutoff = now - max(1, retention_days) * 86400
    path = history_path()
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return 0
    kept = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = row.get("t")
        if _finite(t) and int(t) >= cutoff:
            kept.append(json.dumps(row, separators=(",", ":"), allow_nan=False)
                        + "\n")
    if len(kept) == len(lines):
        return 0
    try:
        import tempfile
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".headroom-hist-", suffix=".jsonl.tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(kept)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        return len(lines) - len(kept)
    except OSError:
        return 0


def load_samples(since=None, until=None, limit=50000):
    """Load raw samples newest-last, optionally time-bounded."""
    path = history_path()
    if not os.path.exists(path):
        return []
    samples = []
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
                samples.append(row)
                if len(samples) > limit:
                    samples = samples[-limit:]
    except OSError:
        return []
    samples.sort(key=lambda row: row["t"])
    return samples


def series_color(provider, email=None, name=None):
    """Professional HSL color: provider hue + shade from identity."""
    hue = PROVIDER_HUE.get((provider or "").lower(), 210)
    seed = (email or name or "x").lower().encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    n = int(digest[:8], 16)
    # Distinct shades within a provider family
    lightness = 42 + (n % 22)          # 42–63
    saturation = 58 + (n // 16 % 18)   # 58–75
    hue = (hue + (n % 9) - 4) % 360    # slight hue drift
    return f"hsl({hue} {saturation}% {lightness}%)"


def _bucket_ts(t, width):
    return int(t // width) * width


def ensure_seed_from_public():
    """If the log is empty, seed one point from the current public snapshot."""
    path = history_path()
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return False
    snapshot = paths.load_json(paths.public_snapshot_path())
    if not snapshot:
        snapshot = paths.load_json(paths.private_snapshot_path())
    return record_snapshot(snapshot)


def query(range_name="week", metric="left", now=None):
    """Return chart-ready payload for day|week|month.

    metric: ``left`` (remaining capacity %) or ``used``.
    """
    range_name = (range_name or "week").lower()
    if range_name not in RANGES:
        range_name = "week"
    metric = "used" if metric == "used" else "left"
    now = int(time.time() if now is None else now)
    since = now - RANGES[range_name]
    width = BUCKETS[range_name]
    ensure_seed_from_public()
    samples = load_samples(since=since, until=now + 60)

    # series_key -> {meta, buckets: {bucket_ts: [values]}}
    series_map = {}
    for sample in samples:
        t = int(sample["t"])
        bucket = _bucket_ts(t, width)
        for account in sample.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            name = account.get("name")
            provider = account.get("provider")
            if not name or not provider:
                continue
            value = account.get(metric)
            if not _finite(value):
                continue
            key = f"{provider}:{name}"
            entry = series_map.setdefault(key, {
                "id": key,
                "name": name,
                "provider": provider,
                "email": account.get("email"),
                "color": series_color(provider, account.get("email"), name),
                "buckets": {},
            })
            entry["buckets"].setdefault(bucket, []).append(float(value))
            if account.get("email") and not entry.get("email"):
                entry["email"] = account.get("email")

    # Build sorted time axis covering the full range for stable charts
    start_bucket = _bucket_ts(since, width)
    end_bucket = _bucket_ts(now, width)
    times = []
    cursor = start_bucket
    while cursor <= end_bucket:
        times.append(cursor)
        cursor += width

    redact = True
    try:
        redact = bool(registry.dashboard_settings().get("redact_emails", True))
    except Exception:  # noqa: BLE001 — charts still work without settings
        redact = True

    series = []
    for key in sorted(series_map.keys()):
        entry = series_map[key]
        points = []
        for bucket in times:
            values = entry["buckets"].get(bucket)
            if not values:
                points.append(None)
            else:
                points.append(round(sum(values) / len(values), 2))
        email = entry.get("email")
        display_email = _maybe_redact(email, redact)
        series.append({
            "id": entry["id"],
            "name": entry["name"],
            "provider": entry["provider"],
            "email": display_email,
            "color": entry["color"],
            "label": _series_label(entry, display_email),
            "points": points,
        })

    return {
        "schema": "headroom_history@1",
        "range": range_name,
        "metric": metric,
        "metric_label": ("Remaining capacity %" if metric == "left"
                         else "Used capacity %"),
        "since": since,
        "until": now,
        "bucket_seconds": width,
        "times": times,
        "series": series,
        "sample_count": len(samples),
        "retention_days": DEFAULT_RETENTION_DAYS,
        "providers": sorted({s["provider"] for s in series}),
    }


def _maybe_redact(email, redact):
    if not email or not redact:
        return email
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return (local[0] if local else "") + "***@" + domain


def _series_label(entry, display_email=None):
    email = display_email if display_email is not None else (entry.get("email") or "")
    name = entry.get("name") or ""
    provider = (entry.get("provider") or "").capitalize()
    if email and "@" in email:
        local = email.split("@", 1)[0]
        return f"{provider} · {local}"
    return f"{provider} · {name}"
