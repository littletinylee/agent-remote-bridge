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
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from . import __version__, agents
from .client import ApiError, Client, DaemonUnavailable
from .config import Config, clear_ilink, load, load_ilink
from .util import gen_secret, json_dumps, log_stderr, now, one_line

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
    state = ch.get("state") or ("healthy" if ch.get("healthy") else "down")
    state_text = {"healthy": "健康", "starting": "启动中（等待首次长轮询）"}.get(state, "异常")
    print(f"守护进程: 运行中 {info.get('uptime_s')}s  http://127.0.0.1:{cfg.port}")
    print(f"通道:     {ch.get('name')} / {state_text}  已登录={ch.get('logged_in')}")
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

    line(sys.version_info >= (3, 10), "Python >= 3.10", sys.version.split()[0])
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
    state = q["state"]
    print(json_dumps({"status": state, "code": q["code"], "answer": q["answer"], "dangerous": q["dangerous"]}))
    if state == "answered":
        return EXIT_OK
    if state == "pending":
        # The waiter gave up but the question is still live in WeChat (it can
        # still be answered there; this process simply will not see it).
        print(
            f"# 等待超时，#{q['code']} 仍留在微信里；之后回答请用 arb pending 查看结果。",
            file=sys.stderr,
        )
        return EXIT_TIMEOUT
    if state == "expired":
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


def cmd_service(args: argparse.Namespace) -> int:
    """Logon autostart for the daemon.

    Windows: a hidden launcher in the user's Startup folder - no admin rights
    needed (schtasks ONLOGON requires elevation). Linux: prints a systemd unit
    for the operator to install, since writing it needs root.
    """
    cfg = _cfg()
    if os.name != "nt":
        print("arb service 只支持 Windows 自动安装。Linux 请用 systemd：")
        print()
        print("[Unit]")
        print("Description=Agent Remote Bridge")
        print("After=network-online.target")
        print()
        print("[Service]")
        print(f"Environment=ARB_HOME={cfg.home}")
        print(f"ExecStart={sys.executable} -m arb serve")
        print("Restart=always")
        print("RestartSec=10")
        print()
        print("[Install]")
        print("WantedBy=default.target")
        return EXIT_USAGE

    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    launcher = startup / "AgentRemoteBridge.vbs"
    wrapper = cfg.home / "arb-serve.cmd"
    repo_src = Path(__file__).resolve().parents[1].as_posix()

    if args.action == "install":
        cfg.ensure_home()
        wrapper.write_text(
            "@echo off\n"
            f'if not defined ARB_HOME set "ARB_HOME={cfg.home}"\n'
            f'set "PYTHONPATH={repo_src};%PYTHONPATH%"\n'
            f'"{sys.executable}" -m arb serve\n',
            encoding="ascii",
        )
        startup.mkdir(parents=True, exist_ok=True)
        # 0 = hidden window, False = do not wait
        launcher.write_text(
            'CreateObject("WScript.Shell").Run """' + str(wrapper) + '""", 0, False\n',
            encoding="ascii",
        )
        print("✅ 已安装登录自启（无需管理员权限）")
        print(f"   启动脚本: {wrapper}")
        print(f"   自启项:   {launcher}")
        print("   下次登录 Windows 时会隐藏运行 arb serve。")
        print("   现在启动: arb start    移除: arb service uninstall")
        if not cfg.owner_ids:
            print("   提醒: 还没有绑定微信，先 arb login 并 /pair。")
        return EXIT_OK

    if args.action == "uninstall":
        removed = False
        if launcher.exists():
            launcher.unlink()
            removed = True
        print("已移除登录自启。" if removed else "没有找到自启项。")
        return EXIT_OK

    if launcher.exists():
        print(f"登录自启: 已安装 → {launcher}")
        print(f"启动脚本: {wrapper} ({'存在' if wrapper.exists() else '缺失，请重新 install'})")
    else:
        print("登录自启: 未安装 (arb service install)")
    return EXIT_OK


