"""Snapshot: the small, factual picture a node reports to the hub.

The advisor (M3) is told to answer only from facts, so a node must publish facts,
not prose: current branch, uncommitted files, last commit, live sessions and
pending decisions. Everything here is best-effort and must never raise - a
snapshot problem must not affect the bridge.
"""

from __future__ import annotations

import shutil
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
        snap["projects"] = sorted({s.get("project") or "" for s in sessions if s.get("project")})[:8]
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
