"""Snapshot: the small, factual picture a node reports to the hub.

The advisor (M3) is told to answer only from facts, so a node must publish facts,
not prose: current branch, uncommitted files, last commit, live sessions and
pending decisions. Everything here is best-effort and must never raise - a
snapshot problem must not affect the bridge.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from .util import now, truncate


def _git(cwd: str, *args: str) -> str:
    git = shutil.which("git")
    if not git:
        return ""
    try:
        out = subprocess.run(
            [git, "-C", cwd, *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=8,
        )
        return (out.stdout or "").strip()
    except Exception:
        return ""


def build(cwd: str, store: Any) -> dict[str, Any]:
    snap: dict[str, Any] = {"ts": now(), "cwd": cwd}
    try:
        snap["host"] = socket.gethostname()
    except Exception:
        snap["host"] = ""
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if branch:
        porcelain = _git(cwd, "status", "--porcelain")
        dirty = len([ln for ln in porcelain.splitlines() if ln.strip()])
        last = truncate(_git(cwd, "log", "-1", "--pretty=%h %s"), 120)
        snap["git"] = {"branch": branch, "dirty_files": dirty, "last_commit": last}
        snap["summary"] = f"{branch} 分支, {dirty} 个未提交改动, 最近: {last}"
    else:
        snap["summary"] = "非 git 目录或 git 不可用"

    try:
        stats = store.stats() if store is not None else {}
        sessions = store.list_sessions(limit=5) if store is not None else []
        snap["stats"] = {
            "active_sessions": stats.get("active_sessions", 0),
            "pending_questions": stats.get("pending_questions", 0),
            "answered": stats.get("answered", 0),
        }
        # A project is a directory that is a git repository. Session labels are
        # not reliable (temp dirs from tests, version-like names), so we look at
        # the working directories instead and keep only real repositories.
        names: set[str] = set()
        for s in sessions:
            cwd = (s.get("cwd") or "").strip()
            if not cwd:
                continue
            path = Path(cwd)
            if not (path / ".git").exists():
                continue
            name = path.name.strip()
            if not name or re.fullmatch(r"[vV]?\d+(\.\d+)*", name):
                continue
            if name.lower().startswith(("arb-verify-", "arb-hub-", "arb-quote-", "arb-auto-", "arb-final-")):
                continue
            names.add(name)
        here = Path(cwd or "")
        if cwd and (here / ".git").exists() and here.name:
            names.add(here.name)
        snap["projects"] = sorted(names)[:6]
        starts = []
        for s in sessions:
            for key in ("created_at", "started_at", "started", "first_seen"):
                value = s.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    starts.append(float(value))
                    break
        snap["since"] = min(starts) if starts else None
        snap["sessions"] = [
            {"agent": s.get("agent"), "project": s.get("project"), "status": s.get("status")}
            for s in sessions[:5]
        ]
    except Exception:
        pass
    return snap


def projects(cwd: str) -> list[str]:
    branch = _git(cwd, "remote", "get-url", "origin")
    name = ""
    if branch:
        name = Path(branch.rstrip("/")).stem
    return [name] if name else ([Path(cwd).name] if cwd else [])
