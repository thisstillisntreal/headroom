"""Bounded launch-event notifications for wrapper scripts.

When ``HEADROOM_NOTIFY_CMD`` names a command, headroom invokes it at launch
transitions with a single JSON argument describing the event:

    {"event": "launch", "mode": "supervised"|"exec",
     "account": ..., "model": ..., "note": ...}
    {"event": "downgrade", "account": ..., "reason": ...}
    {"event": "supervision_lost", "account": ..., "reason": ...}
    {"event": "fallback", "reason": ...}

Delivery is best-effort and bounded: the command gets a hard timeout
(default 10s, override with ``HEADROOM_NOTIFY_TIMEOUT``) and is killed when
it exceeds it. A broken, missing, or hung notify command is swallowed with a
stderr line — it must never block, materially delay, or kill the launch.
This replaces external marker-polling with events; it composes with, and is
independent of, the ``HEADROOM_LAUNCH_MARKER`` handshake.
"""
import json
import os
import shlex
import subprocess
import sys

NOTIFY_TIMEOUT = 10.0


def _timeout():
    raw = os.environ.get("HEADROOM_NOTIFY_TIMEOUT", "").strip()
    if not raw:
        return NOTIFY_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return NOTIFY_TIMEOUT
    # a non-positive or absurd override falls back to the default: the bound
    # must stay a real bound, never "wait forever"
    return value if 0 < value <= 60 else NOTIFY_TIMEOUT


def emit(event):
    """Deliver one event to HEADROOM_NOTIFY_CMD; never raises, never unbounded.

    Returns True when the command ran to completion (its exit status is
    deliberately ignored — a failing observer must not fail the launch),
    False when no command is configured or delivery failed/timed out."""
    raw = os.environ.get("HEADROOM_NOTIFY_CMD", "").strip()
    if not raw:
        return False
    try:
        argv = shlex.split(raw)
        if not argv:
            return False
        payload = json.dumps(event, sort_keys=True, allow_nan=False)
        # start_new_session so a hung command can be killed without touching
        # the launch's terminal/process group; all stdio detached so a chatty
        # observer can never corrupt the CLI's screen
        process = subprocess.Popen(
            argv + [payload],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            process.wait(timeout=_timeout())
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            print("[headroom] notify command timed out and was killed "
                  "(launch continues)", file=sys.stderr)
            return False
        return True
    except Exception as error:  # noqa: BLE001 — an observer can never be fatal
        print(f"[headroom] notify failed: {error} (launch continues)",
              file=sys.stderr)
        return False