def cmd_hooks(args: argparse.Namespace) -> int:
    """Install/remove the Claude Code hook entries (reversible, idempotent)."""
    from . import install_hooks

    cfg = _cfg()
    path = Path(args.settings) if args.settings else install_hooks.default_settings_path()
    if args.action == "install":
        try:
            res = install_hooks.install(path, cfg.home, sys.executable, dry_run=args.dry_run)
        except ValueError as exc:
            log_stderr(str(exc))
            return EXIT_USAGE
        if not res["changed"]:
            print(f"已是最新，无需修改: {path}")
        else:
            print(("预览 " if args.dry_run else "已写入 ") + str(path))
            print("新增事件: " + ", ".join(res["added"]))
            if res.get("backup"):
                print(f"原文件备份: {res['backup']}")
        if not args.dry_run:
            print(f"钩子启动器: {res['wrappers']['hook']}")
            print("让 Claude 主动提问，再执行:")
            print(f'  claude mcp add arb -- {res["wrappers"]["mcp"]} --agent claude-code')
        print("生效方式: 下次启动 Claude Code。撤销: arb hooks uninstall")
        return EXIT_OK
    if args.action == "uninstall":
        res = install_hooks.uninstall(path, dry_run=args.dry_run)
        if not res["changed"]:
            print(f"没有找到本项目的 hook: {path}")
            return EXIT_OK
        print(("预览 " if args.dry_run else "已移除 ") + str(path))
        print("移除事件: " + ", ".join(res["removed"]))
        if res.get("backup"):
            print(f"原文件备份: {res['backup']}")
        return EXIT_OK

    res = install_hooks.status(path)
    if not res["installed"]:
        print(f"Claude Code hooks: 未安装 ({path})")
        print("安装: arb hooks install")
        return EXIT_OK
    print(f"Claude Code hooks: 已安装 → {res['path']}")
    for ev in res["events"]:
        print(f"  · {ev['event']} ({ev['entries']} 条)")
    print("MCP: claude mcp add arb -- " + install_hooks.mcp_command(cfg.home))
    return EXIT_OK


