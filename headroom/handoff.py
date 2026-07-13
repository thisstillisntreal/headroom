"""Carry Claude conversation history across account slots without guessing.

A handoff is deliberately smaller than process migration: it copies one
verified transcript, records the exact next command, and lets Claude create a
fresh session id in the target home. The source remains immutable so every
transfer is reversible and auditable even after the terminal context is gone.
"""
import glob
import hashlib
import json
import math
import os
import re
import shlex
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass

from . import collect, paths, registry, route

SCHEMA = "headroom_handoff@1"
MAX_SCAN_AGE = 48 * 3600


class HandoffError(RuntimeError):
    """A user-actionable refusal; handoff guards intentionally fail closed."""


@dataclass(frozen=True)
class SourceSession:
    session_id: str
    transcript_path: str
    account: dict
    model: str = ""
    seen_at: int = 0


def _journal_path():
    return os.path.join(paths.state_dir(), "sessions.jsonl")


def _ledger_path():
    return os.path.join(paths.state_dir(), "handoffs.jsonl")


def _valid_uuid(value):
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (AttributeError, ValueError):
        return False


def _claude_slug(path):
    # Claude Code replaces every non-alphanumeric/non-hyphen character in
    # the absolute cwd with "-" when naming projects/<slug>.
    return re.sub(r"[^A-Za-z0-9-]", "-", path)


def _read_jsonl(path, label):
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError
                rows.append(row)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise HandoffError(f"{label} is unreadable — inspect {path}") from error
    return rows


def _account_for_path(path, accounts, config_dir=""):
    config_home = os.path.realpath(os.path.expanduser(config_dir)) \
        if config_dir else ""
    if config_home:
        for account in accounts:
            if os.path.realpath(account["home"]) == config_home:
                return account
    absolute = os.path.abspath(os.path.expanduser(path))
    for account in accounts:
        home = os.path.realpath(account["home"])
        try:
            if os.path.commonpath((absolute, home)) == home:
                return account
        except ValueError:
            continue
    return None


def _source(path, session_id, accounts, model="", seen_at=0, config_dir=""):
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.lexists(path):
        raise HandoffError(f"session {session_id} transcript no longer exists")
    account = _account_for_path(path, accounts, config_dir)
    if account is None:
        raise HandoffError(
            f"session {session_id} is not inside a configured Claude home")
    if account.get("provider") != "claude":
        raise HandoffError("handoff only supports same-provider Claude sessions")
    return SourceSession(session_id, path, account, model,
                         int(_timestamp({"ts": seen_at})))


