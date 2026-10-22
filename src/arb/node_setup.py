"""Wire this machine's agents to the bridge, using each vendor's own plugin
mechanism - no "please call the arb tool" instructions in the chat.

Verified contracts (2026-10, see docs/AGENTS.md):

* Codex 0.162 has a plugin marketplace: `.codex-plugin/plugin.json` may carry
  both `mcpServers` (a path to a JSON file) and `hooks` (same event names and
  `hookSpecificOutput` as Claude Code). Installed with
  `codex plugin marketplace add <dir>` + `codex plugin add <name>@<market>`.
* DSH registers MCP servers by adding one `@deepseek-ai/dsh-mcp-client` entry to
  the cordis patch of the active profile; tools then appear to the model as
  `mcp__<serverName>__<tool>` with no prompting.

Everything here is additive, idempotent, backed up and reportable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

MARKET_NAME = "agent-remote-bridge"
PLUGIN_NAME = "agent-remote-bridge"
DSH_ENTRY_ID = "mcp-arb"
MARKER_COMMENT = "# added by agent-remote-bridge (arb node setup)"
CODEX_HOOK_EVENTS: dict[str, tuple[str | None, int]] = {
    "SessionStart": (None, 45),
    "UserPromptSubmit": (None, 45),
    "Stop": (None, 60),
    "SubagentStop": (None, 60),
    "SessionEnd": (None, 45),
    "PreToolUse": ("Bash|Write|Edit|MultiEdit", 900),
}
# Measured on Codex 0.162 (2026-10-22):
# * `command` hooks run and receive the full payload, so the session/project
#   attribution is right. `codex exec --dangerously-bypass-hook-trust` still
#   prints "hook: Stop Failed" - that is the trust bookkeeping, the command
#   itself ran (its push landed). Interactive sessions ask to trust once.
# * `mcp_tool` hooks also run but have no cwd/session context: our notify call
#   was attributed to the MCP server's own cwd ("0.4.1"), so they are not used.
# * the hook command must be a single executable path; a quoted
#   `"python" "script.py"` invocation never started.
CODEX_MCP_HOOK_EVENTS: dict[str, tuple[str, str]] = {}


# --------------------------------------------------------------------------
# wrappers (agents start hooks from an environment without our PYTHONPATH)
# --------------------------------------------------------------------------
def write_wrappers(home: Path, python_exe: str) -> dict[str, Path]:
    home.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).resolve().parents[1].as_posix()
    header = '@echo off\nset "PYTHONPATH=' + src + ';%PYTHONPATH%"\n'
    out = {
        "hook_claude": home / "arb-hook.cmd",
        "hook_codex": home / "arb-hook-codex.cmd",
        "mcp": home / "arb-mcp.cmd",
    }
    out["hook_claude"].write_text(header + f'"{python_exe}" -m arb hook claude-code\n', encoding="ascii")
    # Codex hands the event JSON over as the last argv element -> forward args.
    out["hook_codex"].write_text(header + f'"{python_exe}" -m arb hook codex %*\n', encoding="ascii")
    out["mcp"].write_text(header + f'"{python_exe}" -m arb mcp %*\n', encoding="ascii")
    return out


# --------------------------------------------------------------------------
# Codex plugin marketplace
# --------------------------------------------------------------------------
def codex_plugin_dir(home: Path) -> Path:
    return home / "codex-marketplace"


def codex_files(home: Path, python_exe: str, arb_home: Path) -> dict[Path, str]:
    root = codex_plugin_dir(home)
    plugin = root / "plugins" / PLUGIN_NAME
    hooks: dict[str, list[dict[str, Any]]] = {}
    for event, (matcher, timeout) in CODEX_HOOK_EVENTS.items():
        block: dict[str, Any] = {
            "hooks": [{"type": "command", "command": f'"{home / "arb-hook-codex.cmd"}"', "timeout": timeout}]
        }
        if matcher:
            block["matcher"] = matcher
        hooks[event] = [block]
    for event, (title, kind) in CODEX_MCP_HOOK_EVENTS.items():
        hooks[event] = [
            {
                "hooks": [
                    {
                        "type": "mcp_tool",
                        "server": "arb",
                        "tool": "notify",
                        "input": {"title": title, "kind": kind},
                    }
                ]
            }
        ]

    plugin_json = {
        "name": PLUGIN_NAME,
        "version": "0.4.1",
        "description": (
            "Remote supervision for this Codex session: pushes task start/finish/attention "
            "events and the ask_human tool to your phone through Agent Remote Bridge."
        ),
        "author": {"name": "littletinylee"},
        "license": "MIT",
        "homepage": "https://github.com/littletinylee/agent-remote-bridge",
        "mcpServers": "./.mcp.json",
        "hooks": {"hooks": hooks},
        "interface": {
            "displayName": "Agent Remote Bridge",
            "shortDescription": "Ask your phone before dangerous actions; get progress pushed",
            "category": "Developer Tools",
            "capabilities": ["Interactive"],
        },
    }
    mcp_json = {
        "mcpServers": {
            "arb": {
                "command": python_exe,
                "args": ["-m", "arb", "mcp", "--agent", "codex"],
                "cwd": ".",
                "enabled": True,
                "env": {"PYTHONPATH": Path(__file__).resolve().parents[1].as_posix(), "ARB_HOME": str(arb_home)},
            }
        }
    }
    marketplace = {
        "name": MARKET_NAME,
        "interface": {"displayName": "Agent Remote Bridge"},
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Developer Tools",
            }
        ],
    }
    manifest = json.dumps(marketplace, ensure_ascii=False, indent=2) + "\n"
    return {
        # Codex's own bundled marketplaces live at <root>/.agents/plugins/marketplace.json;
        # write both so `codex plugin marketplace add` accepts the directory either way.
        root / "marketplace.json": manifest,
        root / ".agents" / "plugins" / "marketplace.json": manifest,
        plugin / ".codex-plugin" / "plugin.json": json.dumps(plugin_json, ensure_ascii=False, indent=2) + "\n",
        plugin / ".mcp.json": json.dumps(mcp_json, ensure_ascii=False, indent=2) + "\n",
    }


def install_codex(home: Path, python_exe: str, codex_exe: str, dry_run: bool = False) -> dict[str, Any]:
    files = codex_files(home, python_exe, home)
    result: dict[str, Any] = {"written": [], "commands": [], "errors": []}
    if dry_run:
        result["written"] = [str(p) for p in files]
        return result
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        result["written"].append(str(path))
    if not codex_exe:
        result["errors"].append("codex CLI not found; marketplace files written but not installed")
        return result
    for cmd in (
        [codex_exe, "plugin", "marketplace", "add", str(codex_plugin_dir(home))],
        [codex_exe, "plugin", "add", f"{PLUGIN_NAME}@{MARKET_NAME}"],
    ):
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        result["commands"].append({"cmd": " ".join(cmd), "rc": proc.returncode, "out": (proc.stdout or "")[-400:]})
        if proc.returncode != 0:
            result["errors"].append(f"{' '.join(cmd)} -> {proc.returncode}: {(proc.stderr or proc.stdout or '')[-300:]}")
    return result


# --------------------------------------------------------------------------
# DSH: one cordis plugin entry
# --------------------------------------------------------------------------
def _pyw(python_exe: str) -> str:
    """Prefer pythonw: a console interpreter makes DSH spawn a visible window."""
    from pathlib import Path as _P

    candidate = _P(python_exe).with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else python_exe


def dsh_yaml(home: Path, python_exe: str) -> str:
    """The user patch layer must INSERT a new entry; a bare `- id:` is an
    override and the loader silently skips ids it does not already know
    (verified in the loader: `if (!target) { warn('patch: entry not found'); continue }`).
    """
    src = Path(__file__).resolve().parents[1].as_posix()
    return (
        f"- insert:\n"
        f"    - id: {DSH_ENTRY_ID}\n"
        f"      name: '@deepseek-ai/dsh-mcp-client'\n"
        f"      config:\n"
        f"        serverName: arb\n"
        f"        transport: stdio\n"
        f"        command: '{_pyw(python_exe)}'\n"
        f"        args: ['-m', 'arb', 'mcp', '--agent', 'dsh']\n"
        f"        env:\n"
        f"          PYTHONPATH: '{src}'\n"
        f"          ARB_HOME: '{home.as_posix()}'\n"
        f"        toolCallTimeoutMs: 900000\n"
    )


def default_dsh_profile() -> Path:
    return Path.home() / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"


def install_dsh(home: Path, python_exe: str, profile: Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    profile = profile or default_dsh_profile()
    block = dsh_yaml(home, python_exe)
    if not profile.exists():
        return {"changed": False, "error": f"DSH profile not found: {profile}", "profile": str(profile)}
    text = profile.read_text(encoding="utf-8-sig")
    if f"id: {DSH_ENTRY_ID}" in text:
        return {"changed": False, "profile": str(profile), "reason": "already installed"}

    new_text = text
    if new_text.strip() in {"", "[]"}:
        # A brand-new profile is literally `[]`; appending a block sequence to it
        # would be invalid YAML, so replace the placeholder instead.
        new_text = "# added by agent-remote-bridge (arb node setup)\n" + block
    else:
        if not new_text.endswith("\n"):
            new_text += "\n"
        new_text += "\n# added by agent-remote-bridge (arb node setup)\n" + block
    try:
        import yaml  # optional; only used to refuse writing invalid YAML

        yaml.safe_load(new_text)
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover - defensive
        return {"changed": False, "error": f"refusing to write invalid YAML: {exc}", "profile": str(profile)}

    if dry_run:
        return {"changed": True, "dry_run": True, "profile": str(profile), "block": block}
    backup = profile.with_name(f"{profile.name}.arb-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(profile, backup)
    profile.write_text(new_text, encoding="utf-8")
    return {"changed": True, "profile": str(profile), "backup": str(backup)}


def uninstall_dsh(profile: Path | None = None) -> dict[str, Any]:
    """Remove our `- insert:` block (and the marker comment) without touching
    any other patch entry."""
    profile = profile or default_dsh_profile()
    if not profile.exists():
        return {"changed": False, "profile": str(profile)}
    text = profile.read_text(encoding="utf-8-sig")
    if f"id: {DSH_ENTRY_ID}" not in text:
        return {"changed": False, "profile": str(profile)}
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.startswith(MARKER_COMMENT):
            continue  # our header goes with the entry
        if line.startswith("- insert:") or line.startswith(f"- id: {DSH_ENTRY_ID}"):
            skipping = True
            continue
        if skipping:
            # a new top-level patch item or a blank line ends our block
            if line.startswith("- ") or line.strip() == "":
                skipping = False
            else:
                continue
        if not skipping:
            out.append(line)
    remaining = "".join(out).rstrip() + "\n"
    backup = profile.with_name(f"{profile.name}.arb-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(profile, backup)
    profile.write_text(remaining, encoding="utf-8")
    return {"changed": True, "profile": str(profile), "backup": str(backup)}


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
def find_codex() -> str:
    candidates: list[Path] = []
    which = shutil.which("codex")
    if which:
        candidates.append(Path(which))
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    if base.exists():
        candidates.extend(sorted(base.glob("*/codex.exe")))
    sandbox = Path.home() / ".codex" / ".sandbox-bin" / "codex.exe"
    if sandbox.exists():
        candidates.append(sandbox)
    fallback = ""
    for cand in candidates:
        if not cand.exists():
            continue
        d = cand.parent
        if (d / "codex-code-mode-host.exe").exists() and (d / "codex-command-runner.exe").exists():
            return str(cand)
        fallback = fallback or str(cand)
    return fallback


def status(home: Path, profile: Path | None = None) -> dict[str, Any]:
    profile = profile or default_dsh_profile()
    claude = Path.home() / ".claude" / "settings.json"
    codex_installed = False
    plist = codex_plugin_dir(home) / "plugins" / PLUGIN_NAME / ".codex-plugin" / "plugin.json"
    if plist.exists():
        codex_installed = True
    dsh_text = profile.read_text(encoding="utf-8-sig") if profile.exists() else ""
    return {
        "codex": {"plugin_files": codex_installed, "cli": find_codex() or None},
        "dsh": {"profile": str(profile), "installed": f"id: {DSH_ENTRY_ID}" in dsh_text, "exists": profile.exists()},
        "claude": {"settings": str(claude), "exists": claude.exists()},
    }