def cmd_node(args: argparse.Namespace) -> int:
    """Wire every agent on this machine to the bridge, each in its native form."""
    from . import install_hooks, node_setup

    cfg = _cfg()
    only = {x.strip().lower() for x in (args.only or "").split(",") if x.strip()} or {"codex", "dsh", "claude"}
    dry = args.dry_run

    if args.action == "status":
        st = node_setup.status(cfg.home, Path(args.dsh_profile) if args.dsh_profile else None)
        print(f"配置目录: {cfg.home}")
        print(f"Codex : 插件文件={'是' if st['codex']['plugin_files'] else '否'}  CLI={st['codex']['cli'] or '未找到'}")
        print(f"DSH   : profile={st['dsh']['profile']}  已接入={'是' if st['dsh']['installed'] else '否'}")
        print(f"Claude: settings={st['claude']['settings']}  存在={'是' if st['claude']['exists'] else '否'}")
        print("安装: arb node setup    移除: arb node uninstall")
        return EXIT_OK

    if args.action == "uninstall":
        out: list[str] = []
        if "dsh" in only:
            res = node_setup.uninstall_dsh(Path(args.dsh_profile) if args.dsh_profile else None)
            out.append(f"DSH: {'已移除' if res.get('changed') else '未接入'} {res.get('profile', '')}")
        if "codex" in only:
            codex = node_setup.find_codex()
            if codex:
                proc = subprocess.run(
                    [codex, "plugin", "remove", f"{node_setup.PLUGIN_NAME}@{node_setup.MARKET_NAME}"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                )
                out.append(f"Codex: rc={proc.returncode} {(proc.stdout or proc.stderr or '').strip()[-120:]}")
            else:
                out.append("Codex: 未找到 CLI")
        if "claude" in only:
            res = install_hooks.uninstall(Path(args.settings) if args.settings else install_hooks.default_settings_path())
            out.append(f"Claude hooks: {'已移除' if res.get('changed') else '未安装'}")
        print("\n".join(out))
        return EXIT_OK

    # setup
    print(f"配置目录: {cfg.home}{'  (预览，不写盘)' if dry else ''}")
    if not dry:
        node_setup.write_wrappers(cfg.home, sys.executable)

    if "codex" in only:
        codex = node_setup.find_codex()
        res = node_setup.install_codex(cfg.home, sys.executable, codex, dry_run=dry)
        print(f"\n[Codex] CLI={codex or '未找到'}")
        for w in res["written"]:
            print(f"  写入 {w}")
        for c in res["commands"]:
            print(f"  {'✓' if c['rc'] == 0 else '✗'} {c['cmd']}")
        for e in res["errors"]:
            print(f"  ! {e}")

    if "dsh" in only:
        profile = Path(args.dsh_profile) if args.dsh_profile else None
        res = node_setup.install_dsh(cfg.home, sys.executable, profile, dry_run=dry)
        if res.get("error"):
            print(f"\n[DSH] {res['error']}")
        elif res.get("changed"):
            verb = "将追加（预览）" if dry else "已追加插件条目到"
            print(f"\n[DSH] {verb} {res['profile']}")
            if res.get("backup"):
                print(f"  备份 {res['backup']}")
            if res.get("block"):
                print("  将追加:\n" + "\n".join("    " + ln for ln in res["block"].splitlines()))
            print("  生效方式: 重启 DeepSeek Harness；模型工具里会出现 mcp__arb__ask_human")
        else:
            print(f"\n[DSH] 无需修改（{res.get('reason', '已是最新')}）")

    if "claude" in only:
        try:
            res = install_hooks.install(
                Path(args.settings) if args.settings else install_hooks.default_settings_path(),
                cfg.home, sys.executable, dry_run=dry,
            )
            print(f"\n[Claude Code] {'新增 ' + ', '.join(res['added']) if res.get('changed') else '已是最新'}")
        except ValueError as exc:
            print(f"\n[Claude Code] {exc}")

    print("\n下一步: arb start（或已在运行则无需操作）")
    return EXIT_OK


def cmd_node_join(args: argparse.Namespace) -> int:
    """Register this machine with a hub and switch the daemon to relay mode."""
    import ssl
    import urllib.request

    cfg = _cfg()
    base = args.hub.rstrip("/")
    name = args.name or (os.environ.get("COMPUTERNAME") or "node").lower()
    token = args.token or gen_secret(24)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # self-signed hub; pinned below when given

    def post(path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            base + path, data=json_dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            return json.loads(resp.read().decode() or "{}")

    try:
        with urllib.request.urlopen(base + "/v1/health", timeout=15, context=ctx) as resp:
            health = json.loads(resp.read().decode())
    except Exception as exc:
        log_stderr(f"连不上 Hub {base}: {exc}")
        return EXIT_UNAVAILABLE
    print(f"Hub 可达: {json_dumps(health)}")

    if args.fingerprint:
        import hashlib
        import socket
        with socket.create_connection((base.split("//")[-1].split(":")[0], int(base.rsplit(":", 1)[-1])), timeout=15) as s:
            with ctx.wrap_socket(s) as ss:
                seen = hashlib.sha256(ss.getpeercert(binary_form=True) or b"").hexdigest().upper()
        wanted = args.fingerprint.replace(":", "").upper()
        if seen != wanted:
            log_stderr(f"证书指纹不匹配: {seen} != {wanted}")
            return EXIT_UNAVAILABLE
        print(f"证书指纹校验通过: {seen[:16]}…")
        cfg.hub_fingerprint = wanted

    try:
        reg = post("/v1/register", {"name": name, "token": token, "join_code": args.code})
    except Exception as exc:
        log_stderr(f"注册失败: {exc}")
        return EXIT_UNAVAILABLE

    cfg.channel = "relay"
    cfg.hub_url = base
    cfg.hub_node_name = name
    cfg.hub_token = token
    cfg.hub_node_id = str(reg.get("node_id") or "")
    cfg.hub_join_code = args.code
    cfg.save()
    print(f"已注册为 {name} ({cfg.hub_node_id})，通道切换为 relay")
    print("下一步: arb node setup  (装各家 Agent 插件)  然后 arb start")
    return EXIT_OK

def cmd_hub(args: argparse.Namespace) -> int:
    """Run the relay hub. This is what systemd starts on the server."""
    from . import hub

    return hub.serve_hub(args.config)


def cmd_approval(args: argparse.Namespace) -> int:
    """Toggle the remote approval gate without hand-editing JSON."""
    cfg = _cfg()
    if args.mode == "status" or args.mode is None:
        print(f"approval_mode = {cfg.approval_mode}")
        print(f"拦截工具: {', '.join(cfg.approval_tools)}")
        print(f"超时: {cfg.approval_timeout_s}s → {cfg.on_timeout}")
        print("说明: approve 模式下，上面这些工具每次执行前都会先发微信问你。")
        return EXIT_OK
    cfg.approval_mode = "approve" if args.mode == "on" else "notify"
    cfg.save()
    print(f"approval_mode = {cfg.approval_mode}")
    if cfg.approval_mode == "approve":
        print("从现在起，命中的工具调用会先发微信等你批准（下次 hook 调用即生效，无需重启）。")
        print("⚠️ 请确保 arb 守护进程在运行、且手机能收到消息，否则该操作会回退到本机确认。")
    else:
        print("已关闭远程审批：只推送通知，工具调用由本机权限流程处理。")
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
    """Entry point for agent hooks.

    Vendors differ in how they hand over the event:
      * Claude Code writes the JSON payload to stdin;
      * Codex ``notify`` passes the JSON as the last argv element
        (verified: argv = [.., '{"type":"agent-turn-complete",...}']).
    Support both, and any extra argv, without guessing.
    """
    raw = ""
    # 1) argv first (Codex notify): avoids ever blocking on an inherited stdin.
    candidates = [getattr(args, "payload", "") or ""] + list(getattr(args, "rest", []) or [])
    for cand in candidates:
        if cand.strip().startswith("{"):
            raw = cand
            break
    # 2) then stdin (Claude Code hooks write JSON and close the pipe).
    if not raw and not sys.stdin.isatty():
        raw = sys.stdin.read()
    payload: dict[str, Any] = {}
    if raw.strip():
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw}
    if not isinstance(payload, dict):
        payload = {"raw": raw}

    if os.environ.get("ARB_HOOK_DEBUG"):
        try:
            from .util import now as _now

            debug = _cfg().home / "hook-debug.log"
            with debug.open("a", encoding="utf-8") as fh:
                fh.write(
                    json_dumps(
                        {
                            "t": _now(),
                            "agent_arg": getattr(args, "agent", ""),
                            "argv": sys.argv[1:],
                            "stdin_len": len(raw),
                            "stdin_head": raw[:300],
                            "parsed_keys": sorted(payload)[:8],
                        }
                    )
                    + "\n"
                )
        except Exception:
            pass

    cfg = _cfg()
    agent = args.agent or agents.sniff_agent(payload)

    # Hook contract (Claude Code and Codex both use it): identical event names,
    # identical hookSpecificOutput. One path serves both, with the agent label
    # preserved for routing.
    if payload.get("hook_event_name"):
        ev = agents.hook_envelope(payload, agent, cwd=os.getcwd())
        if ev.event == "PreToolUse":
            return _claude_approval(cfg, ev)
        return _push_event(cfg, ev, hook_mode=True)

    if agent == agents.AGENT_CODEX:
        ev = agents.codex_event(payload, cwd=os.getcwd())
        return _push_event(cfg, ev, hook_mode=True)

    if agent == agents.AGENT_CLAUDE:
        ev = agents.claude_event(payload, cwd=os.getcwd())
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


_SAFE_CHAIN = set(";&><\n")


def _is_safe_read(cfg: Config, ev: "agents.AgentEvent") -> bool:
    """A narrow, boring definition of 'read-only' for the approval gate.

    A read-only command may be a *pipeline* of allowlisted commands
    (`ls ... 2>&1 | head -50`) because every stage is read-only. What stays
    gated: chaining (; && ||), real redirection (> <), command substitution,
    anything outside the allowlist, and anything mentioning a secret.
    """
    if ev.tool != "Bash":
        return False
    cmd = str((ev.tool_input or {}).get("command") or "").strip()
    if not cmd:
        return False
    normalized = cmd.replace("2>&1", " ")
    if any(ch in normalized for ch in _SAFE_CHAIN) or "$(" in normalized or "`" in normalized:
        return False
    low = cmd.lower()
    for secret in ("id_rsa", ".ssh", ".env", "credential", "secret", "token", "password", "wallet"):
        if secret in low:
            return False
    segments = [s.strip() for s in normalized.split("|") if s.strip()]
    if not segments:
        return False
    safe = {s.lower() for s in cfg.safe_commands}
    git_safe = {s.lower() for s in cfg.safe_git_subcommands}
    for seg in segments:
        parts = seg.replace('"', " ").replace("'", " ").split()
        if not parts:
            return False
        head = parts[0].lower()
        if head == "git":
            if not (len(parts) > 1 and parts[1].lower() in git_safe):
                return False
        elif head not in safe:
            return False
    return True


def _claude_approval(cfg: Config, ev: agents.AgentEvent) -> int:
    """Remote approval gate for Claude Code PreToolUse hooks."""
    if cfg.approval_mode != "approve" or not _tool_gated(cfg, ev.tool):
        return EXIT_OK  # not gated: let Claude Code's own permission flow handle it

    if cfg.auto_approve_safe and _is_safe_read(cfg, ev):
        _emit_hook_decision("allow", "只读命令，按 auto_approve_safe 自动放行")
        return EXIT_OK

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
    p.add_argument("--version", action="version", version=f"agent-remote-bridge {__version__}")
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

    sp = sub.add_parser("approval", help="开关远程审批（on/off/status）")
    sp.add_argument("mode", nargs="?", default="status", choices=["on", "off", "status"])
    sp.set_defaults(func=cmd_approval)

    sp = sub.add_parser("hooks", help="安装/移除 Claude Code hooks（install/uninstall/status）")
    sp.add_argument("action", nargs="?", default="status", choices=["install", "uninstall", "status"])
    sp.add_argument("--settings", default="", help="自定义 settings.json 路径")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_hooks)

    sp = sub.add_parser("node", help="把本机各 Agent 接上桥接（setup/uninstall/status）")
    sp.add_argument("action", nargs="?", default="status", choices=["setup", "uninstall", "status"])
    sp.add_argument("--only", default="", help="只处理某些 Agent，逗号分隔：codex,dsh,claude")
    sp.add_argument("--settings", default="", help="Claude settings.json 路径")
    sp.add_argument("--dsh-profile", default="", help="DSH cordis.patch.yml 路径")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_node)

    sp = sub.add_parser("hub", help="在服务器上运行中转 Hub（serve）")
    sp.add_argument("action", nargs="?", default="serve", choices=["serve"])
    sp.add_argument("config", nargs="?", default="/etc/arb/hub.json")
    sp.set_defaults(func=cmd_hub)

    sp = sub.add_parser("node-join", help="把本机注册到 Hub 并切换到 relay 模式")
    sp.add_argument("hub", help="例如 https://120.78.79.184:18443")
    sp.add_argument("--name", default="", help="机器名（默认取 COMPUTERNAME）")
    sp.add_argument("--token", default="", help="节点 token（默认随机生成并保存在本机）")
    sp.add_argument("--fingerprint", default="", help="Hub 自签证书的 SHA-256 指纹（推荐）")
    sp.add_argument("--code", default="", help="Hub 的加入码（新机器首次注册必需）")
    sp.set_defaults(func=cmd_node_join)

    sp = sub.add_parser("service", help="安装/卸载 Windows 登录自启（install/uninstall/status）")
    sp.add_argument("action", nargs="?", default="status", choices=["install", "uninstall", "status"])
    sp.set_defaults(func=cmd_service)
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

    sp = sub.add_parser("hook", help="Agent hook 入口（stdin 或 argv 传入事件 JSON）")
    sp.add_argument("agent", nargs="?", default="")
    sp.add_argument("payload", nargs="?", default="", help="Codex notify 把事件 JSON 作为最后一个参数传入")
    sp.add_argument("rest", nargs="*", default=[])
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
