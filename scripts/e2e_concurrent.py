"""Acceptance test: multi-agent concurrency, non-blocking decisions, no crosstalk.

Phase 1 (non-blocking)
    A = Claude Code writing a file      (keeps working)
    B = Codex asking a question via MCP (blocks in its own process)
    C = Codex writing another file      (keeps working)
    The human answers only AFTER A and C have finished. If the bridge blocked
    anything, A/C could not have finished first.

Phase 2 (two simultaneous questions)
    B1 asks "database?" and B2 asks "cache?" at the same time.
    Both are answered with explicit #CODES. Each agent must report its own
    answer - swapping them here is the failure mode that matters.

Run:  python scripts/e2e_concurrent.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PY = sys.executable
CLAUDE = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report_util import write_report  # noqa: E402


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
    return fallback or "codex"


CODEX = find_codex()


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def api(cfg: dict, path: str, payload: dict | None = None) -> dict:
    import urllib.request

    url = f"http://127.0.0.1:{cfg['port']}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-ARB-Token", cfg["api_token"])
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


class Harness:
    def __init__(self, tmp: Path, arb_home: Path, cfg: dict, env: dict):
        self.tmp = tmp
        self.arb_home = arb_home
        self.cfg = cfg
        self.env = env
        self.events: list[dict] = []
        self.lines: list[str] = []
        self.questions: list[dict] = []
        self._pending_code: str | None = None
        self._lock = threading.Lock()
        self.daemon = subprocess.Popen(
            [PY, "-m", "arb", "serve"],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _now(self) -> float:
        return time.time() - self.t0

    t0 = 0.0

    def mark(self, event: str, **kw) -> None:
        with self._lock:
            self.events.append({"t": round(self._now(), 2), "event": event, **kw})

    def _pump(self) -> None:
        import re

        code_re = re.compile(r"#([2-9A-HJ-NP-Z]{4})")
        assert self.daemon.stdout is not None
        for line in self.daemon.stdout:
            line = line.rstrip("\n")
            with self._lock:
                self.lines.append(line)
                m = code_re.search(line)
                if m and "需要你决策" in line:
                    self._pending_code = m.group(1)
                elif line.startswith("问题:") and self._pending_code:
                    self.questions.append(
                        {"t": round(self._now(), 2), "code": self._pending_code, "prompt": line[3:].strip()}
                    )
                    self._pending_code = None
            print(f"[daemon {self._now():6.2f}s] {line}")

    def answer(self, code: str, text: str = "1") -> None:
        assert self.daemon.stdin is not None
        self.daemon.stdin.write(f"#{code} {text}\n")
        self.daemon.stdin.flush()
        self.mark("human_answer", code=code, text=text)

    def start_agent(self, name: str, cmd: list[str], cwd: Path) -> subprocess.Popen:
        self.mark("start", agent=name)
        return subprocess.Popen(
            cmd, cwd=str(cwd), env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )

    def finish(self, name: str, proc: subprocess.Popen, timeout: float = 600) -> dict:
        out, _ = proc.communicate(timeout=timeout)
        self.mark("finish", agent=name, rc=proc.returncode)
        return {"rc": proc.returncode, "out": out or ""}

    def wait_question(self, timeout: float = 120) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self.questions:
                    return self.questions[0]
            time.sleep(0.1)
        return None

    def close(self) -> None:
        try:
            api(self.cfg, "/api/shutdown", {})
        except Exception:
            pass
        time.sleep(0.3)
        if self.daemon.poll() is None:
            self.daemon.terminate()


def codex_mcp_cmd(project: Path, arb_home: Path, prompt: str, extra: list[str] | None = None) -> list[str]:
    return [
        CODEX, "exec",
        "-c", f"mcp_servers.arb.command={json.dumps(PY)}",
        "-c", f"mcp_servers.arb.args={json.dumps(['-m', 'arb', 'mcp', '--agent', 'codex'])}",
        "-c", f"mcp_servers.arb.env.PYTHONPATH={json.dumps(str(SRC))}",
        "-c", f"mcp_servers.arb.env.ARB_HOME={json.dumps(str(arb_home))}",
        "-c", "mcp_servers.arb.startup_timeout_sec=30",
        "-c", "shell_environment_policy.inherit=all",
        "-C", str(project), "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        *(extra or []),
        prompt,
    ]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="arb-e2e-concurrent-"))
    arb_home = tmp / "arbhome"
    dirs = {name: tmp / name for name in ("a", "c", "b1", "b2")}
    for d in [arb_home, *dirs.values()]:
        d.mkdir(parents=True, exist_ok=True)

    cfg = {
        "port": free_port(),
        "api_token": "e2e-" + os.urandom(6).hex(),
        "pairing_code": "E2E123",
        "channel": "console",
        "owner_ids": ["console"],
        "approval_mode": "notify",
        "notify_min_interval_s": 0,
        "ask_timeout_s": 180,
    }
    (arb_home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    env = dict(os.environ)
    env["ARB_HOME"] = str(arb_home)
    env["PYTHONPATH"] = str(SRC)

    harness = Harness(tmp, arb_home, cfg, env)
    Harness.t0 = time.time()
    result: dict = {"ok": False, "phases": {}, "events": [], "details": {}}
    try:
        for _ in range(150):
            try:
                api(cfg, "/api/health")
                break
            except Exception:
                time.sleep(0.2)
        harness.mark("daemon_ready")
        print("[e2e] daemon ready")

        # ---------------- phase 1: non-blocking ---------------- #
        a = harness.start_agent(
            "A-claude",
            [CLAUDE, "-p",
             "Run exactly this bash command: echo PLAN-A-OK > plan_a.md . "
             "Then reply with the single word A-DONE.",
             "--allowedTools", "Bash", "--output-format", "json"],
            dirs["a"],
        )
        c = harness.start_agent(
            "C-codex",
            [CODEX, "exec", "-C", str(dirs["c"]), "--skip-git-repo-check",
             "-c", "shell_environment_policy.inherit=all",
             "--dangerously-bypass-approvals-and-sandbox",
             "Create a file named note_c.txt containing exactly three lines. Then reply with C-DONE."],
            dirs["c"],
        )
        time.sleep(1.0)
        b = harness.start_agent(
            "B-codex-ask",
            codex_mcp_cmd(
                dirs["b1"], arb_home,
                "Call the MCP tool ask_human from the 'arb' server exactly once with prompt "
                "'Which database should we use?' and options ['Postgres','SQLite']. "
                "Then reply with only the chosen option word.",
            ),
            dirs["b1"],
        )

        q1 = harness.wait_question(timeout=180)
        if not q1:
            raise RuntimeError("B never produced a question")
        harness.mark("question_raised", code=q1["code"])
        print(f"[e2e] B is waiting on #{q1['code']}; A and C must now finish on their own")

        ra = harness.finish("A-claude", a, timeout=600)
        rc = harness.finish("C-codex", c, timeout=600)
        a_file = dirs["a"] / "plan_a.md"
        c_file = dirs["c"] / "note_c.txt"
        print(f"[e2e] A rc={ra['rc']} file={a_file.exists()}  C rc={rc['rc']} file={c_file.exists()}")

        # only now does the human answer
        harness.answer(q1["code"], "1")
        rb = harness.finish("B-codex-ask", b, timeout=600)
        print(f"[e2e] B answered: {rb['out'][-300:]}")

        a_done = ra["rc"] == 0 and a_file.exists() and "PLAN-A-OK" in a_file.read_text(encoding="utf-8", errors="replace")
        c_done = rc["rc"] == 0 and c_file.exists()
        b_used_answer = "postgres" in rb["out"].lower()
        starts = {ev["agent"]: ev["t"] for ev in harness.events if ev["event"] == "start"}
        finishes = {ev["agent"]: ev["t"] for ev in harness.events if ev["event"] == "finish"}
        q_t = next(ev["t"] for ev in harness.events if ev["event"] == "question_raised")
        a_fin = finishes.get("A-claude", 0.0)
        c_fin = finishes.get("C-codex", 0.0)
        b_fin = finishes.get("B-codex-ask", 0.0)
        # Both peers were already running when B asked, and both completed while B
        # was still blocked in its own process. That is the non-blocking contract.
        overlapped = bool(
            starts.get("A-claude", 1e9) < q_t
            and starts.get("C-codex", 1e9) < q_t
            and a_fin >= q_t - 0.05
            and c_fin >= q_t - 0.05
            and a_fin < b_fin
            and c_fin < b_fin
        )
        b_finished_last = b_fin >= max(a_fin, c_fin) and b_fin > 0
        stage1 = bool(a_done and c_done and b_used_answer and overlapped and b_finished_last)
        result["phases"]["phase1_non_blocking"] = {
            "ok": stage1,
            "A_done": a_done,
            "C_done": c_done,
            "B_used_answer": b_used_answer,
            "A_and_C_overlapped_B_wait": overlapped,
            "B_finished_last": b_finished_last,
            "t_question": q_t,
            "t_A_finish": a_fin,
            "t_C_finish": c_fin,
            "t_B_finish": b_fin,
        }
        print(f"[e2e] phase1 ok={stage1} (overlap={overlapped}, B_last={b_finished_last})")

        # ---------------- phase 2: two questions, no crosstalk ---------------- #
        phase2_mark = len(harness.questions)
        b1 = harness.start_agent(
            "B1-db",
            codex_mcp_cmd(
                dirs["b1"], arb_home,
                "Call the MCP tool ask_human from the 'arb' server exactly once with prompt "
                "'Which database should we use?' and options ['Postgres','SQLite']. "
                "Then reply with only the chosen option word.",
            ),
            dirs["b1"],
        )
        b2 = harness.start_agent(
            "B2-cache",
            codex_mcp_cmd(
                dirs["b2"], arb_home,
                "Call the MCP tool ask_human from the 'arb' server exactly once with prompt "
                "'Which cache should we use?' and options ['Redis','File']. "
                "Then reply with only the chosen option word.",
            ),
            dirs["b2"],
        )
        deadline = time.time() + 180
        while time.time() < deadline and len(harness.questions) < phase2_mark + 2:
            time.sleep(0.2)
        qs = list(harness.questions)[phase2_mark:]
        if len(qs) < 2:
            raise RuntimeError(f"expected 2 concurrent questions, got {len(qs)}")
        # map code -> which project it was raised from, using the pushed text
        db_code = next((q["code"] for q in qs if "database" in q["prompt"].lower()), None)
        cache_code = next((q["code"] for q in qs if "cache" in q["prompt"].lower()), None)
        if not db_code or not cache_code:
            raise RuntimeError(f"could not map questions to codes: {qs}")
        print(f"[e2e] two questions: db=#{db_code} cache=#{cache_code}")
        harness.answer(db_code, "1")     # Postgres
        harness.answer(cache_code, "2")  # File
        r1 = harness.finish("B1-db", b1, timeout=600)
        r2 = harness.finish("B2-cache", b2, timeout=600)
        print(f"[e2e] B1: {r1['out'][-200:]}")
        print(f"[e2e] B2: {r2['out'][-200:]}")
        stage2 = ("postgres" in r1["out"].lower() and "file" in r2["out"].lower())
        result["phases"]["phase2_no_crosstalk"] = {
            "ok": stage2,
            "db_code": db_code,
            "cache_code": cache_code,
            "B1_said_postgres": "postgres" in r1["out"].lower(),
            "B2_said_file": "file" in r2["out"].lower(),
        }
        print(f"[e2e] phase2 ok={stage2}")

        stats = api(cfg, "/api/status")["stats"]
        result["details"]["stats"] = stats
        result["details"]["sessions"] = [
            {"agent": s["agent"], "project": s["project"], "status": s["status"]}
            for s in api(cfg, "/api/sessions")["sessions"]
        ]
        result["ok"] = bool(stage1 and stage2)
    except Exception as exc:
        result["details"]["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[e2e] FAIL: {exc}")
    finally:
        harness.mark("done")
        result["events"] = harness.events
        harness.close()
        write_report("e2e-concurrent-report.json", result)
        print("\n=== E2E CONCURRENT RESULT ===")
        print(json.dumps({k: v for k, v in result.items() if k != "events"}, ensure_ascii=False, indent=2))
        print("\nTimeline:")
        for ev in harness.events:
            print(f"  {ev['t']:7.2f}s  {ev['event']:16} {ev.get('agent','')} {ev.get('code','')}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
