"""SQLite persistence: sessions, questions, outbox, inbound dedupe, audit.

Design notes
------------
* WAL + one connection per thread: the daemon runs a channel thread, an
  outbox thread, a sweeper thread and HTTP handler threads concurrently.
* Every state transition that must happen at most once (answering a question)
  is a single conditional ``UPDATE ... WHERE state='pending'``; the winner is
  whoever gets ``rowcount == 1``. That is the whole double-approval defence.
* Nothing is ever deleted, so a crash mid-flight leaves an auditable trail.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .util import gen_code, json_dumps, json_loads, new_id, now, truncate

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    project     TEXT NOT NULL,
    cwd         TEXT,
    label       TEXT,
    parent_id   TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    session_key TEXT,
    meta        TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    last_seen   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_key ON sessions(session_key) WHERE session_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS questions (
    id              TEXT PRIMARY KEY,
    code            TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'decision',
    prompt          TEXT NOT NULL,
    options         TEXT,
    default_action  TEXT,
    dangerous       INTEGER NOT NULL DEFAULT 0,
    state           TEXT NOT NULL DEFAULT 'pending',
    answer          TEXT,
    answer_by       TEXT,
    answer_raw      TEXT,
    answered_at     REAL,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    outbound_msg_id TEXT,
    delivered_at    REAL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    context         TEXT
);
CREATE INDEX IF NOT EXISTS idx_questions_state ON questions(state, created_at);
CREATE INDEX IF NOT EXISTS idx_questions_code ON questions(code);
CREATE INDEX IF NOT EXISTS idx_questions_msg ON questions(outbound_msg_id);

CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe      TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    session_id  TEXT,
    question_id TEXT,
    created_at  REAL NOT NULL,
    next_try    REAL NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    sent_at     REAL,
    sent_msg_id TEXT,
    last_error  TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(sent_at, next_try);

CREATE TABLE IF NOT EXISTS inbound (
    msg_id        TEXT PRIMARY KEY,
    from_user     TEXT,
    text          TEXT,
    ref_svr_id    TEXT,
    context_token TEXT,
    received_at   REAL NOT NULL,
    handled       INTEGER NOT NULL DEFAULT 0,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    k          TEXT PRIMARY KEY,
    v          TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    actor  TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""


@dataclass
class Question:
    id: str
    code: str
    session_id: str
    kind: str
    prompt: str
    options: list[str] = field(default_factory=list)
    default_action: str | None = None
    dangerous: bool = False
    state: str = "pending"
    answer: str | None = None
    answer_by: str | None = None
    answer_raw: str | None = None
    answered_at: float | None = None
    created_at: float = 0.0
    expires_at: float = 0.0
    outbound_msg_id: str | None = None
    delivered_at: float | None = None
    attempts: int = 0
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def remaining(self) -> float:
        return max(0.0, self.expires_at - now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "session_id": self.session_id,
            "kind": self.kind,
            "prompt": self.prompt,
            "options": self.options,
            "default_action": self.default_action,
            "dangerous": self.dangerous,
            "state": self.state,
            "answer": self.answer,
            "answer_by": self.answer_by,
            "answer_raw": self.answer_raw,
            "answered_at": self.answered_at,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "outbound_msg_id": self.outbound_msg_id,
            "delivered_at": self.delivered_at,
            "attempts": self.attempts,
            "context": self.context,
        }


def _row_to_question(row: sqlite3.Row) -> Question:
    return Question(
        id=row["id"],
        code=row["code"],
        session_id=row["session_id"],
        kind=row["kind"],
        prompt=row["prompt"],
        options=json_loads(row["options"], []) or [],
        default_action=row["default_action"],
        dangerous=bool(row["dangerous"]),
        state=row["state"],
        answer=row["answer"],
        answer_by=row["answer_by"],
        answer_raw=row["answer_raw"],
        answered_at=row["answered_at"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        outbound_msg_id=row["outbound_msg_id"],
        delivered_at=row["delivered_at"],
        attempts=row["attempts"],
        context=json_loads(row["context"], {}) or {},
    )


class Store:
    """Thread-safe-enough SQLite wrapper (one connection per thread)."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._init_lock:
            conn = self._conn()
            conn.executescript(SCHEMA)
            conn.commit()

    # -- plumbing --------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self._conn().execute(sql, tuple(args)).fetchall())

    def q1(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, tuple(args)).fetchone()

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        cur = self._conn().execute(sql, tuple(args))
        return cur.rowcount

    # -- kv --------------------------------------------------------------
    def kv_get(self, key: str, default: str = "") -> str:
        row = self.q1("SELECT v FROM kv WHERE k=?", (key,))
        return row["v"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.x(
            "INSERT INTO kv(k,v,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (key, value, now()),
        )

    # -- audit -----------------------------------------------------------
    def audit(self, action: str, actor: str = "system", detail: Any = None) -> None:
        detail_text = detail if isinstance(detail, str) else json_dumps(detail or {})
        self.x("INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)", (now(), actor, action, detail_text))

    def audit_tail(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # -- sessions --------------------------------------------------------
    def upsert_session(
        self,
        agent: str,
        project: str,
        cwd: str = "",
        label: str = "",
        parent_id: str | None = None,
        meta: dict[str, Any] | None = None,
        session_id: str | None = None,
        key: str | None = None,
        status: str = "active",
    ) -> dict[str, Any]:
        """Register or refresh a session.

        ``key`` is a stable per-agent-session identifier (e.g. Claude's
        ``session_id``): when given and already known we reuse the row so a
        restarted hook process keeps routing to the same logical session.
        """
        ts = now()
        existing = None
        if session_id:
            existing = self.q1("SELECT * FROM sessions WHERE id=?", (session_id,))
        if existing is None and key:
            existing = self.q1("SELECT * FROM sessions WHERE session_key=?", (key,))
        if existing is None and not session_id and not key:
            # best-effort convergence for hooks that only know cwd+agent
            existing = self.q1(
                "SELECT * FROM sessions WHERE agent=? AND cwd=? AND status IN ('active','waiting') "
                "ORDER BY last_seen DESC LIMIT 1",
                (agent, cwd),
            )
        if existing is not None:
            sid = existing["id"]
            merged_meta = json_loads(existing["meta"], {}) or {}
            merged_meta.update(meta or {})
            self.x(
                "UPDATE sessions SET status=?, label=COALESCE(NULLIF(?,''),label), "
                "parent_id=COALESCE(?,parent_id), meta=?, session_key=COALESCE(?,session_key), "
                "updated_at=?, last_seen=? WHERE id=?",
                (status, label, parent_id, json_dumps(merged_meta), key, ts, ts, sid),
            )
            return dict(self.q1("SELECT * FROM sessions WHERE id=?", (sid,)))

        sid = session_id or new_id("s")
        self.x(
            "INSERT INTO sessions(id,agent,project,cwd,label,parent_id,status,session_key,meta,"
            "created_at,updated_at,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, agent, project, cwd, label or project, parent_id, status, key, json_dumps(meta or {}), ts, ts, ts),
        )
        return dict(self.q1("SELECT * FROM sessions WHERE id=?", (sid,)))

    def touch_session(self, session_id: str, status: str | None = None) -> None:
        if not session_id:
            return
        if status:
            self.x("UPDATE sessions SET status=?, last_seen=?, updated_at=? WHERE id=?", (status, now(), now(), session_id))
        else:
            self.x("UPDATE sessions SET last_seen=? WHERE id=?", (now(), session_id))

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.q1("SELECT * FROM sessions WHERE id=?", (session_id,))
        return dict(row) if row else None

    def list_sessions(self, include_stale: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.q("SELECT * FROM sessions ORDER BY last_seen DESC LIMIT ?", (limit,))
        out = []
        t = now()
        for r in rows:
            d = dict(r)
            if not include_stale and d["status"] in {"done", "dead"} and t - (d["last_seen"] or 0) > 3600:
                continue
            out.append(d)
        return out

    # -- questions -------------------------------------------------------
    def create_question(
        self,
        session_id: str,
        prompt: str,
        options: list[str] | None = None,
        kind: str = "decision",
        dangerous: bool = False,
        ttl_s: int = 1800,
        default_action: str | None = None,
        context: dict[str, Any] | None = None,
        code: str | None = None,
    ) -> Question:
        ts = now()
        qid = new_id("q")
        chosen = ""
        for _ in range(64):
            candidate = (code or gen_code()).upper()
            busy = self.q1("SELECT 1 FROM questions WHERE code=? AND state='pending'", (candidate,))
            if not busy:
                chosen = candidate
                break
        if not chosen:
            raise RuntimeError("could not allocate a unique question code")
        self.x(
            "INSERT INTO questions(id,code,session_id,kind,prompt,options,default_action,dangerous,state,"
            "created_at,expires_at,context) VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?)",
            (
                qid,
                chosen,
                session_id,
                kind,
                prompt,
                json_dumps(options or []),
                default_action,
                1 if dangerous else 0,
                ts,
                ts + max(30, ttl_s),
                json_dumps(context or {}),
            ),
        )
        q = self.get_question(qid)
        assert q is not None
        return q

    def get_question(self, question_id: str) -> Question | None:
        row = self.q1("SELECT * FROM questions WHERE id=?", (question_id,))
        return _row_to_question(row) if row else None

    def get_question_by_code(self, code: str) -> Question | None:
        row = self.q1(
            "SELECT * FROM questions WHERE code=? ORDER BY (state='pending') DESC, created_at DESC LIMIT 1",
            (code.strip().upper().lstrip("#"),),
        )
        return _row_to_question(row) if row else None

    def get_question_by_outbound(self, msg_id: str) -> Question | None:
        if not msg_id:
            return None
        row = self.q1("SELECT * FROM questions WHERE outbound_msg_id=? ORDER BY created_at DESC LIMIT 1", (msg_id,))
        return _row_to_question(row) if row else None

    def pending_questions(self, session_id: str | None = None) -> list[Question]:
        if session_id:
            rows = self.q("SELECT * FROM questions WHERE state='pending' AND session_id=? ORDER BY created_at", (session_id,))
        else:
            rows = self.q("SELECT * FROM questions WHERE state='pending' ORDER BY created_at")
        return [_row_to_question(r) for r in rows]

    def claim_answer(self, question_id: str, answer: str, raw: str, by: str) -> tuple[bool, Question | None]:
        """Atomically move pending -> answered. Returns (won, question).

        The conditional UPDATE is the single point of truth: with two
        concurrent replies exactly one gets ``rowcount == 1``.
        """
        ts = now()
        cur = self._conn().execute(
            "UPDATE questions SET state='answered', answer=?, answer_raw=?, answer_by=?, answered_at=? "
            "WHERE id=? AND state='pending' AND expires_at > ?",
            (answer, truncate(raw, 1000), by, ts, question_id, ts),
        )
        won = cur.rowcount == 1
        return won, self.get_question(question_id)

    def set_question_state(self, question_id: str, state: str, note: str = "") -> bool:
        n = self.x(
            "UPDATE questions SET state=?, answer=COALESCE(answer, ?) WHERE id=? AND state='pending'",
            (state, note or None, question_id),
        )
        return n == 1

    def expire_due(self) -> list[Question]:
        ts = now()
        due = [dict(r) for r in self.q("SELECT * FROM questions WHERE state='pending' AND expires_at <= ?", (ts,))]
        out: list[Question] = []
        for row in due:
            n = self.x("UPDATE questions SET state='expired' WHERE id=? AND state='pending'", (row["id"],))
            if n == 1:
                q = self.get_question(row["id"])
                if q:
                    out.append(q)
        return out

    def mark_delivered(self, question_id: str, msg_id: str) -> None:
        self.x(
            "UPDATE questions SET outbound_msg_id=?, delivered_at=?, attempts=attempts+1 WHERE id=?",
            (msg_id, now(), question_id),
        )

    def bump_question_attempt(self, question_id: str) -> None:
        self.x("UPDATE questions SET attempts=attempts+1 WHERE id=?", (question_id,))

    # -- outbox ----------------------------------------------------------
    def enqueue(
        self,
        text: str,
        kind: str = "notify",
        dedupe: str | None = None,
        session_id: str | None = None,
        question_id: str | None = None,
        delay_s: float = 0.0,
    ) -> int | None:
        dedupe_key = dedupe or new_id("d")
        ts = now()
        n = self.x(
            "INSERT OR IGNORE INTO outbox(dedupe,kind,text,session_id,question_id,created_at,next_try)"
            " VALUES(?,?,?,?,?,?,?)",
            (dedupe_key, kind, text, session_id, question_id, ts, ts + max(0.0, delay_s)),
        )
        if n == 0:
            return None
        row = self.q1("SELECT id FROM outbox WHERE dedupe=?", (dedupe_key,))
        return int(row["id"]) if row else None

    def due_outbox(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.q(
            "SELECT * FROM outbox WHERE sent_at IS NULL AND next_try <= ? ORDER BY id LIMIT ?", (now(), limit)
        )
        return [dict(r) for r in rows]

    def mark_outbox_sent(self, outbox_id: int, msg_id: str) -> None:
        self.x("UPDATE outbox SET sent_at=?, sent_msg_id=?, attempts=attempts+1 WHERE id=?", (now(), msg_id, outbox_id))

    def mark_outbox_failed(self, outbox_id: int, error: str, retry_in_s: float) -> None:
        self.x(
            "UPDATE outbox SET attempts=attempts+1, last_error=?, next_try=? WHERE id=?",
            (truncate(error, 500), now() + max(1.0, retry_in_s), outbox_id),
        )

    def outbox_stats(self) -> dict[str, int]:
        pending = self.q1("SELECT COUNT(*) c FROM outbox WHERE sent_at IS NULL")
        failed = self.q1("SELECT COUNT(*) c FROM outbox WHERE sent_at IS NULL AND attempts >= 3")
        sent = self.q1("SELECT COUNT(*) c FROM outbox WHERE sent_at IS NOT NULL")
        return {
            "pending": int(pending["c"]) if pending else 0,
            "failing": int(failed["c"]) if failed else 0,
            "sent": int(sent["c"]) if sent else 0,
        }

    # -- inbound ---------------------------------------------------------
    def record_inbound(self, msg_id: str, from_user: str, text: str, ref_svr_id: str = "", context_token: str = "") -> bool:
        """Returns False when this message was already processed (dedupe)."""
        try:
            self.x(
                "INSERT INTO inbound(msg_id,from_user,text,ref_svr_id,context_token,received_at,handled) VALUES(?,?,?,?,?,?,0)",
                (msg_id, from_user, truncate(text, 4000), ref_svr_id, context_token, now()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_inbound(self, msg_id: str, note: str) -> None:
        self.x("UPDATE inbound SET handled=1, note=? WHERE msg_id=?", (truncate(note, 500), msg_id))

    # -- misc ------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        def count(sql: str, args: Iterable[Any] = ()) -> int:
            row = self.q1(sql, args)
            return int(row["c"]) if row else 0

        return {
            "sessions": count("SELECT COUNT(*) c FROM sessions"),
            "active_sessions": count("SELECT COUNT(*) c FROM sessions WHERE status IN ('active','waiting')"),
            "pending_questions": count("SELECT COUNT(*) c FROM questions WHERE state='pending'"),
            "questions_total": count("SELECT COUNT(*) c FROM questions"),
            "answered": count("SELECT COUNT(*) c FROM questions WHERE state='answered'"),
            "expired": count("SELECT COUNT(*) c FROM questions WHERE state='expired'"),
            "inbound": count("SELECT COUNT(*) c FROM inbound"),
        }

    def reset_for_tests(self) -> None:
        for table in ("questions", "outbox", "inbound", "audit", "sessions", "kv"):
            self.x(f"DELETE FROM {table}")
