"""Configuration + on-disk layout.

Everything lives under one home directory (``ARB_HOME`` or
``%APPDATA%\\agent-remote-bridge``) so uninstalling is a single delete and
secrets never end up in the repository.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .util import atomic_write, gen_secret, json_dumps

APP_NAME = "agent-remote-bridge"
CONFIG_VERSION = 1

# Tools that a remote approval gate asks about by default (Claude Code hook
# matchers / Codex approval domains). Deliberately conservative: anything that
# can mutate the machine or run code.
DEFAULT_GATED_TOOLS = ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch"]


def default_home() -> Path:
    env = os.environ.get("ARB_HOME")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


@dataclass
class Config:
    # identity / transport
    port: int = 8848
    api_token: str = ""
    pairing_code: str = ""
    owner_ids: list[str] = field(default_factory=list)

    # channel
    channel: str = "ilink"  # ilink | console | relay
    hub_url: str = ""
    hub_node_name: str = ""
    hub_token: str = ""
    hub_node_id: str = ""
    hub_fingerprint: str = ""
    ilink_base_url: str = "https://ilinkai.weixin.qq.com"
    ilink_bot_type: str = "3"

    # timing
    ask_timeout_s: int = 900
    question_ttl_s: int = 1800
    notify_min_interval_s: int = 20
    max_push_chars: int = 1500
    outbox_retry_max_s: int = 300
    channel_retry_s: int = 30

    # approval gate
    approval_mode: str = "notify"  # notify | approve
    approval_tools: list[str] = field(default_factory=lambda: list(DEFAULT_GATED_TOOLS))
    approval_timeout_s: int = 600
    on_timeout: str = "deny"  # deny | allow | ask  (never auto-allow dangerous)
    approval_max_per_session_per_min: int = 12
    # Read-only shell commands (ls/cat/git status ...) are approved automatically:
    # asking about them is noise, and they cannot change the machine. Anything
    # that shells out, chains, redirects or touches a secret is still asked.
    auto_approve_safe: bool = True
    safe_commands: list[str] = field(
        default_factory=lambda: [
            "ls", "cat", "head", "tail", "wc", "pwd", "echo", "type", "stat",
            "du", "df", "file", "which", "where", "whoami", "date", "env",
            "find", "grep", "rg", "sort", "uniq", "diff", "tree",
        ]
    )
    safe_git_subcommands: list[str] = field(
        default_factory=lambda: ["status", "log", "diff", "show", "branch", "remote", "rev-parse"]
    )

    # behaviour
    quiet: bool = False
    log_level: str = "INFO"
    version: int = CONFIG_VERSION

    # ---- derived paths -------------------------------------------------
    @property
    def home(self) -> Path:
        return default_home()

    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    @property
    def db_path(self) -> Path:
        return self.home / "arb.sqlite3"

    @property
    def ilink_path(self) -> Path:
        return self.home / "ilink.json"

    @property
    def log_path(self) -> Path:
        return self.home / "arb.log"

    @property
    def pid_path(self) -> Path:
        return self.home / "daemon.pid"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ---- io ------------------------------------------------------------
    def ensure_home(self) -> Path:
        home = self.home
        home.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(home, 0o700)
        except OSError:
            pass
        return home

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # never persist the API token in a world readable place twice; keep it
        # in config.json only (0600).
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in (data or {}).items() if k in known}
        cfg = cls(**clean)
        cfg.normalise()
        return cfg

    def normalise(self) -> None:
        self.port = int(self.port)
        self.ask_timeout_s = max(10, int(self.ask_timeout_s))
        self.question_ttl_s = max(30, int(self.question_ttl_s))
        self.approval_timeout_s = max(10, int(self.approval_timeout_s))
        self.owner_ids = [str(x) for x in (self.owner_ids or []) if str(x).strip()]
        if self.channel not in {"ilink", "console", "relay"}:
            self.channel = "ilink"
        if self.approval_mode not in {"notify", "approve"}:
            self.approval_mode = "notify"
        if self.on_timeout not in {"deny", "allow", "ask"}:
            self.on_timeout = "deny"

    def save(self) -> None:
        self.ensure_home()
        atomic_write(self.config_path, json.dumps(self.to_dict(), ensure_ascii=False, indent=2))

    def bootstrap(self) -> "Config":
        """Fill in random secrets if missing, then persist."""
        changed = False
        if not self.api_token:
            self.api_token = gen_secret(32)
            changed = True
        if not self.pairing_code:
            self.pairing_code = gen_secret(9).replace("-", "").replace("_", "")[:10].upper()
            changed = True
        if changed:
            self.save()
        return self


def load() -> Config:
    home = default_home()
    path = home / "config.json"
    if not path.exists():
        cfg = Config()
        cfg.ensure_home()
        return cfg.bootstrap()
    # utf-8-sig: Windows editors (Notepad, PowerShell 5 `-Encoding utf8`) like to
    # prepend a BOM; a hand-edited config must not brick the bridge.
    raw = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    cfg = Config.from_dict(raw)
    return cfg.bootstrap()


def load_or_default() -> Config:
    try:
        return load()
    except (OSError, ValueError):
        return Config().bootstrap()


# --- iLink credentials (separate file, never in config.json dump/logs) ----

@dataclass
class IlinkCredentials:
    token: str = ""
    account_id: str = ""
    base_url: str = ""
    logged_in_at: float = 0.0
    bot_user_id: str = ""

    @property
    def valid(self) -> bool:
        return bool(self.token)


def load_ilink(cfg: Config) -> IlinkCredentials:
    if not cfg.ilink_path.exists():
        return IlinkCredentials()
    data = json.loads(cfg.ilink_path.read_text(encoding="utf-8-sig") or "{}")
    return IlinkCredentials(
        token=data.get("token", ""),
        account_id=data.get("account_id", ""),
        base_url=data.get("base_url", cfg.ilink_base_url),
        logged_in_at=float(data.get("logged_in_at") or 0),
        bot_user_id=data.get("bot_user_id", ""),
    )


def save_ilink(cfg: Config, creds: IlinkCredentials) -> None:
    cfg.ensure_home()
    payload = {
        "token": creds.token,
        "account_id": creds.account_id,
        "base_url": creds.base_url or cfg.ilink_base_url,
        "logged_in_at": creds.logged_in_at,
        "bot_user_id": creds.bot_user_id,
    }
    atomic_write(cfg.ilink_path, json_dumps(payload))


def clear_ilink(cfg: Config) -> None:
    try:
        cfg.ilink_path.unlink()
    except FileNotFoundError:
        pass
