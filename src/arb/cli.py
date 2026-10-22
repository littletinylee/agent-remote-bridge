"""Command line interface.

Exit codes are meaningful so hooks and scripts can branch on them:
    0  success / answered
    2  usage error
    3  question timed out (or non-approval)
    4  bridge unavailable
    5  unknown question / cannot deliver
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import socket
import sys
import time
import traceback
from typing import Any

from . import agents
from .client import ApiError, Client, DaemonUnavailable
from .config import Config, clear_ilink, load, load_ilink, save_ilink
from .util import json_dumps, log_stderr, now, one_line, truncate

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_TIMEOUT = 3
EXIT_UNAVAILABLE = 4
EXIT_UNKNOWN = 5


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _cfg() -> Config:
    cfg = load()
    cfg.ensure_home()
    return cfg


# --------------------------------------------------------------------------
# init / login / serve
# --------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    cfg = _cfg()
    cfg.save()
    print(f"配置目录: {cfg.home}")
    print(f"配置文件: {cfg.config_path}")
    print(f"数据库:   {cfg.db_path}")
    print(f"本机端口: {cfg.port}")
    print(f"绑定码:   {cfg.pairing_code}")
    print()
    print("下一步:")
    print("  1) arb login      用个人微信扫码绑定机器人")
    print("  2) arb start      启动桥接守护进程")
    print("  3) 在微信里给机器人发: /pair " + cfg.pairing_code)
    return EXIT_OK


def cmd_login(args: argparse.Namespace) -> int:
    from .channel.ilink import IlinkError, login

    cfg = _cfg()
    try:
        creds = login(
            cfg,
            timeout_s=args.timeout,
            qr_png=args.qr_png or "",
            show_qr=not args.no_terminal_qr,
        )
    except IlinkError as exc:
        log_stderr(f"登录失败: {exc}")
        return EXIT_UNAVAILABLE
    except KeyboardInterrupt:
        log_stderr("已取消")
        return EXIT_USAGE

    if creds.bot_user_id and creds.bot_user_id not in cfg.owner_ids:
        cfg.owner_ids.append(creds.bot_user_id)
        cfg.save()
        print(f"已自动登记你的微信 ID: {creds.bot_user_id}")
    print(f"凭据已保存: {cfg.ilink_path}")
    print()
    print("下一步:")
    print("  1) arb start                       启动桥接")
    print("  2) 在微信中打开刚出现的机器人会话，发送: /pair " + cfg.pairing_code)
    print("  3) 收到绑定成功提示后，运行: arb test-push")
    return EXIT_OK


def cmd_logout(args: argparse.Namespace) -> int:
    cfg = _cfg()
    clear_ilink(cfg)
    print("已清除本机微信凭据。重新登录请运行 arb login。")
    return EXIT_OK


def cmd_start(args: argparse.Namespace) -> int:
    cfg = _cfg()
    client = Client(cfg)
    try:
        info = client.ensure(spawn=True, wait_s=args.wait)
    except DaemonUnavailable as exc:
        log_stderr(f"启动失败: {exc}")
        log_stderr(f"日志: {cfg.log_path}")
        return EXIT_UNAVAILABLE
    ch = info.get("channel") or {}
    print(f"桥接已启动: http://127.0.0.1:{cfg.port}")
    print(f"通道: {ch.get('name')} / {'在线' if ch.get('healthy') else '离线'}")
    if not ch.get("logged_in"):
        print("提示: 微信尚未登录，运行 arb login")
    print(f"日志: {cfg.log_path}")
    return EXIT_OK


def cmd_stop(args: argparse.Namespace) -> int:
    cfg = _cfg()
    client = Client(cfg)
    try:
        client._call("POST", "/api/shutdown", {})
    except (DaemonUnavailable, ApiError):
        print("桥接未在运行。")
        return EXIT_OK
    print("已请求停止。")
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    from .daemon import run_daemon

    cfg = _cfg()
    if args.channel:
        cfg.channel = args.channel
    if args.port:
        cfg.port = args.port
    cfg.save()
    return run_daemon(cfg)


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp import main as mcp_main

    return mcp_main(args.agent or "")


# --------------------------------------------------------------------------
# status / doctor
# --------------------------------------------------------------------------
def cmd_status(args: argparse.Namespace) -> int:
    cfg = _cfg()
    client = Client(cfg)
    info = client.health(timeout=2.0)
    print(f"配置目录: {cfg.home}")
    print(f"绑定码:   {cfg.pairing_code}")
    print(f"已授权微信: {', '.join(cfg.owner_ids) or '(未绑定)'}")
    if info is None:
        print("守护进程: 未运行 (arb start)")
        return EXIT_OK
    ch = info.get("channel") or {}
    stats = info.get("stats") or {}
    print(f"守护进程: 运行中 {info.get('uptime_s')}s  http://127.0.0.1:{cfg.port}")
    print(f"通道:     {ch.get('name')} / {'健康' if ch.get('healthy') else '异常'}  已登录={ch.get('logged_in')}")
    if ch.get("auth_error"):
        print(f"认证问题: {ch.get('auth_error')}")
    elif ch.get("last_error"):
        print(f"最近错误: {one_line(ch.get('last_error'), 120)}")
    print(
        f"会话: {stats.get('active_sessions')} 活跃 / {stats.get('sessions')} 总计   "
        f"问题: {stats.get('pending_questions')} 待处理 / {stats.get('answered')} 已回答 / {stats.get('expired')} 超时"
    )
    outbox = info.get("outbox") or {}
    print(f"待发消息: {outbox.get('pending')}  (发送失败重试中: {outbox.get('failing')})")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _cfg()
    ok = True

    def line(flag: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok = ok and flag
        print(f"{'✓' if flag else '✗'} {label}{('  — ' + detail) if detail else ''}")

    line(sys.version_info >= (3, 9), "Python >= 3.9", sys.version.split()[0])
    line(cfg.config_path.exists(), "配置文件存在", str(cfg.config_path))
    try:
        from .store import Store

        store = Store(cfg.db_path)
        store.audit("doctor", "cli", {"ok": True})
        line(True, "SQLite 可读写", str(cfg.db_path))
    except Exception as exc:
        line(False, "SQLite 可读写", str(exc))

    creds = load_ilink(cfg)
    line(creds.valid, "微信凭据", "已登录" if creds.valid else "未登录 (arb login)")

    # connectivity
    try:
        from .channel.ilink import IlinkClient

        api = IlinkClient(cfg.ilink_base_url, creds.token)
        start = time.time()
        resp = api.get_qrcode(cfg.ilink_bot_type)
        dt = int((time.time() - start) * 1000)
        line(int(resp.get("ret", 0) or 0) == 0, "iLink 接口可达", f"{dt}ms ret={resp.get('ret')}")
    except Exception as exc:
        line(False, "iLink 接口可达", str(exc)[:100])

    client = Client(cfg)
    info = client.health()
    line(info is not None, "守护进程", "运行中" if info else "未运行 (arb start)")
    if info:
        ch = info.get("channel") or {}
        line(bool(ch.get("healthy")), "通道健康", f"{ch.get('name')} {ch.get('last_error') or ''}".strip())
        line(bool(info.get("owner_ids")), "已绑定微信用户", ", ".join(info.get("owner_ids") or []) or "未绑定")

    try:
        import qrcode  # noqa: F401

        line(True, "终端二维码渲染 (可选依赖)", "已安装")
    except ImportError:
        print("… 终端二维码渲染 (可选依赖)  — 未安装，arb login 会给出备用链接")

    print()
    print("结论:", "全部通过" if ok else "存在问题，请按上面提示处理")
    return EXIT_OK if ok else EXIT_USAGE


# --------------------------------------------------------------------------
# ask / notify / pending / answer
# --------------------------------------------------------------------------
def cmd_ask(args: argparse.Namespace) -> int:
    cfg = _cfg()
    client = Client(cfg)
    ctx = agents.generic_context(args.agent or "cli", project=args.project, label=args.label)
    try:
        client.ensure()
        sess = client.session(
            agent=ctx["agent"], project=ctx["project"], cwd=ctx["cwd"], label=ctx["label"], key=ctx["key"]
        )
        resp = client.ask(
            session_id=sess["id"],
            prompt=args.prompt,
            options=args.option or [],
            timeout_s=args.timeout,
            dangerous=args.dangerous,
        )
    except (DaemonUnavailable, ApiError) as exc:
        log_stderr(f"桥接不可用: {exc}")
        return EXIT_UNAVAILABLE
    q = resp["question"]
    print(f"# 已发送问题 #{q['code']}，等待微信回复…", file=sys.stderr)
    q = client.wait(q["id"], args.timeout)["question"]
    print(json_dumps({"status": q["state"], "code": q["code"], "answer": q["answer"], "dangerous": q["dangerous"]}))
    if q["state"] == "answered":
        return EXIT_OK
    if q["state"] == "expired":
        return EXIT_TIMEOUT
    return EXIT_USAGE


def cmd_notify(args: argparse.Namespace) -> int:
    cfg = _cfg()
    client = Client(cfg)
    ctx = agents.generic_context(args.agent or "cli", project=args.project, label=args.label)
    try:
        client.ensure()
        sess = client.session(
            agent=ctx["agent"], project=ctx["project"], cwd=ctx["cwd"], label=ctx["label"], key=ctx["key"]
        )
        client.notify(session_id=sess["id"], title=args.title, body=args.body or "", kind=args.kind)
    except (DaemonUnavailable, ApiError) as exc:
        log_stderr(f"桥接不可用: {exc}")
        return EXIT_UNAVAILABLE
    return EXIT_OK


def cmd_test_push(args: argparse.Namespace) -> int:
    cfg = _cfg()
    client = Client(cfg)
    try:
        client.ensure()
        sess = client.session(agent="cli", project="agent-remote-bridge", cwd=cfg.home.as_posix(), label="自检")
        client.notify(
            session_id=sess["id"],
            title="测试推送",
            body="如果你在微信里看到这条消息，说明主动推送通道工作正常。\n回复 /status 可以做一次双向验证。",
            kind="info",
        )
    except (DaemonUnavailable, ApiError) as exc:
        log_stderr(f"推送失败: {exc}")
        return EXIT_UNAVAILABLE
    print("已入队测试推送，等待发送…")
    deadline = time.time() + 15
    while time.time() < deadline:
        info = client.health() or {}
        if (info.get("outbox") or {}).get("pending", 0) == 0:
            print("已发送（若微信未收到，运行 arb doctor 检查通道）")
            return EXIT_OK
        time.sleep(0.5)
    print("仍在队列中，运行 arb status 查看通道状态")
    return EXIT_TIMEOUT


def cmd_pending(args: argparse.Namespace) -> int:
    cfg = _cfg()
    client = Client(cfg)
    try:
        pending = client.pending()["pending"]
    except (DaemonUnavailable, ApiError) as exc:
        log_stderr(f"桥接不可用: {exc}")
        return EXIT_UNAVAILABLE
    if not pending:
        print("没有待处理问题。")
        return EXIT_OK
    for q in pending:
        left = max(0, int(q["expires_at"] - now()))
        print(f"#{q['code']}  {q['prompt'][:80]}  (剩 {left}s, 危险={bool(q['dangerous'])})")
    return EXIT_OK


def cmd_answer(args: argparse.Namespace) -> int:
    cfg = _cfg()
    client = Client(cfg)
    try:
        resp = client.answer(args.code, args.text, by="cli")
    except (DaemonUnavailable, ApiError) as exc:
        log_stderr(f"桥接不可用: {exc}")
        return EXIT_UNAVAILABLE
    print(resp.get("outcome", ""))
    return EXIT_OK


def cmd_sessions(args: argparse.Namespace) -> int:
    cfg = _cfg()
    client = Client(cfg)
    try:
        sessions = client.sessions()["sessions"]
    except (DaemonUnavailable, ApiError) as exc:
        log_stderr(f"桥接不可用: {exc}")
        return EXIT_UNAVAILABLE
    for s in sessions:
        print(f"{s['id'][:14]:14}  {s['agent']:11}  {s['project']:16}  {s['status']:8}  {time.strftime('%H:%M:%S', time.localtime(s['last_seen']))}")
    return EXIT_OK


def cmd_paircode(args: argparse.Namespace) -> int:
    cfg = _cfg()
    print(cfg.pairing_code)
    return EXIT_OK


# --------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------
TOOL_DEFAULT = ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"]


def _tool_gated(cfg: Config, tool: str) -> bool:
    if not tool:
        return False
    for pat in cfg.approval_tools:
        if pat == tool:
            return True
        try:
            if re.fullmatch(pat, tool):
                return True
        except re.error:
            continue
    return False


def _emit_hook_decision(decision: str, reason: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def cmd_hook(args: argparse.Namespace) -> int:
    """Entry point for agent hooks. Reads the vendor payload from stdin."""
    raw = sys.stdin.read()
    payload: dict[str, Any] = {}
    if raw.strip():
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw}
    if not isinstance(payload, dict):
        payload = {"raw": raw}

    cfg = _cfg()
    agent = args.agent or agents.sniff_agent(payload)

    if agent == agents.AGENT_CODEX:
        ev = agents.codex_event(payload, cwd=os.getcwd())
        return _push_event(cfg, ev, hook_mode=True)

    if agent == agents.AGENT_CLAUDE:
        ev = agents.claude_event(payload, cwd=os.getcwd())
        if ev.event == "PreToolUse":
            return _claude_approval(cfg, ev)
        return _push_event(cfg, ev, hook_mode=True)

    # generic
    ev = agents.AgentEvent(
        agent=agent,
        event=str(payload.get("event") or "notify"),
        project=agents.project_from_cwd(os.getcwd()),
        cwd=os.getcwd(),
        kind=str(payload.get("kind") or "info"),
        title=str(payload.get("title") or ""),
        body=str(payload.get("body") or ""),
    )
    return _push_event(cfg, ev, hook_mode=True)


def _ensure_session(cfg: Config, ev: agents.AgentEvent) -> tuple[Client, dict[str, Any]] | None:
    client = Client(cfg)
    try:
        client.ensure()
        sess = client.session(
            agent=ev.agent,
            project=ev.project,
            cwd=ev.cwd,
            label=ev.label,
            parent_id=ev.parent_id,
            key=ev.key,
            meta={"event": ev.event},
        )
        return client, sess
    except (DaemonUnavailable, ApiError) as exc:
        log_stderr(f"[arb hook] bridge unavailable: {exc}")
        return None


def _push_event(cfg: Config, ev: agents.AgentEvent, hook_mode: bool = False) -> int:
    if not ev.title:
        return EXIT_OK
    pair = _ensure_session(cfg, ev)
    if pair is None:
        return EXIT_OK if hook_mode else EXIT_UNAVAILABLE
    client, sess = pair
    try:
        client.notify(
            session_id=sess["id"],
            title=ev.title,
            body=ev.body,
            kind=ev.kind,
        )
        if ev.event == "SessionEnd":
            client.close_session(sess["id"])
    except (DaemonUnavailable, ApiError) as exc:
        log_stderr(f"[arb hook] notify failed: {exc}")
        return EXIT_OK if hook_mode else EXIT_UNAVAILABLE
    return EXIT_OK


def _claude_approval(cfg: Config, ev: agents.AgentEvent) -> int:
    """Remote approval gate for Claude Code PreToolUse hooks."""
    if cfg.approval_mode != "approve" or not _tool_gated(cfg, ev.tool):
        return EXIT_OK  # not gated: let Claude Code's own permission flow handle it

    pair = _ensure_session(cfg, ev)
    if pair is None:
        # Fail-safe: never auto-allow when the bridge is down.
        _emit_hook_decision("ask", "远程桥接不可用，回退到本机确认")
        return EXIT_OK
    client, sess = pair
    try:
        resp = client.ask(
            session_id=sess["id"],
            prompt=agents.approval_prompt(ev),
            options=["allow", "deny"],
            timeout_s=cfg.approval_timeout_s,
            dangerous=True,
            kind="approval",
            context={"tool": ev.tool},
        )
        q = resp["question"]
        q = client.wait(q["id"], cfg.approval_timeout_s)["question"]
    except (DaemonUnavailable, ApiError) as exc:
        log_stderr(f"[arb hook] approval unavailable: {exc}")
        _emit_hook_decision("ask", "远程桥接不可用，回退到本机确认")
        return EXIT_OK

    if q["state"] == "answered":
        decision = "allow" if str(q["answer"]).lower() in {"allow", "yes", "允许", "同意"} else "deny"
        _emit_hook_decision(decision, f"微信远程{'批准' if decision == 'allow' else '拒绝'} #{q['code']}")
        return EXIT_OK
    if q["state"] == "cancelled":
        _emit_hook_decision("deny", f"远程取消 #{q['code']}")
        return EXIT_OK
    # expired / pending -> configured fallback
    if cfg.on_timeout == "allow":
        _emit_hook_decision("allow", "等待远程批准超时（配置为 allow）")
    elif cfg.on_timeout == "ask":
        _emit_hook_decision("ask", "远程批准超时，回退到本机确认")
    else:
        _emit_hook_decision("deny", f"等待远程批准超时，默认拒绝 #{q['code']}")
    return EXIT_OK


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arb", description="Agent Remote Bridge — AI agent remote control over WeChat")
    p.add_argument("--version", action="version", version="agent-remote-bridge 1.0.0")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="创建配置目录").set_defaults(func=cmd_init)

    sp = sub.add_parser("login", help="扫码登录微信机器人")
    sp.add_argument("--timeout", type=int, default=300)
    sp.add_argument("--qr-png", default="", help="把二维码保存为 PNG（便于在图形界面中展示）")
    sp.add_argument("--no-terminal-qr", action="store_true", help="不在终端绘制 ASCII 二维码")
    sp.set_defaults(func=cmd_login)

    sub.add_parser("logout", help="清除微信凭据").set_defaults(func=cmd_logout)

    sp = sub.add_parser("start", help="后台启动桥接")
    sp.add_argument("--wait", type=float, default=15.0)
    sp.set_defaults(func=cmd_start)

    sub.add_parser("stop", help="停止桥接").set_defaults(func=cmd_stop)

    sp = sub.add_parser("serve", help="前台运行桥接守护进程")
    sp.add_argument("--channel", choices=["ilink", "console"])
    sp.add_argument("--port", type=int)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("mcp", help="以 MCP stdio 服务器运行（供 Agent 调用）")
    sp.add_argument("--agent", default="")
    sp.set_defaults(func=cmd_mcp)

    sub.add_parser("status", help="查看状态").set_defaults(func=cmd_status)
    sub.add_parser("doctor", help="自检").set_defaults(func=cmd_doctor)
    sub.add_parser("paircode", help="显示绑定码").set_defaults(func=cmd_paircode)
    sub.add_parser("test-push", help="发送一条测试推送").set_defaults(func=cmd_test_push)
    sub.add_parser("pending", help="列出待处理问题").set_defaults(func=cmd_pending)
    sub.add_parser("sessions", help="列出会话").set_defaults(func=cmd_sessions)

    sp = sub.add_parser("ask", help="发送问题并等待微信回复")
    sp.add_argument("prompt")
    sp.add_argument("--option", action="append", help="可选项，可重复")
    sp.add_argument("--timeout", type=int, default=None)
    sp.add_argument("--dangerous", action="store_true")
    sp.add_argument("--agent", default="")
    sp.add_argument("--project", default=None)
    sp.add_argument("--label", default=None)
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("notify", help="推送通知")
    sp.add_argument("title")
    sp.add_argument("--body", default="")
    sp.add_argument("--kind", default="info", choices=["start", "progress", "done", "error", "attention", "info"])
    sp.add_argument("--agent", default="")
    sp.add_argument("--project", default=None)
    sp.add_argument("--label", default=None)
    sp.set_defaults(func=cmd_notify)

    sp = sub.add_parser("answer", help="本机回答问题")
    sp.add_argument("code")
    sp.add_argument("text")
    sp.set_defaults(func=cmd_answer)

    sp = sub.add_parser("hook", help="Agent hook 入口（从 stdin 读取事件）")
    sp.add_argument("agent", nargs="?", default="")
    sp.set_defaults(func=cmd_hook)

    return p


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # pragma: no cover - last resort
        log_stderr(f"错误: {exc}")
        if os.environ.get("ARB_DEBUG"):
            log_stderr(traceback.format_exc())
        return EXIT_USAGE
