"""Use-it-or-lose-it burn goals: burn X credits/tokens before a deadline.

Goals live in ``config.json`` under ``burn_goals``. Progress is measured from
local ledgers/history (never invents provider data). Notifications:

* browser / local push via the dashboard Notification API (client-side)
* optional email via SMTP settings in ``secrets.json``
* optional ``HEADROOM_NOTIFY_CMD`` (existing headroom notify hook)

Example goal::

    {
      "id": "nv-free-2027",
      "label": "NVIDIA free credits",
      "provider": "nvidia",
      "target": 100000,
      "unit": "tokens",
      "deadline": "2027-01-01T00:00:00Z",
      "email": "you@example.com",
      "notify_email": true,
      "notify_browser": true,
      "baseline_used": 0,
      "created_at": 1700000000
    }
"""
import json
import os
import smtplib
import time
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage

from . import history, manage, nvidia_track, paths, registry
from . import notify as launch_notify

UNITS = ("tokens", "credits", "requests", "units")
ALERT_THRESHOLDS = (25, 50, 75, 90, 100)  # % of TIME elapsed
BURN_THRESHOLDS = (25, 50, 75, 90, 100)   # % of TARGET burned


def _now():
    return int(time.time())


def _parse_deadline(value):
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    try:
        # Accept YYYY-MM-DD or full ISO
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            text = text + "T00:00:00+00:00"
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError as error:
        raise manage.ManageError(f"invalid deadline: {value!r}") from error


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def alerts_path():
    return os.path.join(paths.state_dir(), "burn-alerts.json")


def load_alerts():
    raw = paths.load_json(alerts_path())
    return raw if isinstance(raw, dict) else {"sent": {}}


def save_alerts(document):
    paths.ensure_private(paths.state_dir())
    paths.write_json_atomic(alerts_path(), document, mode=0o600)


def list_goals():
    config = manage.load_or_empty()
    goals = config.get("burn_goals")
    if not isinstance(goals, list):
        return []
    return [g for g in goals if isinstance(g, dict) and g.get("id")]


def _save_goals(goals):
    def _mutate(cfg):
        cfg["burn_goals"] = goals

    try:
        registry.mutate(_mutate)
    except registry.RegistryError:
        config = manage.load_or_empty()
        config["burn_goals"] = goals
        if config.get("accounts"):
            manage.save_config(config)
        else:
            raise manage.ManageError(
                "add at least one account before creating burn goals", 400)


def measure_used(provider="any", unit="tokens", account=None, since=None):
    """How many units have been consumed since ``since`` (epoch)."""
    since = int(since or 0)
    provider = (provider or "any").lower()
    unit = (unit or "tokens").lower()
    if unit not in UNITS:
        unit = "tokens"

    burned = 0.0

    # NVIDIA dedicated ledger (tokens / requests)
    if provider in ("nvidia", "any") and unit in ("tokens", "requests"):
        calls = nvidia_track.load_calls(since=since)
        if unit == "requests":
            burned += float(len(calls))
        else:
            burned += float(sum(int(c.get("total_tokens") or 0) for c in calls))
        if provider == "nvidia":
            return round(burned, 2)

    # History-based deltas (credits / units / also supplements "any")
    samples = history.load_samples(since=since)
    if not samples:
        return round(burned, 2)

    def account_match(row):
        if account and row.get("name") != account:
            return False
        if provider not in (None, "", "any") and row.get("provider") != provider:
            return False
        # Don't double-count nvidia when already counted from ledger
        if provider == "any" and row.get("provider") == "nvidia" \
                and unit in ("tokens", "requests"):
            return False
        return True

    def units_from_account(row):
        windows = row.get("windows") if isinstance(row.get("windows"), dict) else {}
        if unit in ("units", "credits", "tokens"):
            for key in ("month", "7d", "5h"):
                w = windows.get(key) or {}
                if _finite(w.get("used_units")):
                    return float(w["used_units"])
            if unit == "units" and _finite(row.get("used")):
                return float(row["used"])
            return 0.0
        return 0.0

    first_totals = {}
    last_totals = {}
    for sample in samples:
        for row in sample.get("accounts") or []:
            if not isinstance(row, dict) or not account_match(row):
                continue
            key = f"{row.get('provider')}:{row.get('name')}"
            value = units_from_account(row)
            if key not in first_totals:
                first_totals[key] = value
            last_totals[key] = value

    for key, last in last_totals.items():
        first = first_totals.get(key, last)
        delta = last - first
        if delta > 0:
            burned += delta

    return round(burned, 2)


