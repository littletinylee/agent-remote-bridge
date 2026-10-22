# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning: SemVer.

## [Unreleased]

### Pending verification (needs the owner's phone)
- W3 only: a push after ≥1 h of no WeChat interaction. Everything else in the
  acceptance list is now measured (see `docs/VERIFICATION.md`).

## [0.4.0] - 2026-10-22

### Added
- `arb service install|uninstall|status`: logon autostart without admin rights
  (Startup-folder hidden launcher; verified by restarting from it).
- `scripts/verify.py`: non-mutating snapshot (python/config/sqlite/daemon/
  channel/endpoint/tests) as text, markdown or JSON.
- `scripts/report_util.py` + `scripts/sanitize_reports.py`: strip WeChat ids,
  bot tokens, API keys and user paths out of committed verification artifacts;
  CI enforces it.
- Stress tests: 20 questions over 10 sessions answered in shuffled order, and
  8 concurrent approvals of one question (exactly one wins).

### Fixed
- Daemon no longer runs silently without a channel after `errcode -14`: it stays
  visibly unhealthy and retries every 30s; re-running `arb login` heals the
  running daemon because credentials are reloaded on each attempt.
- `IlinkError(retryable=...)` raised TypeError inside the exception handler,
  masking real network errors.
- Empty `api_token` authenticated requests that omitted the header; now 401 and
  compared in constant time.
- Pairing code rotates after a successful bind (replay defence).
- `wait_for` busy-looped during shutdown; HTTP probes no longer log tracebacks.
- Notify dedupe hashed only the title, dropping distinct messages that shared
  one.
- `arb ask` now distinguishes "waiter gave up, question still live" (exit 3).

## [0.3.0] - 2026-10-22

### Added
- Verified end-to-end on a real personal phone: proactive push (cold, no
  `context_token`), inbound reply driving the origin agent session, and the
  dangerous-operation gate (a bare `1` was refused, `yes` approved, then Claude
  Code executed the command and wrote its file).
- `scripts/e2e_wechat_agent.py` (real Claude Code + real WeChat approval).
- `arb approval on|off|status`.

## [0.2.0] - 2026-10-22

### Added
- Real-agent end-to-end harnesses: `scripts/e2e_claude.py`, `scripts/e2e_codex.py`,
  `scripts/e2e_concurrent.py`, with recorded reports under `docs/verification/`.
- Multi-agent concurrency acceptance: Claude Code and Codex running simultaneously while a third
  agent blocks on a WeChat decision; two concurrent questions answered by `#CODE` with no crosstalk.
- Docs: architecture, agent recipes, security model, limitations, deploy/ops, verification record.
- CI workflow (Windows + Linux, Python 3.9/3.12/3.14).

### Fixed
- `arb hook` now accepts the event payload from **argv** (Codex `notify` passes it there) as well as
  stdin (Claude Code). Reading argv first also avoids blocking on an inherited stdin.
- `config.json` / `ilink.json` tolerate a UTF-8 BOM (Notepad / PowerShell 5 write one).
- `arb login` keeps refreshing the QR for the caller's whole window instead of giving up after 5.
- `GET /api/answer` callers are treated as authenticated local actors rather than untrusted WeChat
  senders.
- The loopback server no longer sets `SO_REUSEADDR`, so a second daemon fails loudly instead of
  silently stealing the port on Windows.
- Inbound dedupe moved into the daemon so every channel benefits (previously iLink-only).

### Changed
- `arb login` gained `--qr-png` and `--no-terminal-qr` for GUI/chat-based login.

## [0.1.0] - 2026-10-22

### Added
- WeChat iLink Bot channel: QR login, `getupdates` long poll, `sendmessage`, `context_token` reuse,
  `errcode -14` detection, reconnect with backoff, exact wire header casing.
- SQLite state machine: sessions, questions, outbox, inbound dedupe, audit.
- Router with four-tier answer attribution (quoted message > `#CODE` > sole pending > refuse),
  owner allowlist with `/pair`, dangerous-operation gate.
- Daemon: loopback token-authenticated JSON API, outbox retry worker, expiry sweeper, restart recovery.
- Agent adapters: Claude Code hooks (incl. `PreToolUse` remote approval with fail-safe fallback),
  Codex `notify`, generic env/CLI context.
- MCP stdio server: `ask_human`, `notify`, `progress`, `check_answer`, `list_pending`.
- CLI: `init login logout start stop serve status doctor ask notify answer pending sessions hook mcp`.
- 74 offline tests.
- Research report comparing Happy, tlive, claude-telegram-mirror, openclaw-weixin, wechat-claude-code.