def _age_text(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _timestamp(row):
    value = row.get("ts")
    return float(value) if _number(value) else 0.0


def _ambiguity(rows, now):
    lines = []
    for row in sorted(rows, key=_timestamp, reverse=True):
        age = _age_text(now - _timestamp(row))
        lines.append(f"  {row.get('session_id')}  age={age}  "
                     f"model={row.get('model') or '?'}")
    return ("multiple sessions share this cwd; pass --session UUID:\n"
            + "\n".join(lines))


def _filesystem_matches(session_id, accounts):
    matches = []
    for account in accounts:
        if account.get("provider") != "claude":
            continue
        pattern = os.path.join(account["home"], "projects", "**",
                               session_id + ".jsonl")
        for path in glob.glob(pattern, recursive=True):
            matches.append((path, account))
    return matches


def resolve_source(session_id=None, accounts=None, cwd=None, now=None):
    """Resolve a session from explicit intent, then journal, then a narrow scan.

    Transcript recency alone never decides between two sessions; ambiguity is
    surfaced because resuming the wrong conversation is worse than stopping.
    """
    accounts = registry.accounts() if accounts is None else accounts
    cwd = os.path.realpath(os.getcwd() if cwd is None else cwd)
    now = time.time() if now is None else now
    if session_id is not None:
        if not _valid_uuid(session_id):
            raise HandoffError("--session must be a UUID")
        session_id = str(uuid.UUID(session_id))
        journal_error = None
        try:
            journal = _read_jsonl(_journal_path(), "session journal")
        except HandoffError as error:
            journal, journal_error = [], error
        journal_hits = [row for row in journal
                        if row.get("session_id", "").lower() == session_id.lower()
                        and isinstance(row.get("transcript_path"), str)
                        and os.path.exists(row["transcript_path"])]
        if journal_hits:
            row = max(journal_hits, key=_timestamp)
            return _source(row["transcript_path"], session_id, accounts,
                           row.get("model", ""), row.get("ts", 0),
                           row.get("config_dir", ""))
        matches = _filesystem_matches(session_id, accounts)
        if len(matches) == 1:
            return SourceSession(session_id, os.path.abspath(matches[0][0]),
                                 matches[0][1])
        if len(matches) > 1:
            ledger_hits = [row for row in
                           _read_jsonl(_ledger_path(), "handoff ledger")
                           if row.get("session_id") == session_id]
            if ledger_hits:
                source_slot = max(ledger_hits, key=_timestamp).get("source_slot")
                for path, account in matches:
                    if account.get("name") == source_slot:
                        return SourceSession(session_id, os.path.abspath(path),
                                             account)
            raise HandoffError(
                f"session {session_id} matched {len(matches)} configured transcripts")
        if journal_error is not None:
            raise journal_error
        raise HandoffError(
            f"session {session_id} matched none configured transcripts")

    journal = _read_jsonl(_journal_path(), "session journal")
    rows = []
    for row in journal:
        row_cwd = row.get("cwd")
        if not isinstance(row_cwd, str) or os.path.realpath(row_cwd) != cwd:
            continue
        session = row.get("session_id")
        if not isinstance(session, str) or not _valid_uuid(session):
            continue
        if _timestamp(row) >= next(
                (_timestamp(item) for item in rows
                 if item.get("session_id") == session), -1):
            rows = [item for item in rows if item.get("session_id") != session]
            rows.append(row)
    if len(rows) > 1:
        raise HandoffError(_ambiguity(rows, now))
    if len(rows) == 1:
        row = rows[0]
        return _source(row["transcript_path"], row["session_id"], accounts,
                       row.get("model", ""), row.get("ts", 0),
                       row.get("config_dir", ""))

    slug = _claude_slug(cwd)
    scanned = []
    for account in accounts:
        if account.get("provider") != "claude":
            continue
        pattern = os.path.join(account["home"], "projects", slug, "*.jsonl")
        for path in glob.glob(pattern):
            if not _valid_uuid(os.path.splitext(os.path.basename(path))[0]):
                continue
            try:
                age = now - os.stat(path).st_mtime
            except OSError:
                continue
            if 0 <= age < MAX_SCAN_AGE:
                scanned.append((path, account, age))
    if len(scanned) != 1:
        rows = [{"session_id": os.path.splitext(os.path.basename(path))[0],
                 "ts": now - age, "model": "?"}
                for path, _, age in scanned]
        if rows:
            raise HandoffError(_ambiguity(rows, now))
        raise HandoffError(
            "no recent session matches this cwd — pass --session UUID")
    path, account, _ = scanned[0]
    session_id = os.path.splitext(os.path.basename(path))[0]
    print(f"[headroom] found session {session_id} for the current cwd",
          file=sys.stderr)
    return SourceSession(session_id, os.path.abspath(path), account)


def guard_source_stable(path, now=None, sleep=None):
    """Require a quiet transcript before copying; process hunting is brittle."""
    try:
        first = os.stat(path)
    except OSError as error:
        raise HandoffError(f"cannot stat source transcript: {error}") from error
    now = time.time() if now is None else now
    if now - first.st_mtime < 5:
        raise HandoffError(
            "source transcript changed recently — /exit the session first, "
            "wait 5 seconds, then hand off")
    (time.sleep if sleep is None else sleep)(1.0)
    try:
        second = os.stat(path)
    except OSError as error:
        raise HandoffError(f"cannot recheck source transcript: {error}") from error
    # The copy-time SHA-256 check is the authoritative anti-race backstop.
    if second.st_size > first.st_size or second.st_mtime != first.st_mtime:
        raise HandoffError(
            "source transcript is still changing — /exit the session first")


def _content_blocks(event):
    message = event.get("message") if isinstance(event, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _guard_complete_turn(events):
    last_tool_use = -1
    last_user_or_result = -1
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        blocks = _content_blocks(event)
        if event.get("type") == "assistant" and any(
                isinstance(block, dict) and block.get("type") == "tool_use"
                for block in blocks):
            last_tool_use = index
        if event.get("type") == "user" or any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in blocks):
            last_user_or_result = index
    if last_tool_use > last_user_or_result:
        raise HandoffError(
            "session stopped mid-tool-call; resume it once on the source "
            "account to finish the turn, then hand off")


def inspect_transcript(path):
    """Validate every JSONL record before deriving a content-addressed baton."""
    if os.path.islink(path):
        raise HandoffError("source transcript is a symlink — refusing to copy")
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as error:
        raise HandoffError(f"cannot read source transcript: {error}") from error
    lines = data.splitlines()
    events = []
    for index, raw in enumerate(lines):
        try:
            event = json.loads(raw.decode("utf-8", errors="replace"))
            events.append(event)
        except (ValueError, json.JSONDecodeError) as error:
            if index == len(lines) - 1:
                raise HandoffError(
                    "transcript has an incomplete final line — is the session "
                    "still writing?") from error
            raise HandoffError(
                f"transcript contains invalid JSON at line {index + 1}") from error
    _guard_complete_turn(events)
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data), "events": events,
    }