def evaluate_goal(goal, now=None):
    """Attach live progress + countdown fields to a goal dict (copy)."""
    now = _now() if now is None else int(now)
    goal = dict(goal)
    deadline = _parse_deadline(goal.get("deadline"))
    created = int(goal.get("created_at") or now)
    target = float(goal.get("target") or 0)
    unit = (goal.get("unit") or "tokens").lower()
    provider = (goal.get("provider") or "any").lower()
    account = goal.get("account")
    baseline = float(goal.get("baseline_used") or 0)

    current_used = measure_used(provider=provider, unit=unit,
                                account=account, since=created)
    # burned since goal creation relative to baseline snapshot
    burned = max(0.0, current_used - baseline) if baseline else current_used
    # If baseline was absolute lifetime used at create, delta is correct.
    # When baseline is 0 and measure is since-created, current_used is already delta.
    if baseline == 0:
        burned = current_used

    remaining_to_burn = max(0.0, target - burned)
    progress = 0.0 if target <= 0 else min(100.0, round(100.0 * burned / target, 1))

    seconds_left = None if deadline is None else deadline - now
    total_window = None if deadline is None else max(1, deadline - created)
    time_elapsed_pct = None
    if total_window is not None and seconds_left is not None:
        time_elapsed_pct = min(
            100.0, max(0.0, round(100.0 * (now - created) / total_window, 1)))

    pace_ok = None
    if target > 0 and total_window and time_elapsed_pct is not None:
        # On pace if burned% >= time_elapsed% (roughly)
        pace_ok = progress + 1e-6 >= time_elapsed_pct * 0.85

    status = "active"
    if deadline is not None and seconds_left is not None and seconds_left <= 0:
        status = "expired" if progress < 100 else "completed"
    elif progress >= 100:
        status = "completed"

    goal.update({
        "deadline_epoch": deadline,
        "seconds_left": seconds_left,
        "countdown": _format_countdown(seconds_left),
        "burned": burned,
        "remaining_to_burn": remaining_to_burn,
        "progress_percent": progress,
        "time_elapsed_percent": time_elapsed_pct,
        "pace_ok": pace_ok,
        "status": status,
        "unit": unit,
        "provider": provider,
    })
    return goal


