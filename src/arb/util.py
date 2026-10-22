"""Small shared helpers: time, ids, atomic IO, redaction, JSON.

stdlib only, by design: the bridge must install and run on a bare Windows
machine with nothing but Python 3.9+.
"""

from __future__ import annotations

import hmac
import json
import os
import random
import re
import secrets
import string
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Crockford-ish alphabet: no 0/O, 1/I/L to keep codes readable in chat.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = 4


def now() -> float:
    return time.time()


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts if ts is not None else now()))


def short_time(ts: float | None = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts if ts is not None else now()))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def gen_code(rng: random.Random | None = None) -> str:
    r = rng or secrets.SystemRandom()
    return "".join(r.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def gen_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def gen_client_id(prefix: str = "arb") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def json_loads(text: str | bytes | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def atomic_write(path: os.PathLike[str] | str, text: str, mode: int = 0o600) -> None:
    """Write a file atomically (POSIX + Windows). Best effort on permissions."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, p)


_TOKEN_PATTERNS = [
    re.compile(r"(bot_token|token|secret|api_key|apikey|password)([\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9_\-\.\/\+=]{8,})", re.I),
    re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"),
]


def redact(text: str, keep: int = 4) -> str:
    """Redact obvious secrets so they never reach chat or logs."""
    if not text:
        return text

    def _sub(m: re.Match[str]) -> str:
        val = m.group(3) if m.lastindex and m.lastindex >= 3 else m.group(1)
        head = val[:keep]
        return m.group(0).replace(val, head + "***")

    out = text
    for pat in _TOKEN_PATTERNS:
        out = pat.sub(_sub, out)
    return out


def truncate(text: str, limit: int = 600, suffix: str = "…") -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - len(suffix)] + suffix


def one_line(text: str, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return truncate(text, limit)


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "allow", "allowed", "approve", "approved"}


def is_windows() -> bool:
    return os.name == "nt"


def log_stderr(msg: str) -> None:
    """Diagnostics must never pollute stdout: MCP/hook channels own stdout."""
    print(msg, file=sys.stderr, flush=True)


def humanize_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分钟"
    return f"{seconds // 3600}小时{(seconds % 3600) // 60}分"


def safe_basename(path: str | os.PathLike[str]) -> str:
    try:
        return Path(path).name or str(path)
    except (OSError, ValueError):
        return string.capwords(str(path))


def secret_compare(given: str, expected: str) -> bool:
    """Constant-time comparison for the loopback API token."""
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def clean_text(value: Any) -> str:
    """Make a string safe to store/broadcast.

    Agents (and Windows console codepages) occasionally hand us lone UTF-16
    surrogates. JSON accepts them, sqlite3 then raises UnicodeEncodeError, and
    the caller sees a half-applied request. Replace them instead of exploding.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8", "replace")


def clean_json(value: Any) -> Any:
    """Recursively clean every string inside a JSON-ish structure."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return {clean_text(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    return value