def select_target(source_slot, snapshot, requested=None):
    ranked = route.candidates("claude", snapshot)
    if requested:
        match = next(((account, reason) for account, reason in ranked
                      if account.get("name") == requested), None)
        if match is None:
            raise HandoffError(f"no configured Claude account named {requested!r}")
        account, reason = match
        if account["name"] == source_slot:
            raise HandoffError("source and target slots must be different")
        if reason is not None:
            raise HandoffError(
                f"target {requested} has no proven headroom: {reason}")
        return account
    target = next((account for account, reason in ranked
                   if reason is None and account["name"] != source_slot), None)
    if target is None:
        raise HandoffError(
            "no account has proven headroom to receive this session")
    return target


def destination_path(target_home, source_transcript, session_id):
    slug = os.path.basename(os.path.dirname(source_transcript))
    return os.path.join(target_home, "projects", slug, session_id + ".jsonl")


def stage_transcript(source, destination, expected_sha256):
    """Publish only a complete, fsynced, hash-matched private copy."""
    try:
        return _stage_transcript(source, destination, expected_sha256)
    except HandoffError:
        raise
    except OSError as error:
        raise HandoffError(f"could not stage transcript: {error}") from error


def _stage_transcript(source, destination, expected_sha256):
    if os.path.islink(source):
        raise HandoffError("source transcript is a symlink — refusing to copy")
    if os.path.exists(destination):
        raise HandoffError(
            "target already has this session id; --force does not overwrite "
            "destination collisions — inspect the previous partial handoff")
    directory = os.path.dirname(destination)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".handoff-", suffix=".tmp",
                                              dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        digest = hashlib.sha256()
        with open(source, "rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
            descriptor = None
            while True:
                chunk = incoming.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if digest.hexdigest() != expected_sha256:
            raise HandoffError("source changed during copy — handoff aborted")
        with open(temporary, "rb") as handle:
            copied = hashlib.sha256(handle.read()).hexdigest()
        if copied != expected_sha256:
            raise HandoffError("copied transcript failed SHA-256 verification")
        if os.path.exists(destination):
            raise HandoffError(
                "target already has this session id; --force does not overwrite "
                "destination collisions — inspect the previous partial handoff")
        os.rename(temporary, destination)
        temporary = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _previous_handoff(session_id, digest):
    for row in _read_jsonl(_ledger_path(), "handoff ledger"):
        if row.get("session_id") == session_id \
                and row.get("transcript_sha256") == digest:
            return row
    return None


def guard_not_duplicate(session_id, digest, force=False):
    previous = _previous_handoff(session_id, digest)
    if previous and not force:
        if not _number(previous.get("ts")) \
                or not isinstance(previous.get("target_slot"), str):
            raise HandoffError(
                f"handoff ledger is unreadable — inspect {_ledger_path()}")
        when = time.strftime("%Y-%m-%d %H:%M:%S UTC",
                             time.gmtime(previous.get("ts", 0)))
        raise HandoffError(
            f"already handed off to {previous.get('target_slot')} at {when} — "
            "re-run with --force and a different --to to create a second fork")


def append_ledger(record):
    try:
        state = paths.ensure_private(paths.state_dir())
        ledger = os.path.join(state, "handoffs.jsonl")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        descriptor = os.open(ledger, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            payload = json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
            payload = payload.encode("utf-8")
            if os.write(descriptor, payload) != len(payload):
                raise HandoffError("handoff ledger append was incomplete")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except HandoffError:
        raise
    except OSError as error:
        raise HandoffError(f"could not append handoff ledger: {error}") from error


def resume_command(target_home, session_id):
    return (f"CLAUDE_CONFIG_DIR={shlex.quote(target_home)} claude --resume "
            f"{shlex.quote(session_id)} --fork-session")


def _snapshot_rows(snapshot):
    return {row.get("name"): row for row in (snapshot or {}).get("accounts", [])
            if isinstance(row, dict) and row.get("name")}


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _email_domain(value):
    return value.rpartition("@")[2].lower() if isinstance(value, str) else ""


def _print_baton(record):
    print("BATON — conversation history staged")
    print(f"session: {record['session_id']} ({record['transcript_bytes']} bytes)")
    print(f"cwd: {record['cwd']}")
    print(f"from -> to: {record['source_slot']} -> {record['target_slot']}")
    print("does not carry: background tasks / MCP connections / permission approvals")
    print("NEXT COMMAND:")
    print(record["resume_command"])


def _parse_args(args):
    options = {"session": None, "to": None, "print": False, "force": False}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("--print", "--force"):
            options[arg[2:]] = True
        elif arg in ("--session", "--to") and index + 1 < len(args):
            index += 1
            options[arg[2:]] = args[index]
        else:
            raise HandoffError(
                "usage: headroom handoff [--session UUID] [--to SLOT] "
                "[--print] [--force]")
        index += 1
    return options


def cmd_handoff(args):
    """Stage, record, cool, and either print or exec the verified next step."""
    try:
        options = _parse_args(args)
        if not options["print"] and not sys.stdin.isatty():
            raise HandoffError(
                "non-interactive handoff requires --print; default mode needs "
                "a terminal for confirmation")
        try:
            cwd = os.path.realpath(os.getcwd())
        except OSError as error:
            raise HandoffError("current working directory no longer exists") from error
        if not os.path.isdir(cwd):
            raise HandoffError("current working directory no longer exists")
        accounts = registry.accounts()
        source = resolve_source(options["session"], accounts, cwd)
        snapshot = route.ensure_fresh_snapshot()
        target = select_target(source.account["name"], snapshot, options["to"])
        if os.path.islink(source.transcript_path):
            raise HandoffError("source transcript is a symlink — refusing to copy")
        guard_source_stable(source.transcript_path)
        inspected = inspect_transcript(source.transcript_path)
        guard_not_duplicate(source.session_id, inspected["sha256"], options["force"])
        destination = destination_path(target["home"], source.transcript_path,
                                       source.session_id)
        rows = _snapshot_rows(snapshot)
        source_row = rows.get(source.account["name"], {})
        target_row = rows.get(target["name"], {})
        source_email = source_row.get("email") or source.account.get("expected_email") or ""
        target_email = target_row.get("email") or target.get("expected_email") or ""
        if (_email_domain(source_email) and _email_domain(target_email)
                and _email_domain(source_email) != _email_domain(target_email)):
            print("warning: conversation content is moving to the other "
                  "account's data boundary")
        used = ((source_row.get("windows") or {}).get("5h") or {}).get(
            "used_percent")
        used = float(used) if _number(used) else None
        command = resume_command(target["home"], source.session_id)
        record = {
            "schema": SCHEMA, "ts": int(time.time()),
            "session_id": source.session_id,
            "source_slot": source.account["name"],
            "source_email_redacted": collect.redact_email(source_email),
            "target_slot": target["name"], "cwd": cwd,
            "transcript_sha256": inspected["sha256"],
            "transcript_bytes": inspected["bytes"],
            "source_5h_used": used,
            "reason": "capped" if used is not None and used >= 99 else "manual",
            "resume_command": command,
        }
        stage_transcript(source.transcript_path, destination, inspected["sha256"])
        try:
            append_ledger(record)
        except Exception:
            os.unlink(destination)
            raise
        try:
            reset = route.window_reset(snapshot, source.account["name"], "5h")
            reset = reset if _number(reset) else None
            route.mark(source.account["name"], "claude", reset,
                       account_wide=True, window="5h")
        except Exception as error:  # noqa: BLE001 — the verified copy remains valid
            print(f"warning: could not cool source slot: {error}", file=sys.stderr)
        _print_baton(record)
        if options["print"]:
            return 0
        answer = input(f"hand off to {target['name']}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("handoff staged; resume command not run")
            print(command)
            return 0
        environment = collect.scrubbed_env()
        environment["CLAUDE_CONFIG_DIR"] = target["home"]
        try:
            os.execvpe("claude", ["claude", "--resume", source.session_id,
                                   "--fork-session"], environment)
        except OSError as error:
            print(f"headroom: cannot exec claude: {error}", file=sys.stderr)
            return 127
    except HandoffError as error:
        print(f"headroom: {error}", file=sys.stderr)
        return 2
