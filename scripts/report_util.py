"""Report sanitising: verification artifacts are committed, so they must never
carry credentials, account ids or absolute user paths.

Used by the e2e/verify scripts before writing their JSON reports, and by
``scripts/sanitize_reports.py`` to clean up existing files.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_ID_RE = re.compile(r"\b([A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]{4,}@im\.(wechat|bot)")
_SK_RE = re.compile(r"(?i)\b(sk-[a-z0-9_\-]{6})[a-z0-9_\-]+")
_GH_RE = re.compile(r"(?i)\b(github_pat_[a-z0-9_]{6})[a-z0-9_]+")
_GH2_RE = re.compile(r"(?i)\b(ghp_[a-z0-9]{6})[a-z0-9]+")
_WINUSER_RE = re.compile(r"([A-Za-z]:\\{1,2}(?:[Uu]sers|Documents and Settings)\\{1,2})([^\\\"]+)")


def scrub_str(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = _ID_RE.sub(r"\1…@im.\2", text)
    text = _SK_RE.sub(r"\1***", text)
    text = _GH_RE.sub(r"\1***", text)
    text = _GH2_RE.sub(r"\1***", text)
    # bot token looks like  <botid>@im.bot:<hex secret>
    text = re.sub(r"(?i)(@im\.bot:)([a-f0-9]{6})[a-f0-9]+", r"\1\2***", text)
    for var in ("USERPROFILE", "HOME", "APPDATA"):
        base = os.environ.get(var)
        if base:
            text = text.replace(base, "<home>")
            text = text.replace(base.replace("\\", "\\\\"), "<home>")
    text = _WINUSER_RE.sub(r"\1<user>", text)
    text = re.sub(r"(?i)(/users/)([^/\"]+)", r"\1<user>", text)
    return text


def scrub(obj: Any) -> Any:
    if isinstance(obj, str):
        return scrub_str(obj)
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def write_report(path: str | Path, data: Any) -> Path:
    p = Path(path)
    p.write_text(json.dumps(scrub(data), ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def sanitize_file(path: str | Path) -> bool:
    """Rewrite a JSON report in place if sanitising changes anything."""
    p = Path(path)
    try:
        original = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    cleaned = scrub(original)
    if cleaned == original:
        return False
    p.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    return True
