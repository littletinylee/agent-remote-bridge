# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning: SemVer.

## [Unreleased]

### Pending verification (needs the owner's phone)
- W1/W2/W3: real personal-WeChat delivery, reply, and long-idle push. See `docs/VERIFICATION.md`.

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
