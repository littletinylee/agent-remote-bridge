"""Loopback API client shared by the CLI, hooks and the MCP server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from .config import Config, load


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int = 0, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class DaemonUnavailable(RuntimeError):
    pass


class Client:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or load()

    # -- transport -------------------------------------------------------
    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 20.0) -> dict[str, Any]:
        url = f"{self.cfg.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-ARB-Token", self.cfg.api_token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            raise ApiError(f"{path}: HTTP {exc.code} {raw[:300]}", status=exc.code) from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise DaemonUnavailable(str(exc)) from None
        return json.loads(body.decode("utf-8") or "{}")

    def health(self, timeout: float = 2.0) -> dict[str, Any] | None:
        try:
            return self._call("GET", "/api/health", timeout=timeout)
        except (ApiError, DaemonUnavailable, OSError, ValueError):
            return None

    # -- daemon boot -----------------------------------------------------
    def ensure(self, spawn: bool = True, wait_s: float = 15.0) -> dict[str, Any]:
        info = self.health()
        if info is not None:
            return info
        if not spawn:
            raise DaemonUnavailable("daemon is not running")
        self._spawn()
        deadline = time.time() + wait_s
        while time.time() < deadline:
            info = self.health()
            if info is not None:
                return info
            time.sleep(0.25)
        raise DaemonUnavailable(
            f"daemon did not come up within {wait_s:.0f}s (see {self.cfg.log_path})"
        )

    def _spawn(self) -> None:
        lock = self.cfg.home / "daemon.start.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        if lock.exists():
            try:
                if time.time() - lock.stat().st_mtime > 20:
                    lock.unlink()
            except OSError:
                pass
        fd = None
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # another process is starting it; just wait for health
            return
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            if fd is not None:
                os.close(fd)
        env = dict(os.environ)
        env.setdefault("ARB_HOME", str(self.cfg.home))
        log_path = self.cfg.log_path
        # Keep the log from growing without bound on a machine that runs for weeks.
        try:
            if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
                backup = log_path.with_suffix(".log.1")
                if backup.exists():
                    backup.unlink()
                log_path.rename(backup)
        except OSError:
            pass
        log = open(log_path, "ab")  # noqa: SIM115 - intentionally long lived
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "env": env, "cwd": os.getcwd(), **quiet_kwargs()}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen([sys.executable, "-m", "arb", "serve"], **kwargs)
        finally:
            try:
                lock.unlink()
            except OSError:
                pass

    # -- api -------------------------------------------------------------
    def session(self, **kw: Any) -> dict[str, Any]:
        return self._call("POST", "/api/session", kw)["session"]

    def notify(self, **kw: Any) -> dict[str, Any]:
        return self._call("POST", "/api/notify", kw)

    def ask(self, **kw: Any) -> dict[str, Any]:
        return self._call("POST", "/api/ask", kw)

    def wait(self, question_id: str, timeout: float) -> dict[str, Any]:
        return self._call("GET", f"/api/wait?id={question_id}&timeout={int(timeout)}", timeout=timeout + 15)

    def pending(self) -> dict[str, Any]:
        return self._call("GET", "/api/pending")

    def status(self) -> dict[str, Any]:
        return self._call("GET", "/api/status")

    def sessions(self) -> dict[str, Any]:
        return self._call("GET", "/api/sessions")

    def question(self, **kw: Any) -> dict[str, Any]:
        qs = "&".join(f"{k}={v}" for k, v in kw.items())
        return self._call("GET", f"/api/question?{qs}")

    def answer(self, code: str, text: str, by: str = "cli") -> dict[str, Any]:
        return self._call("POST", "/api/answer", {"code": code, "text": text, "by": by})

    def close_session(self, session_id: str) -> dict[str, Any]:
        return self._call("POST", "/api/session/close", {"session_id": session_id})
