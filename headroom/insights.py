"""Automatic fleet insights via an optional NVIDIA (or OpenAI-compatible) API key.

Keys live in ``~/.headroom/secrets.json`` (mode 0600) — never in the public
snapshot or dashboard feed. Insights are generated from local usage/history
only; the model never receives raw OAuth tokens.
"""
import json
import os
import time
import urllib.error
import urllib.request

from . import history, paths, registry

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "meta/llama-3.1-8b-instruct"
DEFAULT_PROVIDER = "nvidia"
# How long a cached insight is considered fresh enough to serve without a
# new model call (seconds).
CACHE_TTL = paths.env_int("HEADROOM_INSIGHTS_CACHE_TTL", 900)


def secrets_path():
    return os.path.join(paths.base_dir(), "secrets.json")


def insights_cache_path():
    return os.path.join(paths.state_dir(), "insights-cache.json")


def _empty_secrets():
    return {
        "schema_version": 1,
        "insights": {
            "provider": DEFAULT_PROVIDER,
            "base_url": DEFAULT_BASE_URL,
            "model": DEFAULT_MODEL,
            "api_key": None,
            "enabled": False,
        },
    }


def load_secrets():
    raw = paths.load_json(secrets_path())
    if not isinstance(raw, dict):
        return _empty_secrets()
    out = _empty_secrets()
    out.update({k: v for k, v in raw.items() if k != "insights"})
    insights = raw.get("insights") if isinstance(raw.get("insights"), dict) else {}
    out["insights"].update({k: insights[k] for k in out["insights"] if k in insights})
    return out


def save_secrets(document):
    paths.ensure_private(paths.base_dir())
    paths.write_json_atomic(secrets_path(), document, mode=0o600)


