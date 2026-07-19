"""HTTP mutation handlers for the local dashboard (loopback only).

Keeps domain routing out of the long Handler method so each surface
(accounts, providers, burn, insights) has a single focused entry.
"""
from __future__ import annotations

import json
import os
import urllib.parse

from . import burn as burn_mod
from . import collect as collector
from . import insights as insights_mod
from . import manage, paths


def overlay_public_from_config():
    """Merge config-only fields/order into the public usage snapshot.

    Used after account field edits and reorders so the dashboard updates
    without a full multi-provider network collect.
    """
    snap_path = paths.public_snapshot_path()
    if not os.path.exists(snap_path):
        return False
    try:
        with open(snap_path) as handle:
            snapshot = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("accounts"), list):
        return False

    try:
        config_accounts = manage.list_state().get("accounts") or []
    except Exception:  # noqa: BLE001
        return False
    by_name = {a["name"]: a for a in config_accounts if a.get("name")}
    order = [a["name"] for a in config_accounts if a.get("name")]

    existing = {
        row.get("name"): row
        for row in snapshot["accounts"]
        if isinstance(row, dict) and row.get("name")
    }
    reordered = []
    for name in order:
        row = existing.get(name)
        cfg = by_name.get(name) or {}
        if row is None:
            # New slot with no live reading yet — publish a held stub so UI shows it
            row = {
                "name": name,
                "provider": cfg.get("provider"),
                "ok": False,
                "note": "waiting for collect",
                "windows": {},
            }
        else:
            row = dict(row)
        # Config overlays (operator-declared pins always win)
        if cfg.get("monthly_cost_usd") is not None:
            row["monthly_cost_usd"] = cfg.get("monthly_cost_usd")
        if cfg.get("annual_cost_usd") is not None:
            row["annual_cost_usd"] = cfg.get("annual_cost_usd")
        if cfg.get("renews_on"):
            row["renews_on"] = cfg["renews_on"]
        else:
            row.pop("renews_on", None)
        if cfg.get("renew_amount") is not None:
            row["renew_amount"] = cfg["renew_amount"]
        else:
            row.pop("renew_amount", None)
        reordered.append(row)

    # Drop removed slots; keep unknown extras only if not in config (shouldn't happen)
    snapshot["accounts"] = reordered
    try:
        # Re-run display projection if available
        from . import dashboard as dash_mod
        paths.write_json_atomic(
            snap_path,
            dash_mod.display_snapshot(snapshot),
            mode=0o644,
        )
    except Exception:  # noqa: BLE001
        try:
            with open(snap_path, "w") as handle:
                json.dump(snapshot, handle, allow_nan=False)
        except OSError:
            return False
    return True


def after_config_change(collect=False):
    """Publish dashboard after a config mutation.

    collect=False (default): overlay config onto public snapshot + rebuild shell.
    collect=True: full multi-provider collect (add/remove account, explicit refresh).
    """
    from . import dashboard as dash_mod

    if collect:
        try:
            collector.run_collect(quiet=True)
        except Exception:  # noqa: BLE001
            pass
    else:
        overlay_public_from_config()
    try:
        dash_mod.build(
            snapshot_file=paths.public_snapshot_path()
            if os.path.exists(paths.public_snapshot_path()) else None)
    except Exception:  # noqa: BLE001
        pass


def handle_mutate(handler, method, route):
    """Dispatch dashboard API mutations. Returns True if handled."""
    if handler.demo:
        handler._send_json(403, {
            "error": "account management disabled in demo mode",
            "demo": True,
        })
        return True

    try:
        body = {} if method == "DELETE" else handler._read_json_body()
        if _dispatch(handler, method, route, body):
            return True
        handler._send_json(404, {"error": "not found"})
        return True
    except manage.ManageError as error:
        handler._send_json(error.status, {"error": str(error)})
        return True
    except TypeError as error:
        handler._send_json(400, {"error": str(error)})
        return True
    except Exception as error:  # noqa: BLE001
        handler._send_json(500, {"error": "internal error: " + type(error).__name__})
        return True


