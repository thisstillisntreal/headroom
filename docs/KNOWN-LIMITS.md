# Known limits and design tradeoffs

Findings from an adversarial cross-model review (GPT-5.6, x-high effort,
2026-07-11) that are deliberate tradeoffs or blocked on upstream, documented
here so users can judge them for their own threat model.

## Claude usage binding is trust-on-first-use

The Anthropic usage endpoint identifies its organization in a response
header, but a login's *default* org (from `claude auth status`) can
legitimately differ from its *usage* org (multi-org accounts). headroom
therefore pins the usage-org fingerprint per slot on the first successful
read and holds the slot if it ever changes. The first read itself is
unpinned — if an attacker controls your config home *before* first use, TOFU
cannot detect it (they could also just take the credentials). Run
`headroom collect` once right after connecting to close the window.

## Codex reads need a Codex CLI with the app-server

Codex usage is read live from `codex app-server`
(`account/rateLimits/read` + `account/read`), which requires a reasonably
recent Codex CLI. On an older Codex without the app-server, headroom falls
back to a best-effort read of the CLI's on-disk `rate_limits` session
telemetry — which is only current while you're actively using that account
and is held by the router (shown Idle/Waiting on the dashboard) until a fresh
reading appears. Set `HEADROOM_CODEX_ROUTING=0` to force Codex dashboard-only.

## A project's own CLI settings can override the selected provider

headroom scrubs provider-override environment variables before launching a
CLI, but Claude Code and Codex also read their OWN config after startup — a
project `.claude/settings.json` with an `env` block or `apiKeyHelper`, or a
Codex `config.toml` custom provider, is applied by the CLI itself and can send
your session to a different provider/account than the slot headroom selected.
headroom can't override that from outside. If you use alternate-provider
settings (Bedrock/Vertex/custom gateways), headroom's account routing does not
apply to those sessions — use headroom only with direct OAuth/subscription
logins.

## The Codex fallback path (only when the app-server is unavailable)

The primary Codex read is the live app-server call above. If that fails (an
older Codex CLI), headroom falls back to the CLI's on-disk `rate_limits`
session telemetry, which is best-effort:

- an account you're actively using shows **Live**;
- a quiet account shows **Idle — last seen Nh ago** (held by the router);
- an account that has never run Codex shows **Waiting — run Codex once**;
- a rate-limited account shows **Limited — resets …**.

Upstream gaps that make the fallback best-effort: session logs don't reliably
identity-stamp which user a `rate_limits` event belongs to (openai/codex#16323)
and some versions emit `rate_limits: null` (openai/codex#14880). The live
app-server read has none of these problems — it returns identity-bound,
real-time data — so keeping your Codex CLI current is the way to get
first-class Codex tracking.

## `verified_local` identities are routable

When the network or provider CLI is unavailable, identity falls back to
local credential metadata and is labeled `verified_local` (visible in the
snapshot and on the dashboard). This keeps offline/air-gapped setups usable.
If you want provider-verified-only routing, treat `verified_local` as held —
open an issue if you want this as a config flag.

## macOS Keychain (Claude) — read directly, with one caveat

On recent Claude Code for **macOS**, the OAuth token is stored in the login
**Keychain**, not in `~/.claude/.credentials.json`. Importantly, setting
`CLAUDE_CONFIG_DIR` does **not** move it to a file on macOS the way it does on
Linux/Windows — the credential stays in a single, system-wide Keychain item
shared across logins.

headroom reads that item directly (via the `security` CLI, item name
`Claude Code-credentials`) whenever the file is absent, so a normal macOS
Claude login is tracked with no extra steps. If your Keychain is locked, macOS
will prompt to allow access the first time; approve it (choose *Always Allow*
to avoid repeat prompts). Override the item name with the
`HEADROOM_CLAUDE_KEYCHAIN_SERVICE` environment variable if a future CLI version
changes it.

- **The caveat: one Keychain item = one Claude account on macOS.** Because the
  item is shared across all logins, you can't isolate *multiple* Claude
  accounts on one Mac by config directory — a second `claude` login overwrites
  the first. For multi-account rotation on macOS, run the extra accounts on a
  Linux host/server (file-based, fully isolated) or keep them in Codex; a
  single Mac Claude account tracks and routes fine.
- **Codex `cli_auth_credentials_store = "keyring"`** and other non-file stores
  are likewise invisible; such slots show as not logged in.

## Scoped model caps aren't enforced on the generic `claude` route

`headroom claude` routes on the account-wide 5h/7d windows — it can't know
which model the Claude CLI will actually use, so it does NOT hold an account
just because one model's weekly cap (e.g. Opus) is exhausted (that would
wrongly block Sonnet/Haiku work on the same account). To gate on a specific
model's cap, name it: `headroom claude --model opus` holds when the Opus
weekly cap is full.

## `headroom run` retries are for idempotent commands

Rotation replays the whole command on the next account when a run *fails*
with a provider-limit error on stderr. If your command has side effects
before the limit hits, those side effects happen once per attempt. Use
`headroom claude`/`env`/`pick` for non-idempotent work.

## The local dashboard is plain HTTP on 127.0.0.1

`headroom serve` binds loopback only AND validates the `Host` header — a
non-loopback Host is rejected with 403, so a remote page can't reach it via
DNS-rebinding. What it does NOT have is authentication: any process on the
same machine using a normal loopback Host can read the served feed (the
sanitized public snapshot — emails redacted by default). For anything shared
or multi-user, put the static build behind your own web server and auth.
