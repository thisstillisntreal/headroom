"""Resident, fail-closed Claude auto-handoff supervisor.

One 250 ms loop owns hook ingestion and child lifecycle.  Hook evidence never
terminates a child by itself: it must be bound to the current child, match a
narrow subscription-cap phrase, and be corroborated by a fresh identity-bound
usage collect before every remaining pre-stop check succeeds.
"""
import fcntl
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import termios
import time
import uuid
from dataclasses import dataclass

from . import collect, handoff, paths, registry, route

POLL_SECONDS = 0.25
BIND_TIMEOUT = 30.0
TERM_TIMEOUT = 10.0
QUIET_SECONDS = 5.0
LOOP_WINDOW = 10 * 60
LOOP_MAX = 3
MAX_HOOK_BYTES = 1024 * 1024

CAP_RE = re.compile(
    r"\b(?:(?:you(?:'|’)ve\s+)?hit your "
    r"(?:session|weekly|usage) limit|usage limit reached)\b", re.I)

HOOK_EVENTS = {"SessionStart", "StopFailure", "CwdChanged", "SessionEnd"}
INCOMPATIBLE_FLAGS = {
    "--bare", "--safe-mode", "--disable-all-hooks", "--print", "-p",
    "--output-format", "--input-format", "--no-session-persistence",
}


class SupervisorError(RuntimeError):
    """A fail-closed supervisor refusal."""


class PermanentSupervisorError(SupervisorError):
    """A child-local condition that cannot become safe on a later hook."""


@dataclass(frozen=True)
class Binding:
    session_id: str
    transcript_path: str
    cwd: str
    model: str
    version: str
    config_dir: str


@dataclass(frozen=True)
class CapProof:
    event: dict
    message: str
    snapshot: dict
    scope: dict
    family: str


@dataclass
class Child:
    process: subprocess.Popen
    account: dict
    generation: int
    event_path: str
    settings_path: str
    launched_at: float
    automation: bool
    binding: Binding = None
    session_ended: bool = False
    event_offset: int = 0
    hint_printed: bool = False


@dataclass(frozen=True)
class Relaunch:
    account: dict
    argv: list
    cwd: str
    automatic: bool
    handoff_id: str = ""


def _supervisors_dir():
    return os.path.join(paths.state_dir(), "supervisors")


def event_path(supervisor_id):
    return os.path.join(_supervisors_dir(), supervisor_id + ".jsonl")


def _model_name(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("display_name", "displayName", "name", "model"):
            if isinstance(value.get(key), str):
                return value[key]
    return ""


def _hook_executable():
    override = os.environ.get("HEADROOM_EXECUTABLE")
    if override:
        return override
    installed = shutil.which("headroom")
    if installed:
        return installed
    return os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "bin", "headroom")


def _hook_command(matcher=""):
    command = shlex.quote(_hook_executable()) + " _hook-event"
    if matcher:
        command = "HEADROOM_HOOK_MATCHER=" + shlex.quote(matcher) + " " + command
    return command


def hook_settings():
    normal = {"type": "command", "command": _hook_command()}
    limited = {"type": "command", "command": _hook_command("rate_limit")}
    return {"hooks": {
        "SessionStart": [{"hooks": [normal]}],
        "StopFailure": [{"matcher": "rate_limit", "hooks": [limited]}],
        "CwdChanged": [{"hooks": [normal]}],
        "SessionEnd": [{"hooks": [normal]}],
    }}