def _format_countdown(seconds_left):
    if seconds_left is None:
        return None
    if seconds_left <= 0:
        return "EXPIRED"
    days, rem = divmod(int(seconds_left), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    if hours > 0:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


def list_status():
    goals = [evaluate_goal(g) for g in list_goals()]
    return {
        "goals": goals,
        "email_ready": bool(_smtp_config().get("host")),
        "notify_cmd": bool(os.environ.get("HEADROOM_NOTIFY_CMD", "").strip()),
    }


def create_goal(*, label, provider="any", account=None, target=0, unit="tokens",
                deadline=None, email=None, notify_email=True,
                notify_browser=True):
    if not label or not str(label).strip():
        raise manage.ManageError("label is required", 400)
    target = float(target or 0)
    if target <= 0:
        raise manage.ManageError("target must be > 0", 400)
    unit = (unit or "tokens").lower()
    if unit not in UNITS:
        raise manage.ManageError(f"unit must be one of {UNITS}", 400)
    provider = (provider or "any").lower()
    deadline_epoch = _parse_deadline(deadline)
    if deadline_epoch is None:
        raise manage.ManageError(
            "deadline required (YYYY-MM-DD or ISO datetime)", 400)
    if deadline_epoch <= _now():
        raise manage.ManageError("deadline must be in the future", 400)
    if email and "@" not in str(email):
        raise manage.ManageError("email looks invalid", 400)

    now = _now()
    baseline = measure_used(provider=provider, unit=unit, account=account,
                            since=0)
    # For since-created measurement we store baseline_used as 0 and measure
    # with since=created_at. baseline_lifetime kept for reference.
    goal = {
        "id": uuid.uuid4().hex[:12],
        "label": str(label).strip()[:80],
        "provider": provider,
        "account": account or None,
        "target": target,
        "unit": unit,
        "deadline": datetime.fromtimestamp(
            deadline_epoch, timezone.utc
        ).isoformat().replace("+00:00", "Z"),
        "email": (str(email).strip() if email else None),
        "notify_email": bool(notify_email),
        "notify_browser": bool(notify_browser),
        "baseline_used": 0.0,
        "baseline_lifetime": baseline,
        "created_at": now,
    }
    goals = list_goals()
    goals.append(goal)
    _save_goals(goals)
    return evaluate_goal(goal)


def delete_goal(goal_id):
    goals = list_goals()
    kept = [g for g in goals if g.get("id") != goal_id]
    if len(kept) == len(goals):
        raise manage.ManageError(f"no burn goal {goal_id!r}", 404)
    _save_goals(kept)
    alerts = load_alerts()
    alerts.get("sent", {}).pop(goal_id, None)
    save_alerts(alerts)
    return {"removed": goal_id}


def update_goal(goal_id, **fields):
    goals = list_goals()
    match = next((g for g in goals if g.get("id") == goal_id), None)
    if match is None:
        raise manage.ManageError(f"no burn goal {goal_id!r}", 404)
    allowed = {"label", "email", "notify_email", "notify_browser", "target",
               "deadline", "provider", "account", "unit"}
    unknown = set(fields) - allowed
    if unknown:
        raise manage.ManageError(f"unsupported fields: {sorted(unknown)}", 400)
    if "label" in fields and fields["label"]:
        match["label"] = str(fields["label"]).strip()[:80]
    if "email" in fields:
        email = fields["email"]
        match["email"] = (str(email).strip() if email else None)
    if "notify_email" in fields:
        match["notify_email"] = bool(fields["notify_email"])
    if "notify_browser" in fields:
        match["notify_browser"] = bool(fields["notify_browser"])
    if "target" in fields:
        target = float(fields["target"])
        if target <= 0:
            raise manage.ManageError("target must be > 0", 400)
        match["target"] = target
    if "deadline" in fields:
        epoch = _parse_deadline(fields["deadline"])
        if epoch is None or epoch <= _now():
            raise manage.ManageError("deadline must be a future date", 400)
        match["deadline"] = datetime.fromtimestamp(
            epoch, timezone.utc).isoformat().replace("+00:00", "Z")
    if "provider" in fields and fields["provider"]:
        match["provider"] = str(fields["provider"]).lower().strip()
    if "account" in fields:
        match["account"] = fields["account"] or None
    if "unit" in fields and fields["unit"]:
        unit = str(fields["unit"]).lower()
        if unit not in UNITS:
            raise manage.ManageError(f"unit must be one of {UNITS}", 400)
        match["unit"] = unit
    _save_goals(goals)
    return evaluate_goal(match)


def _smtp_config():
    """SMTP settings from secrets.json → email block."""
    try:
        from . import insights as insights_mod
        secrets = insights_mod.load_secrets()
    except Exception:  # noqa: BLE001
        secrets = {}
    email_cfg = secrets.get("email") if isinstance(secrets, dict) else None
    if not isinstance(email_cfg, dict):
        email_cfg = {}
    # Env fallbacks
    host = email_cfg.get("smtp_host") or os.environ.get("HEADROOM_SMTP_HOST")
    port = email_cfg.get("smtp_port") or os.environ.get("HEADROOM_SMTP_PORT") or 587
    user = email_cfg.get("smtp_user") or os.environ.get("HEADROOM_SMTP_USER")
    password = email_cfg.get("smtp_password") or os.environ.get("HEADROOM_SMTP_PASSWORD")
    mail_from = email_cfg.get("from") or os.environ.get("HEADROOM_SMTP_FROM") or user
    use_tls = email_cfg.get("use_tls", True)
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 587
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from": mail_from,
        "use_tls": bool(use_tls),
    }


def set_email_config(*, smtp_host, smtp_port=587, smtp_user=None,
                     smtp_password=None, mail_from=None, use_tls=True):
    from . import insights as insights_mod
    secrets = insights_mod.load_secrets()
    secrets["email"] = {
        "smtp_host": smtp_host,
        "smtp_port": int(smtp_port or 587),
        "smtp_user": smtp_user,
        "smtp_password": smtp_password,
        "from": mail_from or smtp_user,
        "use_tls": bool(use_tls),
    }
    insights_mod.save_secrets(secrets)
    return {"ok": True, "email_ready": bool(smtp_host)}


def send_email(to_addr, subject, body):
    cfg = _smtp_config()
    if not cfg.get("host") or not to_addr:
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg.get("from") or cfg.get("user") or "headroom@localhost"
    message["To"] = to_addr
    message.set_content(body)
    try:
        if cfg.get("use_tls"):
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as smtp:
                smtp.ehlo()
                smtp.starttls()
                if cfg.get("user"):
                    smtp.login(cfg["user"], cfg.get("password") or "")
                smtp.send_message(message)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as smtp:
                if cfg.get("user"):
                    smtp.login(cfg["user"], cfg.get("password") or "")
                smtp.send_message(message)
        return True
    except Exception as error:  # noqa: BLE001
        print(f"[headroom] burn email failed: {error}", flush=True)
        return False


