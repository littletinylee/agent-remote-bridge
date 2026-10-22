"""Agent Remote Bridge Hub - the single owner of the WeChat bot connection.

Why this exists: the iLink bot token is one credential with one shared cursor,
so exactly one process may long-poll it. When several dev machines must share a
bot, this hub is that one process; every machine connects *out* to the hub.

Safety invariants (mirrored in the tests):
  * the hub never answers a decision - it only forwards text between the human
    and the machine that asked;
  * decisions are routed by question code only; an unknown or ambiguous target
    is refused, never guessed;
  * a message from machine A can never reach machine B;
  * the chat/advisor layer (M3) may read snapshots and talk, but it is never
    given a path that could approve anything.

Wire protocol (JSON over TLS, per-node bearer token):
  POST /v1/register   {name, token}                -> {node_id, name}
  POST /v1/poll       {node_id, timeout}           -> {messages: [{kind, text, code?}]}
  POST /v1/send       {node_id, text, kind, question?}
  POST /v1/question   {node_id, code, prompt}      -> {code}
  POST /v1/snapshot   {node_id, snapshot}
  POST /v1/heartbeat  {node_id, status}
  GET  /v1/health
  GET  /v1/machines
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .channel.ilink import IlinkChannel, IlinkClient
from .util import clean_text, gen_secret, json_dumps, log_stderr, now, truncate

VERSION = "0.1.0"
DEFAULT_PORT = 18443
CODE_RE = re.compile(r"#([2-9A-HJ-NP-Z]{4})\b", re.I)

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, token_hash TEXT NOT NULL,
    projects TEXT, last_seen REAL, status TEXT DEFAULT 'unknown',
    snapshot TEXT
);
CREATE TABLE IF NOT EXISTS inbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT NOT NULL, kind TEXT NOT NULL,
    text TEXT NOT NULL, code TEXT, created_at REAL NOT NULL, delivered_at REAL, frm TEXT
);
CREATE INDEX IF NOT EXISTS idx_inbox_node ON inbox(node_id, delivered_at);
CREATE TABLE IF NOT EXISTS questions (
    code TEXT PRIMARY KEY, node_id TEXT NOT NULL, prompt TEXT, created_at REAL,
    state TEXT DEFAULT 'pending', msg_id TEXT
);
CREATE TABLE IF NOT EXISTS seen (
    msg_id TEXT PRIMARY KEY, at REAL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, role TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, action TEXT, detail TEXT
);
"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class HubStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        conn = self.conn()
        conn.executescript(SCHEMA)
        cols = {r['name'] for r in conn.execute("PRAGMA table_info(questions)").fetchall()}
        if 'msg_id' not in cols:  # migrate a hub created before quoted replies
            conn.execute("ALTER TABLE questions ADD COLUMN msg_id TEXT")
        icols = {r['name'] for r in conn.execute("PRAGMA table_info(inbox)").fetchall()}
        if 'frm' not in icols:  # migrate a hub created before sender forwarding
            conn.execute("ALTER TABLE inbox ADD COLUMN frm TEXT")
        conn.commit()

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn().execute(sql, args).fetchall())

    def q1(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, args).fetchone()

    def x(self, sql: str, args: tuple = ()) -> int:
        return self.conn().execute(sql, args).rowcount

    def audit(self, actor: str, action: str, detail: Any = None) -> None:
        self.x(
            "INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)",
            (now(), actor, action, clean_text(detail if isinstance(detail, str) else json_dumps(detail or {}))),
        )

    # -- chat log --------------------------------------------------------
    def chat_add(self, role: str, text: str) -> None:
        self.x(
            "INSERT INTO chat(ts,role,text) VALUES(?,?,?)",
            (now(), role, clean_text(text)[:4000]),
        )

    def chat_recent(self, limit: int = 60) -> list[dict]:
        rows = self.q("SELECT ts, role, text FROM chat ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in reversed(rows)]

    def kv_get(self, k: str, default: str = "") -> str:
        row = self.q1("SELECT v FROM kv WHERE k=?", (k,))
        return row["v"] if row else default

    def kv_set(self, k: str, v: str) -> None:
        self.x(
            "INSERT INTO kv(k,v,updated_at) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (k, v, now()),
        )

    # -- nodes -----------------------------------------------------------
    def register(self, name: str, token: str) -> dict[str, Any]:
        existing = self.q1("SELECT * FROM nodes WHERE name=?", (name,))
        if existing:
            if existing["token_hash"] != _hash(token):
                raise PermissionError("node name已注册且 token 不匹配")
            self.x("UPDATE nodes SET last_seen=? WHERE id=?", (now(), existing["id"]))
            return {"node_id": existing["id"], "name": name}
        node_id = gen_secret(8).replace("-", "").replace("_", "")
        self.x(
            "INSERT INTO nodes(id,name,token_hash,projects,last_seen,status) VALUES(?,?,?,'[]',?,'online')",
            (node_id, name, _hash(token), now()),
        )
        self.audit("hub", "node_registered", {"node": name, "id": node_id})
        return {"node_id": node_id, "name": name}

    def node_by_id(self, node_id: str) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM nodes WHERE id=?", (node_id,))

    def node_by_name(self, name: str) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM nodes WHERE name=?", (name,))

    def auth(self, node_id: str, token: str) -> sqlite3.Row | None:
        row = self.node_by_id(node_id)
        if row and row["token_hash"] == _hash(token):
            return row
        return None

    def touch(self, node_id: str, status: str | None = None) -> None:
        if status:
            self.x("UPDATE nodes SET last_seen=?, status=? WHERE id=?", (now(), status, node_id))
        else:
            self.x("UPDATE nodes SET last_seen=? WHERE id=?", (now(), node_id))

    def nodes(self) -> list[dict[str, Any]]:
        out = []
        for r in self.q("SELECT * FROM nodes ORDER BY last_seen DESC"):
            d = dict(r)
            d.pop("token_hash", None)
            d["online"] = bool(d.get("last_seen") and now() - d["last_seen"] < 90)
            out.append(d)
        return out

    # -- inbox -----------------------------------------------------------
    def push(self, node_id: str, text: str, kind: str = "reply", code: str | None = None,
             frm: str = "") -> int | None:
        self.x(
            "INSERT INTO inbox(node_id,kind,text,code,created_at,frm) VALUES(?,?,?,?,?,?)",
            (node_id, kind, clean_text(text), code, now(), frm or None),
        )
        row = self.q1("SELECT last_insert_rowid() AS id")
        return int(row["id"]) if row else None

    def take(self, node_id: str, timeout: float) -> list[dict[str, Any]]:
        deadline = now() + max(0.0, timeout)
        while True:
            rows = self.q(
                "SELECT * FROM inbox WHERE node_id=? AND delivered_at IS NULL ORDER BY seq LIMIT 20", (node_id,)
            )
            if rows:
                self.x(
                    "UPDATE inbox SET delivered_at=? WHERE node_id=? AND delivered_at IS NULL",
                    (now(), node_id),
                )
                return [
                    {"kind": r["kind"], "text": r["text"], "code": r["code"], "frm": r["frm"] or ""}
                    for r in rows
                ]
            if now() >= deadline:
                return []
            time.sleep(0.2)

    # -- questions (routing index only) ----------------------------------
    def put_question(self, code: str, node_id: str, prompt: str, msg_id: str = "") -> None:
        self.x(
            "INSERT INTO questions(code,node_id,prompt,created_at,state,msg_id) VALUES(?,?,?,?,'pending',?) "
            "ON CONFLICT(code) DO UPDATE SET node_id=excluded.node_id, prompt=excluded.prompt, "
            "state='pending', msg_id=excluded.msg_id",
            (code.upper(), node_id, clean_text(prompt), now(), msg_id or None),
        )

    def resolve(self, code: str) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM questions WHERE code=?", (code.upper(),))

    def resolve_by_msg(self, msg_id: str) -> sqlite3.Row | None:
        """A quoted reply points at the pushed message, not at a code."""
        if not msg_id:
            return None
        return self.q1("SELECT * FROM questions WHERE msg_id=? ORDER BY created_at DESC LIMIT 1", (msg_id,))

    def pending(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.q("SELECT * FROM questions WHERE state='pending' ORDER BY created_at")]

    def close_question(self, code: str) -> None:
        self.x("UPDATE questions SET state='closed' WHERE code=?", (code.upper(),))

    def reap(self, max_age: float) -> int:
        """Expire codes nobody answered.

        A stale pending code is worse than none: it keeps a bare reply
        ambiguous and lets an old question be routed to a machine that has
        long since moved on.
        """
        cutoff = now() - max(60.0, float(max_age))
        return self.x(
            "UPDATE questions SET state='expired' WHERE state='pending' AND created_at < ?",
            (cutoff,),
        )


class Hub:
    """Routes between the one WeChat conversation and N machines."""

    def __init__(self, store: HubStore, client: IlinkClient, owner_ids: list[str], hub_name: str = "hub"):
        self.store = store
        self.client = client
        self.owner_ids = owner_ids
        self.name = hub_name
        self._stop = threading.Event()
        self._ctx: dict[str, str] = {}
        self.ui_user = ""
        self.ui_password = ""
        self.question_ttl_s = 1800.0
        self.chat: Any = None  # set by M3 (advisor); never receives approval rights

    # -- outbound to WeChat ---------------------------------------------
    def say(self, text: str) -> str:
        for user in self.owner_ids:
            ctx = self._ctx.get(user) or self.store.kv_get(f"ctx.{user}", "")
            try:
                resp = self.client.send_message(user, clean_text(text), context_token=ctx)
            except Exception as exc:
                self.store.audit("hub", "send_failed", {"error": str(exc)[:200]})
                return ""
            ret = int(resp.get("ret", 0) or 0)
            errcode = int(resp.get("errcode", 0) or 0)
            if errcode == -14 or ret == -14:
                self.store.audit("hub", "token_expired", {"ret": ret, "errcode": errcode})
                return ""
            if errcode or ret:
                # Anything else is a real failure (e.g. ret -2 "prepare failed"
                # when the bot session is stale) and must be visible, not swallowed.
                self.store.audit(
                    "hub", "send_failed",
                    {"ret": ret, "errcode": errcode, "errmsg": str(resp.get("errmsg") or "")[:120]},
                )
                return ""
            self.store.chat_add("bot", text)
            return str(resp.get("message_id") or "")
        return ""

    def push_from_node(self, node_id: str, text: str, kind: str = "notify", code: str | None = None) -> None:
        node = self.store.node_by_id(node_id)
        label = node["name"] if node else node_id
        self.say(f"[{label}] {truncate(text, 1200)}")

    def question_from_node(self, node_id: str, code: str, prompt: str) -> bool:
        node = self.store.node_by_id(node_id)
        label = node["name"] if node else node_id
        out_id = self.say(
            f"🚨 需要你决策  #{code.upper()}\n[{label}]\n{truncate(prompt, 900)}\n"
            f"回复: #{code.upper()} 你的答复（或直接回复选项序号）"
        )
        self.store.put_question(code, node_id, prompt, out_id)
        if not out_id:
            # The code is indexed, but the human has not been told. The caller
            # must report this so the node can fall back to its own push.
            self.store.audit("hub", "question_push_failed", {"code": code.upper()})
        return bool(out_id)

    # -- inbound from WeChat --------------------------------------------
    def handle_human(self, msg_id: str, from_user: str, text: str, context_token: str = "",
                     ref_svr_id: str = "", ref_code: str = "", quoted: bool = False) -> str:
        if self.store.q1("SELECT 1 FROM seen WHERE msg_id=?", (msg_id,)):
            return "duplicate"
        self.store.x("INSERT INTO seen(msg_id,at) VALUES(?,?)", (msg_id, now()))
        if from_user not in self.owner_ids:
            self.store.audit(from_user, "unauthorized", {"text": truncate(text, 80)})
            return "unauthorized"
        if context_token:
            self._ctx[from_user] = context_token
            self.store.kv_set(f"ctx.{from_user}", context_token)
        raw = (text or "").strip()
        if not raw:
            return "empty"

        # --- hub commands ---
        if raw.startswith("/"):
            cmd, _, rest = raw[1:].partition(" ")
            cmd = cmd.lower().strip()
            if cmd in {"status", "machines", "pending", "help"}:
                self.say(self._hub_status(cmd))
                return f"hub:{cmd}"
            if cmd == "m":
                name, _, body = rest.partition(" ")
                node = self.store.node_by_name(name.strip())
                if not node:
                    self.say(f"没有叫 {name!r} 的机器。/machines 查看")
                    return "unknown_machine"
                self.store.push(node["id"], body, kind="human", frm=from_user)
                self.say(f"→ 已发给 {name}")
                return "routed"

        # --- quoted reply: binds to the exact pushed message (strongest) ---
        if quoted or ref_svr_id or ref_code:
            q = self.store.resolve_by_msg(ref_svr_id)
            if q is None and ref_code:
                q = self.store.resolve(ref_code)
            if q is not None:
                if q["state"] != "pending":
                    self.say(f"⚠️ 你引用的那条 (#{q['code']}) 已经处理过了，这条回复被忽略。")
                    return "already_handled"
                self.store.push(q["node_id"], raw, kind="answer", code=q["code"], frm=from_user)
                self.store.audit("hub", "answer_forwarded", {"code": q["code"], "source": "quote"})
                return "forwarded_quote"
            self.store.audit("hub", "quote_unknown", {"ref": ref_svr_id[:32], "code": ref_code})
            self.say("⚠️ 无法识别你引用的提问。请单独回复 #CODE 你的答复，或 /pending 查看。")
            return "unknown_quote"

        # --- decision by code ---
        m = CODE_RE.search(raw)
        if m:
            code = m.group(1).upper()
            q = self.store.resolve(code)
            if not q or q["state"] != "pending":
                self.say(f"没有 #{code} 这个待决策项。/pending 查看")
                return "unknown_code"
            answer = (raw[: m.start()] + raw[m.end():]).strip(" :：,，-")
            self.store.push(q["node_id"], answer or raw, kind="answer", code=code, frm=from_user)
            # The node owns question state: it closes the code once its own
            # router actually claimed the answer. Closing here made the hub
            # report success even when the answer was rejected on the machine
            # (e.g. "使用" is not one of allow/deny).
            return "forwarded"

        # --- a bare number answers the numbered list we just sent ---
        pending = self.store.pending()
        m_num = re.fullmatch(r"\s*(\d{1,2})\s*", raw)
        if m_num and len(pending) > 1:
            try:
                saved = json.loads(self.store.kv_get("pending_list", "") or "{}")
            except ValueError:
                saved = {}
            codes = list(saved.get("codes") or [])
            fresh = now() - float(saved.get("ts") or 0) < 600
            idx = int(m_num.group(1))
            if fresh and 1 <= idx <= len(codes):
                q = self.store.resolve(codes[idx - 1])
                if q is not None and q["state"] == "pending":
                    self.store.push(q["node_id"], raw, kind="answer", code=q["code"], frm=from_user)
                    return "forwarded_index"
            self.say("没看懂这个序号，请回复最新那条列表里的编号，或直接发 #编号。")
            return "bad_index"

        # --- bare text: only when exactly one question is pending ---
        if len(pending) == 1:
            q = pending[0]
            self.store.push(q["node_id"], raw, kind="answer", code=q["code"], frm=from_user)
            return "forwarded_sole"
        if len(pending) > 1:
            lines = [f"有 {len(pending)} 个待决策，回复序号即可:"] + [
                f"{i}) #{p['code']} [{self._name(p['node_id'])}] {truncate(p['prompt'] or '', 40)}"
                for i, p in enumerate(pending, 1)
            ]
            self.say("\n".join(lines))
            # The owner cannot quote a bot message on WeChat, so remember the
            # order we just showed: a bare number answers that item.
            self.store.kv_set(
                "pending_list",
                json_dumps({"ts": now(), "codes": [p["code"] for p in pending]}),
            )
            return "ambiguous"

        # --- otherwise: the chat layer may answer, it may never approve ---
        if self.chat is not None:
            try:
                reply = self.chat.reply(raw, node_names=[n["name"] for n in self.store.nodes()])
            except Exception as exc:
                self.store.audit("hub", "chat_error", {"error": str(exc)[:150]})
                reply = None
            if reply:
                self.say(reply)
                return "chat"
        self.say("当前没有待决策项，且聊天层未启用。发送 /machines 看机器，/help 看命令。")
        return "no_target"

    def _name(self, node_id: str) -> str:
        row = self.store.node_by_id(node_id)
        return row["name"] if row else node_id

    def _hub_status(self, cmd: str) -> str:
        nodes = self.store.nodes()
        if cmd == "help":
            return "/machines 机器列表\n/pending 待决策\n/m 机器名 内容 定向发送\n#编号 答复\n其余文字=聊天"
        if cmd == "pending":
            pend = self.store.pending()
            if not pend:
                return "没有待决策项"
            return "\n".join(f"#{p['code']} [{self._name(p['node_id'])}] {truncate(p['prompt'] or '', 50)}" for p in pend)
        if cmd == "machines":
            if not nodes:
                return "还没有机器注册"
            return "\n".join(
                f"{'🟢' if n['online'] else '⚪️'} {n['name']}  项目={','.join(json.loads(n['projects'] or '[]')) or '-'}  {int((now()-(n['last_seen'] or 0))//60)}分钟前"
                for n in nodes
            )
        online = sum(1 for n in nodes if n["online"])
        return (
            f"📊 Hub {VERSION}\n机器: {online}/{len(nodes)} 在线\n"
            f"待决策: {len(self.store.pending())}\n"
            f"微信: {'已连接' if self.client.token else '未登录'}"
        )

    # -- WeChat inbound loop --------------------------------------------
    def run_wechat(self) -> None:
        buf = self.store.kv_get("ilink.buf", "")
        timeout = 35.0
        failures = 0
        last_reap = 0.0
        while not self._stop.is_set():
            if now() - last_reap > 60:
                try:
                    self.store.reap(self.question_ttl_s)
                except Exception:
                    pass
                last_reap = now()
            try:
                resp = self.client.get_updates(buf, timeout=timeout)
                errcode = int(resp.get("errcode", 0) or 0)
                ret = int(resp.get("ret", 0) or 0)
                if errcode == -14 or ret == -14:
                    self.store.audit("hub", "token_expired", {})
                    log_stderr("[hub] WeChat token expired; run `arb login` on the hub host")
                    self._stop.wait(30)
                    continue
                if errcode or ret:
                    raise RuntimeError(f"getupdates ret={ret} errcode={errcode}")
                new_buf = str(resp.get("get_updates_buf") or "")
                if new_buf:
                    buf = new_buf
                    self.store.kv_set("ilink.buf", buf)
                suggested = int(resp.get("longpolling_timeout_ms") or 0)
                if suggested > 0:
                    timeout = max(5.0, min(60.0, suggested / 1000.0))
                for msg in resp.get("msgs") or []:
                    try:
                        n = int(self.store.kv_get("raw_samples", "0") or 0)
                    except ValueError:
                        n = 0
                    if n < 5:
                        self.store.kv_set("raw_samples", str(n + 1))
                        self.store.audit("hub", "inbound_raw", {"raw": json_dumps(msg)[:900]})
                    parsed = IlinkChannel._parse(msg)
                    if parsed is None:
                        continue
                    self.handle_human(
                        parsed.msg_id, parsed.from_user, parsed.text, parsed.context_token,
                        parsed.ref_svr_id, parsed.ref_code, parsed.quoted,
                    )
                failures = 0
            except Exception as exc:  # network hiccup: back off, keep serving nodes
                failures += 1
                self.store.audit("hub", "wechat_poll_error", {"error": str(exc)[:150], "n": failures})
                self._stop.wait(min(30.0, 1.5 ** min(failures, 8)))
        return

    def stop(self) -> None:
        self._stop.set()


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(hub: Hub) -> type[BaseHTTPRequestHandler]:
    store = hub.store

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"arb-hub/{VERSION}"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, code: int, payload: Any) -> None:
            body = json_dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

        def _node(self, body: dict[str, Any]) -> sqlite3.Row | None:
            node_id = str(body.get("node_id") or "")
            token = str(body.get("token") or self.headers.get("X-ARB-Node-Token") or "")
            row = store.auth(node_id, token) if node_id and token else None
            if row is None:
                self._json(401, {"ok": False, "error": "bad node credentials"})
            return row

        def _cookie(self) -> str:
            raw = self.headers.get("Cookie") or ""
            for part in raw.split(";"):
                name, _, value = part.strip().partition("=")
                if name == "arb_ui":
                    return value
            return ""

        def _send_html(self, code: int, text: str, set_cookie: str = "") -> None:
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if set_cookie:
                self.send_header("Set-Cookie", set_cookie)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/ui"):
                from urllib.parse import parse_qs, urlparse

                from .hub_ui import handle_ui

                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                status, html_text, cookie = handle_ui(store, hub, "GET", q, {}, self._cookie())
                self._send_html(status, html_text, cookie)
                return
            if self.path.startswith("/v1/health"):
                self._json(200, {
                    "ok": True, "version": VERSION,
                    "nodes": len(store.nodes()),
                    "pending": len(store.pending()),
                    "wechat_logged_in": bool(hub.client.token),
                })
                return
            self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?")[0].startswith("/ui"):
                from urllib.parse import parse_qs, urlparse

                from .hub_ui import handle_ui

                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
                form = {k: v[0] for k, v in parse_qs(raw).items()}
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                status, html_text, cookie = handle_ui(store, hub, "POST", q, form, self._cookie())
                self._send_html(status, html_text, cookie)
                return
            try:
                body = self._body()
            except ValueError:
                self._json(400, {"ok": False, "error": "bad json"})
                return
            path = self.path.split("?")[0]
            try:
                if path == "/v1/register":
                    name = str(body.get("name") or "").strip()
                    token = str(body.get("token") or "")
                    if not name or not token:
                        self._json(400, {"ok": False, "error": "name and token required"})
                        return
                    self._json(200, {"ok": True, **store.register(name, token)})
                    return
                node = self._node(body)
                if node is None:
                    return
                store.touch(node["id"], str(body.get("status") or "online"))
                if path == "/v1/poll":
                    msgs = store.take(node["id"], float(body.get("timeout") or 25))
                    store.touch(node["id"])
                    self._json(200, {"ok": True, "messages": msgs})
                    return
                if path == "/v1/send":
                    hub.push_from_node(node["id"], str(body.get("text") or ""), str(body.get("kind") or "notify"))
                    self._json(200, {"ok": True})
                    return
                if path == "/v1/question/close":
                    code = str(body.get("code") or "").upper()
                    if code:
                        store.close_question(code)
                    self._json(200, {"ok": True})
                    return
                if path == "/v1/question":
                    code = str(body.get("code") or "").upper()
                    if not CODE_RE.fullmatch(f"#{code}"):
                        self._json(400, {"ok": False, "error": "bad code"})
                        return
                    pushed = hub.question_from_node(node["id"], code, str(body.get("prompt") or ""))
                    self._json(200, {
                        "ok": pushed, "code": code,
                        "error": "" if pushed else "hub could not push to WeChat",
                    })
                    return
                if path == "/v1/snapshot":
                    store.x(
                        "UPDATE nodes SET snapshot=?, projects=? WHERE id=?",
                        (json_dumps(body.get("snapshot") or {}), json_dumps(body.get("projects") or []), node["id"]),
                    )
                    self._json(200, {"ok": True})
                    return
                if path == "/v1/heartbeat":
                    self._json(200, {"ok": True})
                    return
                self._json(404, {"ok": False, "error": "not found"})
            except PermissionError as exc:
                self._json(403, {"ok": False, "error": str(exc)})
            except Exception as exc:  # pragma: no cover
                store.audit("hub", "handler_error", {"path": path, "error": str(exc)[:200]})
                self._json(500, {"ok": False, "error": str(exc)[:200]})

    return Handler


def serve_hub(cfg_path: str) -> int:
    """Entry point used by `arb hub serve` on the ECS."""
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8-sig"))
    store = HubStore(cfg["state"])
    creds = json.loads(Path(cfg["ilink_creds"]).read_text(encoding="utf-8-sig"))
    client = IlinkClient(creds["base_url"], creds["token"])
    hub = Hub(store, client, list(cfg["owner_ids"]), cfg.get("name", "hub"))
    hub.question_ttl_s = float(cfg.get("question_ttl_s", 1800))
    from .hub_ui import DEFAULT_PASSWORD, DEFAULT_USER

    hub.ui_user = str(cfg.get("ui_user") or DEFAULT_USER)
    hub.ui_password = str(cfg.get("ui_password") or DEFAULT_PASSWORD)

    key_file = cfg.get("deepseek_key_file")
    if key_file and Path(key_file).exists():
        from .hub_chat import Advisor

        api_key = Path(key_file).read_text(encoding="utf-8").strip()
        if api_key:
            # Read-only advisor: it is only consulted for free text, never for a
            # decision, and it cannot approve anything (see hub_chat docstring).
            hub.chat = Advisor(
                api_key,
                store,
                model=cfg.get("deepseek_model", "deepseek-chat"),
                base_url=cfg.get("deepseek_base_url", "https://api.deepseek.com"),
            )
            log_stderr(f"[hub] chat advisor enabled (model={hub.chat.model})")

    port = int(cfg.get("port", DEFAULT_PORT))
    httpd = HubServer(("0.0.0.0", port), make_handler(hub))
    if cfg.get("tls_cert") and cfg.get("tls_key"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cfg["tls_cert"], cfg["tls_key"])
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        log_stderr(f"[hub] TLS enabled, listening on 0.0.0.0:{port}")
    else:
        log_stderr(f"[hub] WARNING: plaintext, listening on 0.0.0.0:{port}")

    threading.Thread(target=hub.run_wechat, name="wechat", daemon=True).start()

    if cfg.get("proactive", True):
        from .hub_proactive import start_thread

        start_thread(hub, store, cfg)
    log_stderr(f"[hub] state={cfg['state']} nodes={len(store.nodes())} owner={cfg['owner_ids']}")
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(serve_hub(sys.argv[1] if len(sys.argv) > 1 else "/etc/arb/hub.json"))