def write_hook_event(stream=None, environ=None, now=None):
    """Hidden hook adapter: validate an envelope and append one private row."""
    stream = sys.stdin if stream is None else stream
    environ = os.environ if environ is None else environ
    try:
        raw = stream.read(MAX_HOOK_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_HOOK_BYTES:
            raise SupervisorError("hook payload too large")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise SupervisorError("hook payload must be an object")
        hook_name = payload.get("hook_event_name")
        if hook_name not in HOOK_EVENTS:
            raise SupervisorError("unknown hook event")
        supervisor_id = environ.get("HEADROOM_SUPERVISOR_ID", "")
        if not handoff._valid_uuid(supervisor_id):
            raise SupervisorError("invalid supervisor id")
        generation_raw = environ.get("HEADROOM_CHILD_GENERATION", "")
        if not generation_raw.isdigit():
            raise SupervisorError("invalid child generation")
        slot = environ.get("HEADROOM_SOURCE_SLOT", "")
        if not registry.NAME_RE.fullmatch(slot):
            raise SupervisorError("invalid source slot")
        config_dir = environ.get("CLAUDE_CONFIG_DIR", "")
        if not config_dir:
            raise SupervisorError("missing Claude config home")
        record = {
            "schema": "headroom_hook_event@1",
            "received_at": time.time() if now is None else float(now),
            "supervisor_id": supervisor_id,
            "generation": int(generation_raw),
            "source_slot": slot,
            "config_dir": registry.expand(config_dir),
            "matcher": environ.get("HEADROOM_HOOK_MATCHER", ""),
            "payload": payload,
        }
        directory = paths.ensure_private(_supervisors_dir())
        destination = os.path.join(directory, supervisor_id + ".jsonl")
        descriptor = os.open(destination,
                             os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            encoded = (json.dumps(record, separators=(",", ":"),
                                  allow_nan=False) + "\n").encode("utf-8")
            if os.write(descriptor, encoded) != len(encoded):
                raise SupervisorError("hook event append was incomplete")
            os.fsync(descriptor)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        return 0
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError,
            SupervisorError) as error:
        print(f"headroom: hook event refused: {error}", file=sys.stderr)
        return 2


def incompatible_args(args):
    for arg in args:
        if arg == "--settings" or arg.startswith("--settings="):
            return "user-supplied --settings"
        if arg in INCOMPATIBLE_FLAGS:
            return arg
    return ""


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _event_text(event):
    if not isinstance(event, dict):
        return ""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    texts = []
    for item in content if isinstance(content, list) else []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            texts.append(item["text"])
    if texts:
        return "\n".join(texts)
    return "\n".join(_strings(event.get("text")))


def _last_transcript_cap(path):
    try:
        with open(path, "rb") as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
        if not lines:
            return ""
        event = json.loads(lines[-1].decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return ""
    is_api = event.get("isApiErrorMessage") is True
    if not is_api and isinstance(event.get("message"), dict):
        is_api = event["message"].get("isApiErrorMessage") is True
    text = _event_text(event)
    return text if is_api and CAP_RE.search(text) else ""


def _record_matches(record, child, binding=None):
    if not isinstance(record, dict):
        return False
    expected_id = os.path.splitext(os.path.basename(child.event_path))[0]
    if record.get("supervisor_id") != expected_id:
        return False
    if record.get("generation") != child.generation \
            or record.get("source_slot") != child.account.get("name"):
        return False
    if registry.expand(record.get("config_dir", "/")) \
            != registry.expand(child.account["home"]):
        return False
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return False
    if binding is not None:
        if payload.get("session_id") != binding.session_id:
            return False
        transcript = payload.get("transcript_path")
        if transcript is not None and os.path.realpath(transcript) \
                != binding.transcript_path:
            return False
    return True


def parse_session_start(record, child):
    if not _record_matches(record, child):
        raise SupervisorError("SessionStart identity does not match this child")
    payload = record["payload"]
    if payload.get("hook_event_name") != "SessionStart":
        raise SupervisorError("not a SessionStart event")
    session_id = payload.get("session_id")
    transcript = payload.get("transcript_path")
    cwd = payload.get("cwd")
    if not isinstance(session_id, str) or not handoff._valid_uuid(session_id):
        raise SupervisorError("SessionStart has no valid session id")
    if not isinstance(transcript, str) or not transcript:
        raise SupervisorError("SessionStart has no transcript path")
    try:
        source = handoff._source(transcript, session_id, [child.account],
                                 config_dir=record["config_dir"])
    except handoff.HandoffError as error:
        raise SupervisorError(str(error)) from error
    if not isinstance(cwd, str) or not os.path.isdir(os.path.realpath(cwd)):
        raise SupervisorError("SessionStart cwd is missing or unreadable")
    return Binding(
        session_id, source.transcript_path, os.path.realpath(cwd),
        _model_name(payload.get("model")),
        payload.get("version", "") if isinstance(payload.get("version"), str)
        else "", record["config_dir"])


def cap_message(record, child):
    """Return the narrow cap message, or empty when any binding proof fails."""
    binding = child.binding
    if binding is None or not _record_matches(record, child, binding):
        return ""
    payload = record["payload"]
    if payload.get("hook_event_name") != "StopFailure":
        return ""
    if record.get("matcher") != "rate_limit":
        return ""
    error_type = payload.get("error") or payload.get("error_type")
    if error_type is not None and error_type != "rate_limit":
        return ""
    direct = payload.get("last_assistant_message")
    if direct is None:
        direct = payload.get("error_details")
    if direct is not None:
        text = "\n".join(_strings(direct))
        return text if CAP_RE.search(text) else ""
    return _last_transcript_cap(binding.transcript_path)


def _read_events(child):
    if not os.path.exists(child.event_path):
        return []
    try:
        with open(child.event_path, "rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            handle.seek(child.event_offset)
            data = handle.read()
            fcntl.flock(handle, fcntl.LOCK_UN)
        if not data:
            return []
        if not data.endswith(b"\n"):
            raise SupervisorError("hook event file has an incomplete record")
        events = []
        for line in data.splitlines():
            record = json.loads(line.decode("utf-8"))
            received = record.get("received_at") if isinstance(record, dict) else None
            payload = record.get("payload") if isinstance(record, dict) else None
            if (not isinstance(record, dict)
                    or record.get("schema") != "headroom_hook_event@1"
                    or not handoff._valid_uuid(record.get("supervisor_id"))
                    or not isinstance(record.get("generation"), int)
                    or isinstance(record.get("generation"), bool)
                    or not isinstance(record.get("source_slot"), str)
                    or not isinstance(record.get("config_dir"), str)
                    or not isinstance(record.get("matcher"), str)
                    or not isinstance(received, (int, float))
                    or isinstance(received, bool) or not math.isfinite(received)
                    or not isinstance(payload, dict)
                    or payload.get("hook_event_name") not in HOOK_EVENTS):
                raise ValueError
            events.append(record)
        child.event_offset += len(data)
        return events
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise SupervisorError("hook event file is unreadable") from error


def _source_row_is_bound(account, family, snapshot, collect_started):
    if not isinstance(snapshot, dict):
        return "collect returned no snapshot"
    started = snapshot.get("run_started")
    generated = snapshot.get("generated")
    floor = int(collect_started)
    if not isinstance(started, (int, float)) or isinstance(started, bool) \
            or started < floor:
        return "collect did not start after the cap event"
    if not isinstance(generated, (int, float)) or isinstance(generated, bool) \
            or generated < floor:
        return "collect did not finish after the cap event"
    row = next((item for item in snapshot.get("accounts", [])
                if isinstance(item, dict) and item.get("name") == account["name"]),
               None)
    reason = route.block_reason(account, family, row, {}, time.time(), reserve=0)
    capacity_reasons = {"5h at 100%", "7d at 100%",
                        f"{family} weekly cap at 100%",
                        "5h critical", "7d critical"}
    if reason is not None and reason not in capacity_reasons:
        return reason
    captured = row.get("captured_at") if isinstance(row, dict) else None
    if not isinstance(captured, (int, float)) or isinstance(captured, bool) \
            or captured < floor:
        return "source observation predates the cap event"
    return ""


class _SignalGuard:
    def __init__(self):
        self.original = {}
        self.shutdown_signal = None
        self.polls = 0
        self.forwarded = False

    def _shutdown(self, signum, _frame):
        if self.shutdown_signal is None:
            self.shutdown_signal = signum
            self.polls = 0

    def install(self):
        for signum in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            self.original[signum] = signal.getsignal(signum)
        signal.signal(signal.SIGINT, lambda _s, _f: None)
        signal.signal(signal.SIGHUP, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def poll(self, process):
        if self.shutdown_signal is None or process.poll() is not None:
            return
        self.polls += 1
        if self.polls >= 2 and not self.forwarded:
            try:
                os.kill(process.pid, self.shutdown_signal)
            except ProcessLookupError:
                pass
            self.forwarded = True

    def restore(self):
        for signum, handler in self.original.items():
            signal.signal(signum, handler)


class Supervisor:
    def __init__(self, family, args, account, *, collect_fn=None,
                 popen=None, now=None, sleep=None, supervisor_id=None):
        self.family = family
        self.initial_args = list(args)
        self.account = account
        self.collect_fn = collect.run_collect if collect_fn is None else collect_fn
        self.popen = subprocess.Popen if popen is None else popen
        self.now = time.time if now is None else now
        self.sleep = time.sleep if sleep is None else sleep
        self.supervisor_id = supervisor_id or str(uuid.uuid4())
        self.generation = 0
        self.settings_files = []

    def _settings_file(self, generation):
        directory = paths.ensure_private(_supervisors_dir())
        filename = f"{self.supervisor_id}-{generation}.settings.json"
        destination = os.path.join(directory, filename)
        paths.write_json_atomic(destination, hook_settings(), mode=0o600)
        self.settings_files.append(destination)
        return destination

    def _environment(self, account, generation, automatic):
        environment = collect.scrubbed_env()
        environment["CLAUDE_CONFIG_DIR"] = account["home"]
        if automatic:
            environment.update({
                "HEADROOM_SUPERVISOR_ID": self.supervisor_id,
                "HEADROOM_CHILD_GENERATION": str(generation),
                "HEADROOM_SOURCE_SLOT": account["name"],
            })
        else:
            for key in ("HEADROOM_SUPERVISOR_ID", "HEADROOM_CHILD_GENERATION",
                        "HEADROOM_SOURCE_SLOT", "HEADROOM_HOOK_MATCHER"):
                environment.pop(key, None)
        return environment

    def _spawn(self, account, args, cwd, automatic):
        self.generation += 1
        settings = self._settings_file(self.generation) if automatic else ""
        argv = ["claude"]
        if settings:
            argv.extend(["--settings", settings])
        argv.extend(args)
        environment = self._environment(account, self.generation, automatic)
        try:
            process = self.popen(argv, env=environment, cwd=cwd)
        except OSError as error:
            raise SupervisorError(f"cannot start Claude: {error}") from error
        return Child(process, account, self.generation,
                     event_path(self.supervisor_id), settings, self.now(), automatic)

    def _fresh_collect(self, event_time):
        # Provider snapshots use whole-second timestamps.  Crossing the next
        # second before starting removes the historical same-second ambiguity.
        boundary = math.floor(event_time) + 1
        while self.now() < boundary:
            self.sleep(min(POLL_SECONDS, boundary - self.now()))
        started = self.now()
        try:
            snapshot = self.collect_fn(quiet=True)
        except TypeError:
            snapshot = self.collect_fn()
        except Exception as error:  # noqa: BLE001 — a failed proof never stops
            raise SupervisorError(f"fresh usage collect failed: {error}") from error
        return snapshot, started

    def _prove_cap(self, child, record):
        message = cap_message(record, child)
        if not message:
            return None
        try:
            source = handoff.SourceSession(
                child.binding.session_id, child.binding.transcript_path,
                child.account, child.binding.model)
            family = handoff.resolve_model_family(source)
            if family == "claude":
                raise handoff.HandoffError(
                    "automatic handoff requires the actual model family")
            snapshot, started = self._fresh_collect(record["received_at"])
            reason = _source_row_is_bound(child.account, family, snapshot, started)
            if reason:
                raise SupervisorError(reason)
            scope = route.cap_scope(snapshot, child.account["name"], family,
                                    message)
            if scope is None:
                raise SupervisorError(
                    "fresh usage is below 99% or the cap scope is ambiguous")
            reset = scope.get("reset")
            if not isinstance(reset, (int, float)) or isinstance(reset, bool) \
                    or not math.isfinite(reset) or reset <= self.now():
                raise SupervisorError("fresh cap reset is missing or ambiguous")
            return CapProof(record, message, snapshot, scope, family)
        except (handoff.HandoffError, registry.RegistryError) as error:
            if "model" in str(error).lower():
                raise PermanentSupervisorError(str(error)) from error
            raise SupervisorError(str(error)) from error

    def _loop_guard(self):
        rows = handoff._read_jsonl(handoff._ledger_path(), "handoff ledger")
        cutoff = self.now() - LOOP_WINDOW
        count = sum(1 for row in rows if row.get("automatic") is True
                    and row.get("action") == "cap_confirmed"
                    and isinstance(row.get("ts"), (int, float))
                    and not isinstance(row.get("ts"), bool)
                    and row["ts"] >= cutoff)
        if count >= LOOP_MAX:
            raise SupervisorError(
                "automatic handoff loop guard: 3 handoffs in 10 minutes")

    def _preflight(self, child, proof):
        binding = child.binding
        self._loop_guard()
        target = handoff.select_target(child.account["name"], proof.snapshot,
                                       proof.family)
        handoff.guard_source_stable(binding.transcript_path, now=self.now(),
                                    sleep=lambda _seconds: None)
        source = handoff.SourceSession(
            binding.session_id, binding.transcript_path, child.account,
            binding.model, int(self.now()))
        plan = handoff.plan_handoff(
            source, proof.family, target, proof.snapshot, proof.scope,
            binding.cwd, automatic=True, child_generation=child.generation)
        # Final account/credential/cooldown check immediately before signaling.
        handoff.select_target(child.account["name"], proof.snapshot,
                              proof.family, requested=target["name"])
        return plan

    @staticmethod
    def _save_terminal():
        try:
            if sys.stdin.isatty():
                return termios.tcgetattr(sys.stdin.fileno())
        except (OSError, termios.error):
            pass
        return None

    @staticmethod
    def _restore_terminal(saved):
        if saved is None:
            return
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
        except (OSError, termios.error):
            pass

    def _wait_stopped(self, child):
        deadline = self.now() + TERM_TIMEOUT
        while child.process.poll() is None and self.now() < deadline:
            try:
                for record in _read_events(child):
                    if _record_matches(record, child, child.binding) \
                            and record["payload"].get("hook_event_name") \
                            == "SessionEnd":
                        child.session_ended = True
            except SupervisorError:
                pass
            self.sleep(POLL_SECONDS)
        returncode = child.process.poll()
        try:
            for record in _read_events(child):
                if _record_matches(record, child, child.binding) \
                        and record["payload"].get("hook_event_name") \
                        == "SessionEnd":
                    child.session_ended = True
        except SupervisorError:
            pass
        return returncode

    def _post_stop_valid(self, plan):
        try:
            handoff.guard_source_stable(plan.source.transcript_path,
                                        now=self.now(), sleep=self.sleep)
            inspected = handoff.inspect_transcript(plan.source.transcript_path,
                                                   allow_dangling=True)
            return (inspected["sha256"] == plan.inspected["sha256"]
                    and inspected["bytes"] == plan.inspected["bytes"])
        except handoff.HandoffError:
            return False

    def _failure(self, plan, reason):
        try:
            handoff.append_action(
                plan.handoff_id, "failure", automatic=True,
                source_slot=plan.source.account["name"],
                target_slot=plan.target["name"], reason=reason,
                old_session_id=plan.source.session_id,
                child_generation=plan.child_generation)
        except handoff.HandoffError:
            pass

    def _stop_and_commit(self, child, plan):
        print(f"[headroom] cap confirmed; {plan.source.account['name']} -> "
              f"{plan.target['name']}", file=sys.stderr)
        try:
            handoff.append_action(
                plan.handoff_id, "cap_confirmed", automatic=True,
                source_slot=plan.source.account["name"],
                target_slot=plan.target["name"],
                old_session_id=plan.source.session_id,
                actual_model_family=plan.family,
                cap_scope=plan.cap_proof.get("key"),
                cap_used_percent=plan.cap_proof.get("used_percent"),
                cap_reset=plan.cap_proof.get("reset"),
                child_generation=plan.child_generation)
        except handoff.HandoffError as error:
            raise SupervisorError(f"cannot ledger cap proof: {error}") from error
        saved = self._save_terminal()
        ledger_error = None
        try:
            os.kill(child.process.pid, signal.SIGTERM)
            try:
                handoff.append_action(
                    plan.handoff_id, "stop_sent", automatic=True,
                    source_slot=plan.source.account["name"],
                    old_session_id=plan.source.session_id,
                    child_generation=plan.child_generation)
            except handoff.HandoffError as error:
                ledger_error = error
            returncode = self._wait_stopped(child)
        except OSError as error:
            self._failure(plan, "stop_failed: " + str(error))
            child.automation = False
            return None
        finally:
            self._restore_terminal(saved)
        if returncode is None:
            self._failure(plan, "sigterm_timeout")
            print("[headroom] Claude did not exit after one SIGTERM; automatic "
                  "handoff disabled for this child", file=sys.stderr)
            child.automation = False
            return None
        if ledger_error is not None:
            self._failure(plan, "stop_ledger_failed: " + str(ledger_error))
            print("[headroom] stop ledger failed after Claude exited; "
                  "relaunching the source with automation off", file=sys.stderr)
            return Relaunch(plan.source.account,
                            ["--resume", plan.source.session_id], plan.cwd,
                            False)
        try:
            handoff.append_action(
                plan.handoff_id, "stopped", automatic=True,
                source_slot=plan.source.account["name"],
                old_session_id=plan.source.session_id,
                child_generation=plan.child_generation,
                child_exit_code=returncode,
                session_end=child.session_ended)
        except handoff.HandoffError:
            pass
        if not child.session_ended:
            self._failure(plan, "missing_session_end")
            print("[headroom] SessionEnd proof is missing; relaunching the "
                  "source with automation off", file=sys.stderr)
            return Relaunch(plan.source.account,
                            ["--resume", plan.source.session_id], plan.cwd,
                            False)
        if not self._post_stop_valid(plan):
            self._failure(plan, "post_stop_transcript_validation_failed")
            print("[headroom] final transcript validation failed; relaunching "
                  "the source with automation off", file=sys.stderr)
            return Relaunch(plan.source.account,
                            ["--resume", plan.source.session_id], plan.cwd,
                            False)
        try:
            result = handoff.commit_handoff(plan)
        except handoff.HandoffError as error:
            self._failure(plan, "commit_failed: " + str(error))
            print(f"[headroom] handoff commit failed ({error}); relaunching the "
                  "source with automation off", file=sys.stderr)
            return Relaunch(plan.source.account,
                            ["--resume", plan.source.session_id], plan.cwd,
                            False)
        if plan.inspected["unresolved_tool_ids"]:
            print("[headroom] note: the interrupted tool call may re-run on resume",
                  file=sys.stderr)
        return Relaunch(plan.target, handoff.resume_argv(result)[1:], plan.cwd,
                        True, plan.handoff_id)

    def _handle_events(self, child, pending_handoff_id):
        proof = None
        try:
            records = _read_events(child)
        except SupervisorError as error:
            print(f"[headroom] {error}; automatic handoff disabled for this child",
                  file=sys.stderr)
            child.automation = False
            return None
        for record in records:
            if not _record_matches(record, child):
                continue
            payload = record["payload"]
            hook_name = payload.get("hook_event_name")
            if hook_name == "SessionStart" and child.binding is None:
                try:
                    child.binding = parse_session_start(record, child)
                    if pending_handoff_id:
                        handoff.append_action(
                            pending_handoff_id, "resume_bound", automatic=True,
                            source_slot=child.account["name"],
                            new_session_id=child.binding.session_id,
                            transcript_path=child.binding.transcript_path,
                            child_generation=child.generation)
                except (SupervisorError, handoff.HandoffError) as error:
                    print(f"[headroom] {error}; automatic handoff disabled for "
                          "this child", file=sys.stderr)
                    child.automation = False
            elif child.binding and not _record_matches(record, child, child.binding):
                continue
            elif hook_name == "CwdChanged" and child.binding:
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and os.path.isdir(os.path.realpath(cwd)):
                    child.binding = Binding(
                        child.binding.session_id, child.binding.transcript_path,
                        os.path.realpath(cwd), child.binding.model,
                        child.binding.version, child.binding.config_dir)
            elif hook_name == "SessionEnd" and child.binding:
                child.session_ended = True
            elif hook_name == "StopFailure" and child.binding \
                    and child.automation:
                try:
                    proof = self._prove_cap(child, record)
                    if proof is None:
                        print("[headroom] rate-limit hook was not a subscription "
                              "cap; child continues", file=sys.stderr)
                except PermanentSupervisorError as error:
                    child.automation = False
                    print(f"[headroom] cap not corroborated ({error}); automatic "
                          "handoff disabled for this child", file=sys.stderr)
                except SupervisorError as error:
                    print(f"[headroom] cap not corroborated ({error}); child "
                          "continues", file=sys.stderr)
        return proof

    def _monitor(self, child, pending_handoff_id=""):
        signals = _SignalGuard()
        signals.install()
        proof = None
        try:
            while True:
                signals.poll(child.process)
                if signals.shutdown_signal is not None:
                    child.automation = False
                candidate = self._handle_events(child, pending_handoff_id)
                if candidate is not None:
                    proof = candidate
                returncode = child.process.poll()
                if returncode is not None:
                    return returncode
                if child.automation and child.binding is None \
                        and self.now() - child.launched_at >= BIND_TIMEOUT:
                    if not child.hint_printed:
                        print("[headroom] no SessionStart handshake within 30s; "
                              "automatic handoff disabled for this child",
                              file=sys.stderr)
                        child.hint_printed = True
                    child.automation = False
                if proof is not None and child.automation:
                    try:
                        plan = self._preflight(child, proof)
                    except handoff.HandoffError as error:
                        # A recent mtime is expected just after StopFailure; keep
                        # polling until the required five quiet seconds pass.
                        if "changed recently" not in str(error):
                            reset = route.earliest_reset(
                                proof.snapshot, proof.family)
                            hint = f"; earliest reset {route.tfmt(reset)}" \
                                if reset else ""
                            print(f"[headroom] automatic handoff held: {error}{hint}; "
                                  "child continues", file=sys.stderr)
                            proof = None
                    except SupervisorError as error:
                        print(f"[headroom] automatic handoff held: {error}; child "
                              "continues", file=sys.stderr)
                        proof = None
                    else:
                        relaunch = self._stop_and_commit(child, plan)
                        if relaunch is not None:
                            return relaunch
                        proof = None
                self.sleep(POLL_SECONDS)
        finally:
            signals.restore()

    def run(self):
        account = self.account
        args = self.initial_args
        cwd = os.path.realpath(os.getcwd())
        automatic = True
        pending_handoff_id = ""
        last_exit = 0
        while True:
            try:
                child = self._spawn(account, args, cwd, automatic)
            except SupervisorError as error:
                print(f"headroom: {error}", file=sys.stderr)
                return 127
            if pending_handoff_id:
                try:
                    handoff.append_action(
                        pending_handoff_id, "resume_spawned", automatic=True,
                        target_slot=account["name"],
                        old_session_id=args[1] if len(args) > 1 else "",
                        child_generation=child.generation)
                except handoff.HandoffError as error:
                    print(f"[headroom] could not ledger resume spawn: {error}; "
                          "automatic handoff disabled", file=sys.stderr)
                    child.automation = False
                    automatic = False
            outcome = self._monitor(child, pending_handoff_id)
            if isinstance(outcome, Relaunch):
                account, args, cwd = outcome.account, outcome.argv, outcome.cwd
                automatic = outcome.automatic
                pending_handoff_id = outcome.handoff_id
                continue
            last_exit = int(outcome)
            return last_exit


def _initial_account(family):
    snapshot = route.ensure_fresh_snapshot()
    if snapshot is None:
        return None
    account = next((candidate for candidate, reason in route.candidates(
        family, snapshot) if reason is None), None)
    if account is None:
        return None
    rows = route._snapshot_accounts(snapshot)
    reason = route.block_reason(account, family, rows.get(account["name"]),
                                route.cooldowns(), time.time())
    return account if reason is None else None


def cmd_claude(family, args):
    account = _initial_account(family)
    if account is None:
        print(f"[headroom] no account for '{family}' has proven headroom; "
              f"try `headroom status {family}`", file=sys.stderr)
        return 2
    print(f"[headroom] {family} -> {account['name']} ({account['home']})",
          file=sys.stderr)
    return Supervisor(family, args, account).run()