def _alert_message(goal, kind, detail):
    label = goal.get("label") or goal.get("id")
    provider = (goal.get("provider") or "any").upper()
    countdown = goal.get("countdown") or "?"
    burned = goal.get("burned")
    target = goal.get("target")
    unit = goal.get("unit")
    remaining = goal.get("remaining_to_burn")
    subject = f"headroom · USE IT OR LOSE IT · {label}"
    body = (
        f"{detail}\n\n"
        f"Goal: {label}\n"
        f"Provider: {provider}\n"
        f"Progress: {burned} / {target} {unit} ({goal.get('progress_percent')}%)\n"
        f"Still to burn: {remaining} {unit}\n"
        f"Countdown: {countdown}\n"
        f"Deadline: {goal.get('deadline')}\n"
        f"Status: {goal.get('status')}\n\n"
        f"Open http://127.0.0.1:8377/ and spend those credits before they're gone.\n"
    )
    return subject, body


def check_and_notify(force=False):
    """Evaluate goals, send due alerts, return status + browser push payloads."""
    alerts = load_alerts()
    sent = alerts.setdefault("sent", {})
    browser_pushes = []
    email_sent = []
    evaluated = []

    for raw in list_goals():
        goal = evaluate_goal(raw)
        evaluated.append(goal)
        gid = goal["id"]
        record = sent.setdefault(gid, {})
        # Time-based urgency alerts
        te = goal.get("time_elapsed_percent")
        if te is not None and goal.get("status") == "active":
            for threshold in ALERT_THRESHOLDS:
                key = f"time_{threshold}"
                if te >= threshold and (force or not record.get(key)):
                    detail = (
                        f"{threshold}% of the countdown is gone — "
                        f"USE your {goal.get('provider', 'provider').upper()} "
                        f"credits before {goal.get('deadline')} or LOSE THEM."
                    )
                    _dispatch_alert(goal, detail, record, key, browser_pushes,
                                    email_sent)
        # Burn progress milestones
        bp = goal.get("progress_percent") or 0
        for threshold in BURN_THRESHOLDS:
            key = f"burn_{threshold}"
            if bp >= threshold and (force or not record.get(key)):
                if threshold >= 100:
                    detail = (
                        f"Target hit — you burned {goal.get('burned')} "
                        f"{goal.get('unit')} before the deadline. Nice."
                    )
                else:
                    detail = (
                        f"Burn progress {threshold}% "
                        f"({goal.get('burned')}/{goal.get('target')} "
                        f"{goal.get('unit')}). Countdown: {goal.get('countdown')}."
                    )
                _dispatch_alert(goal, detail, record, key, browser_pushes,
                                email_sent)
        # Expired with remaining credits
        if goal.get("status") == "expired" and (force or not record.get("expired")):
            detail = (
                f"DEADLINE PASSED with {goal.get('remaining_to_burn')} "
                f"{goal.get('unit')} unspent on "
                f"{(goal.get('provider') or 'fleet').upper()}."
            )
            _dispatch_alert(goal, detail, record, "expired", browser_pushes,
                            email_sent)

    save_alerts(alerts)
    return {
        "goals": evaluated,
        "browser_pushes": browser_pushes,
        "email_sent": email_sent,
        "email_ready": bool(_smtp_config().get("host")),
    }


def _dispatch_alert(goal, detail, record, key, browser_pushes, email_sent):
    subject, body = _alert_message(goal, key, detail)
    record[key] = _now()
    if goal.get("notify_browser", True):
        browser_pushes.append({
            "title": subject,
            "body": detail,
            "goal_id": goal.get("id"),
            "tag": f"headroom-burn-{goal.get('id')}-{key}",
        })
    if goal.get("notify_email") and goal.get("email"):
        if send_email(goal["email"], subject, body):
            email_sent.append({"to": goal["email"], "key": key,
                               "goal_id": goal.get("id")})
    # Optional external push hook
    launch_notify.emit({
        "event": "burn_alert",
        "goal_id": goal.get("id"),
        "label": goal.get("label"),
        "provider": goal.get("provider"),
        "detail": detail,
        "countdown": goal.get("countdown"),
        "progress_percent": goal.get("progress_percent"),
        "status": goal.get("status"),
    })