def _dispatch(handler, method, route, body):
    if route == "/api/insights/key" and method == "DELETE":
        handler._send_json(200, {"ok": True, "status": insights_mod.clear_api_key()})
        return True

    if route == "/api/collect" and method == "POST":
        after_config_change(collect=True)
        handler._send_json(200, {"ok": True, "state": manage.list_state()})
        return True

    if route == "/api/insights/key" and method in ("POST", "PATCH"):
        key = body.get("api_key")
        if key is None and "key" in body:
            key = body.get("key")
        if method == "POST" and body.get("clear"):
            handler._send_json(200, {"ok": True, "status": insights_mod.clear_api_key()})
            return True
        result = insights_mod.set_api_key(
            key,
            provider=body.get("provider"),
            model=body.get("model"),
            base_url=body.get("base_url"),
            enabled=body.get("enabled", True),
        )
        handler._send_json(200, {"ok": True, "status": result})
        return True

    if route == "/api/insights" and method == "POST":
        payload = insights_mod.generate(force=True)
        status = 200 if payload.get("ok") else 400
        if payload.get("error") == "provider_error":
            status = 502
        handler._send_json(status, payload)
        return True

    if route == "/api/burn" and method == "POST":
        goal = burn_mod.create_goal(
            label=body.get("label"),
            provider=body.get("provider") or "any",
            account=body.get("account"),
            target=body.get("target"),
            unit=body.get("unit") or "tokens",
            deadline=body.get("deadline"),
            email=body.get("email"),
            notify_email=body.get("notify_email", True),
            notify_browser=body.get("notify_browser", True),
        )
        handler._send_json(201, {"ok": True, "goal": goal,
                                  "status": burn_mod.list_status()})
        return True

    if route == "/api/burn/email" and method in ("POST", "PATCH"):
        result = burn_mod.set_email_config(
            smtp_host=body.get("smtp_host") or body.get("host"),
            smtp_port=body.get("smtp_port") or body.get("port") or 587,
            smtp_user=body.get("smtp_user") or body.get("user"),
            smtp_password=body.get("smtp_password") or body.get("password"),
            mail_from=body.get("from") or body.get("mail_from"),
            use_tls=body.get("use_tls", True),
        )
        handler._send_json(200, result)
        return True

    if route == "/api/burn/check" and method == "POST":
        handler._send_json(200, burn_mod.check_and_notify(
            force=bool(body.get("force"))))
        return True

    burn_prefix = "/api/burn/"
    if route.startswith(burn_prefix) and len(route) > len(burn_prefix):
        goal_id = urllib.parse.unquote(route[len(burn_prefix):])
        if goal_id not in ("email", "check"):
            if method == "PATCH":
                goal = burn_mod.update_goal(goal_id, **body)
                handler._send_json(200, {"ok": True, "goal": goal,
                                          "status": burn_mod.list_status()})
                return True
            if method == "DELETE":
                result = burn_mod.delete_goal(goal_id)
                handler._send_json(200, {"ok": True, **result,
                                          "status": burn_mod.list_status()})
                return True

    if route == "/api/terminal" and method == "POST":
        handler._api_open_terminal(body)
        return True

    if route == "/api/accounts" and method == "POST":
        return _handle_accounts_post(handler, body)

    if route == "/api/accounts/order" and method in ("PUT", "PATCH", "POST"):
        result = manage.reorder_accounts(
            body.get("order") or body.get("names") or [])
        after_config_change(collect=False)
        handler._send_json(200, {
            "ok": True, **result, "state": manage.list_state()})
        return True

    if route == "/api/dashboard" and method == "PATCH":
        dash = manage.update_dashboard(**body)
        after_config_change(collect=False)
        handler._send_json(200, {"ok": True, "dashboard": dash})
        return True

    if route == "/api/providers/order" and method in ("PUT", "PATCH", "POST"):
        if body.get("direction") or body.get("dir"):
            dash = manage.move_provider(
                body.get("provider") or body.get("name"),
                body.get("direction") or body.get("dir"))
        else:
            dash = manage.reorder_providers(
                body.get("order") or body.get("providers") or [])
        after_config_change(collect=False)
        handler._send_json(200, {
            "ok": True,
            "dashboard": dash,
            "provider_order": dash.get("provider_order"),
            "state": manage.list_state(),
        })
        return True

    prefix = "/api/accounts/"
    if route.startswith(prefix) and len(route) > len(prefix):
        name = urllib.parse.unquote(route[len(prefix):])
        if "/" in name or name in (".", ".."):
            raise manage.ManageError("invalid account name", 400)
        if name == "order" and method in ("PUT", "PATCH", "POST"):
            result = manage.reorder_accounts(
                body.get("order") or body.get("names") or [])
            after_config_change(collect=False)
            handler._send_json(200, {
                "ok": True, **result, "state": manage.list_state()})
            return True
        if method == "PATCH":
            if body.get("direction") or body.get("dir"):
                result = manage.move_account(
                    name,
                    body.get("direction") or body.get("dir"),
                    scope=body.get("scope") or "provider")
                after_config_change(collect=False)
                handler._send_json(200, {
                    "ok": True, **result, "state": manage.list_state()})
                return True
            # Field edit — config only
            result = manage.update_account(name, **body)
            after_config_change(collect=False)
            handler._send_json(200, {"ok": True, "account": result,
                                      "state": manage.list_state()})
            return True
        if method == "DELETE":
            result = manage.remove_account(name)
            after_config_change(collect=True)  # drop readings for removed slot
            handler._send_json(200, {"ok": True, **result,
                                      "state": manage.list_state()})
            return True

    return False


def _handle_accounts_post(handler, body):
    mode = (body.get("mode") or body.get("action") or "adopt").lower()
    # Canonical order API also accepted via POST mode for UI compatibility
    if mode in ("reorder", "order"):
        result = manage.reorder_accounts(
            body.get("order") or body.get("names") or [])
        after_config_change(collect=False)
        handler._send_json(200, {
            "ok": True, **result, "state": manage.list_state()})
        return True
    if mode in ("move", "move_account"):
        result = manage.move_account(
            body.get("name") or body.get("account"),
            body.get("direction") or body.get("dir"),
            scope=body.get("scope") or "provider")
        after_config_change(collect=False)
        handler._send_json(200, {
            "ok": True, **result, "state": manage.list_state()})
        return True

    result = handler._api_add_account(body)
    needs_collect = mode not in ("prepare", "fresh", "prepare_fresh")
    if needs_collect:
        after_config_change(collect=True)
    else:
        after_config_change(collect=False)
    status_code = 200 if mode in ("prepare", "fresh", "prepare_fresh") else 201
    payload = {"ok": True, "state": manage.list_state()}
    if mode in ("prepare", "fresh", "prepare_fresh"):
        payload["prepared"] = result
    else:
        payload["account"] = result
    handler._send_json(status_code, payload)
    return True