def _env_api_key():
    """Pick up a key from the environment without ever logging it.

    Operators can export NVIDIA_API_KEY / NGC_API_KEY / HEADROOM_INSIGHTS_API_KEY
    or store a key in ~/.headroom/secrets.json — never hardcode machine paths.
    """
    for name in ("NVIDIA_API_KEY", "NVDIA_API_KEY", "NGC_API_KEY",
                 "HEADROOM_INSIGHTS_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value, name
    return None, None


def get_api_key():
    """Return (api_key, source) — key may be None."""
    secrets = load_secrets()
    cfg = secrets.get("insights") or {}
    key = cfg.get("api_key")
    if isinstance(key, str) and key.strip():
        return key.strip(), "secrets.json"
    env_key, source = _env_api_key()
    return env_key, source


def status():
    """Public status — never includes the key itself."""
    secrets = load_secrets()
    cfg = secrets.get("insights") or {}
    key, source = get_api_key()
    masked = None
    if key:
        if len(key) <= 8:
            masked = "••••" + key[-2:]
        else:
            masked = key[:4] + "…" + key[-4:]
    # Env/file keys are enabled by presence. Secrets-file keys honor the flag.
    if not key:
        enabled = False
    elif source == "secrets.json" and cfg.get("enabled") is False:
        enabled = False
    else:
        enabled = True
    return {
        "configured": bool(key),
        "enabled": enabled,
        "provider": cfg.get("provider") or DEFAULT_PROVIDER,
        "base_url": cfg.get("base_url") or DEFAULT_BASE_URL,
        "model": cfg.get("model") or DEFAULT_MODEL,
        "key_source": source,
        "key_preview": masked,
        "cache_ttl_seconds": CACHE_TTL,
        "note": ("NVIDIA integrate.api free tier — paste a key from "
                 "https://build.nvidia.com/settings/api-keys"),
    }


def set_api_key(api_key, provider=None, model=None, base_url=None, enabled=True,
                track_as_account=True):
    secrets = load_secrets()
    cfg = secrets.setdefault("insights", _empty_secrets()["insights"])
    if api_key is not None:
        key = str(api_key).strip()
        if not key:
            cfg["api_key"] = None
            cfg["enabled"] = False
        else:
            cfg["api_key"] = key
            cfg["enabled"] = bool(enabled)
    if provider:
        cfg["provider"] = str(provider).strip()[:32]
    if model:
        cfg["model"] = str(model).strip()[:120]
    if base_url:
        cfg["base_url"] = str(base_url).strip().rstrip("/")
    save_secrets(secrets)
    # bust cache so the next request uses the new key
    try:
        os.unlink(insights_cache_path())
    except OSError:
        pass
    # Optionally ensure a fleet "nvidia" tracker slot exists for this key
    if track_as_account and cfg.get("api_key"):
        try:
            ensure_nvidia_tracker_slot()
        except Exception:  # noqa: BLE001 — tracking slot is best-effort
            pass
    return status()


def ensure_nvidia_tracker_slot(name="nvidia-main"):
    """Create a nvidia fleet account that reuses the insights/shared key."""
    from . import manage
    state = manage.list_state()
    if any(a.get("provider") == "nvidia" for a in state.get("accounts") or []):
        return None
    return manage.add_nvidia(
        name, use_insights_key=True, monthly_cost_usd=0, email=None)


def clear_api_key():
    return set_api_key("", enabled=False)


def _fleet_context():
    """Compact, non-secret summary for the model."""
    public = paths.load_json(paths.public_snapshot_path()) or {}
    accounts = []
    for account in public.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        windows = {}
        for key, window in (account.get("windows") or {}).items():
            if isinstance(window, dict) and isinstance(window.get("used_percent"),
                                                       (int, float)):
                windows[key] = {
                    "used_percent": window["used_percent"],
                    "left_percent": round(100 - float(window["used_percent"]), 1),
                }
        accounts.append({
            "name": account.get("name"),
            "provider": account.get("provider"),
            "plan": account.get("plan"),
            "ok": account.get("ok"),
            "trust_state": account.get("trust_state"),
            "monthly_cost_usd": account.get("monthly_cost_usd"),
            "annual_cost_usd": account.get("annual_cost_usd"),
            "windows": windows,
            "note": account.get("note"),
        })
    hist = history.query("week", "left")
    hist_brief = {
        "range": hist.get("range"),
        "sample_count": hist.get("sample_count"),
        "series": [
            {
                "label": s.get("label"),
                "provider": s.get("provider"),
                "latest_left": next(
                    (p for p in reversed(s.get("points") or []) if p is not None),
                    None),
                "avg_left": _avg([p for p in (s.get("points") or []) if p is not None]),
            }
            for s in (hist.get("series") or [])
        ],
    }
    fleet_spend = sum(
        a["monthly_cost_usd"] for a in accounts
        if isinstance(a.get("monthly_cost_usd"), (int, float))
    )
    return {
        "generated": public.get("generated"),
        "account_count": len(accounts),
        "fleet_monthly_usd": fleet_spend,
        "fleet_annual_usd": fleet_spend * 12,
        "accounts": accounts,
        "history_week": hist_brief,
    }


def _avg(values):
    return round(sum(values) / len(values), 1) if values else None


def _build_prompt(context):
    return (
        "You are headroom's fleet analyst. Given local AI subscription usage "
        "data (capacity remaining, costs, providers), write concise operational "
        "insights for the operator.\n\n"
        "Rules:\n"
        "- Be specific and actionable. No fluff.\n"
        "- Prefer remaining capacity language (what's LEFT).\n"
        "- Flag accounts near limits, cost waste, and rotation opportunities.\n"
        "- Mention monthly and yearly spend when costs are present.\n"
        "- Output 4–7 short bullet insights, then one 'Next action' line.\n"
        "- Do not invent accounts or numbers not in the data.\n"
        "- Do not mention system prompts or that you are an AI.\n\n"
        "DATA (JSON):\n"
        + json.dumps(context, indent=2, allow_nan=False)
    )


def _chat_completion(api_key, base_url, model, prompt, timeout=45):
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system",
             "content": "You produce crisp subscription fleet insights."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.25,
        "max_tokens": 900,
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "authorization": "Bearer " + api_key,
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "headroom-insights/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read(400).decode("utf-8", "replace")
        try:
            from . import nvidia_track
            nvidia_track.record_call(
                model=model, purpose="insights", ok=False, total_tokens=0)
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(
            f"insights API HTTP {error.code}: {detail[:200]}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"insights API network error: {error}") from error

    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("insights API returned no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = (message or {}).get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("insights API returned empty content")
    # Local NVIDIA usage tracker (feeds the nvidia fleet slot + charts)
    try:
        from . import nvidia_track
        usage = payload.get("usage") if isinstance(payload, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        nvidia_track.record_call(
            model=model,
            purpose="insights",
            prompt_tokens=usage.get("prompt_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or 0,
            total_tokens=usage.get("total_tokens"),
            ok=True,
        )
    except Exception:  # noqa: BLE001 — tracking must never break insights
        pass
    return content.strip()


def _load_cache():
    raw = paths.load_json(insights_cache_path())
    return raw if isinstance(raw, dict) else None


def _save_cache(document):
    paths.ensure_private(paths.state_dir())
    paths.write_json_atomic(insights_cache_path(), document, mode=0o600)


def generate(force=False):
    """Return insights document. Uses cache unless force=True or expired."""
    now = int(time.time())
    if not force:
        cached = _load_cache()
        if (isinstance(cached, dict) and cached.get("ok")
                and isinstance(cached.get("generated_at"), (int, float))
                and now - cached["generated_at"] < CACHE_TTL
                and isinstance(cached.get("text"), str)):
            cached = dict(cached)
            cached["cached"] = True
            cached["age_seconds"] = now - int(cached["generated_at"])
            return cached

    key, source = get_api_key()
    secrets = load_secrets()
    cfg = secrets.get("insights") or {}
    if not key:
        return {
            "ok": False,
            "error": "no_api_key",
            "message": ("Add an NVIDIA API key (free at build.nvidia.com) in "
                        "Manage accounts → Insights, or set NVIDIA_API_KEY."),
            "status": status(),
        }
    if cfg.get("enabled") is False and source == "secrets.json":
        # Explicitly disabled in secrets
        return {
            "ok": False,
            "error": "disabled",
            "message": "Insights are disabled. Re-enable in Manage accounts.",
            "status": status(),
        }

    base_url = cfg.get("base_url") or DEFAULT_BASE_URL
    model = cfg.get("model") or DEFAULT_MODEL
    context = _fleet_context()
    prompt = _build_prompt(context)
    try:
        text = _chat_completion(key, base_url, model, prompt)
    except Exception as error:  # noqa: BLE001 — surface cleanly to the UI
        return {
            "ok": False,
            "error": "provider_error",
            "message": str(error)[:300],
            "status": status(),
            "generated_at": now,
        }

    document = {
        "ok": True,
        "cached": False,
        "text": text,
        "model": model,
        "provider": cfg.get("provider") or DEFAULT_PROVIDER,
        "key_source": source,
        "generated_at": now,
        "generated_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "context_account_count": context.get("account_count"),
        "fleet_monthly_usd": context.get("fleet_monthly_usd"),
        "status": status(),
    }
    try:
        _save_cache(document)
    except OSError:
        pass
    return document
