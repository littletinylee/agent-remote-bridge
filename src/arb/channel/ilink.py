"""WeChat channel over the iLink Bot HTTP API.

Protocol (verified against Tencent's MIT-licensed `openclaw-weixin` plugin and
`wechat-claude-code`; both speak the same five endpoints):

    GET  ilink/bot/get_bot_qrcode?bot_type=3      -> {qrcode, qrcode_img_content}
    GET  ilink/bot/get_qrcode_status?qrcode=...   -> {status, bot_token, ilink_bot_id, ...}
    POST ilink/bot/getupdates                     -> {msgs[], get_updates_buf, errcode, ...}
    POST ilink/bot/sendmessage                    -> {message_id}
    POST ilink/bot/msg/notifystart | notifystop

Properties that matter for us:
  * everything is client-initiated (long poll), so the Windows box needs no
    public IP, no port forwarding and no cloud relay;
  * `sendmessage` is a normal bot push with no 48h customer-service window, so
    a notification sent hours after the last human message still arrives;
  * auth is a bearer token obtained by scanning a QR code with the personal
    WeChat app; errcode -14 means the token died and a re-login is required.
"""

from __future__ import annotations

import http.client
import json
import random
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from base64 import b64encode
from pathlib import Path
from typing import Any, Callable

from ..config import Config, IlinkCredentials, load_ilink, save_ilink
from ..util import gen_client_id, log_stderr, redact
from .base import Channel, ChannelError, InboundMessage, OnMessage

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
LONG_POLL_TIMEOUT_S = 35.0
API_TIMEOUT_S = 15.0
STALE_TOKEN_ERRCODE = -14
UA_VERSION = "agent-remote-bridge/1.0"

STATUS_WAIT = "wait"
STATUS_SCANED = "scaned"
STATUS_CONFIRMED = "confirmed"
STATUS_EXPIRED = "expired"
STATUS_BINDED_REDIRECT = "binded_redirect"


def _random_uin() -> str:
    """X-WECHAT-UIN: decimal of 4 random bytes, base64 encoded (mirrors clients)."""
    num = random.getrandbits(32)
    return b64encode(str(num).encode("utf-8")).decode("ascii")

class IlinkError(ChannelError):
    def __init__(self, message: str, *, status: int = 0, body: str = "", errcode: int = 0):
        super().__init__(
            message,
            auth_invalid=(status in (401, 403) or errcode == STALE_TOKEN_ERRCODE),
            retryable=(status == 0 or status >= 500 or status == 429),
        )
        self.status = status
        self.body = body
        self.errcode = errcode


class IlinkClient:
    """Thin, dependency-free HTTP client for the iLink bot API."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str = ""):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.token = token
        self._ssl = ssl.create_default_context()

    # -- low level -------------------------------------------------------
    def _headers(self, with_body: bool, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _random_uin(),
            "iLink-App-ClientVersion": "1",
        }
        if with_body:
            h["Content-Type"] = "application/json"
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if extra:
            h.update(extra)
        return h

    def _request(
        self,
        method: str,
        endpoint: str,
        body: dict[str, Any] | None = None,
        timeout: float = API_TIMEOUT_S,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        parts = urllib.parse.urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = self._headers(with_body=data is not None)
        if data is not None:
            headers["Content-Length"] = str(len(data))

        # http.client (not urllib) because urllib silently re-capitalises header
        # names, and the bot API's `AuthorizationType` header must go on the wire
        # exactly as the reference clients send it.
        if parts.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                parts.hostname, parts.port or 443, timeout=timeout, context=self._ssl
            )
        else:
            conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
        try:
            conn.putrequest(method, path, skip_accept_encoding=True)
            for name, value in headers.items():
                conn.putheader(name, value)
            conn.endheaders(data)
            resp = conn.getresponse()
            status = resp.status
            raw = resp.read()
        except (socket.timeout, TimeoutError):
            raise IlinkError(f"{endpoint}: timeout after {timeout}s", retryable=True) from None
        except (OSError, http.client.HTTPException) as exc:
            raise IlinkError(f"{endpoint}: network error {exc}", retryable=True) from None
        finally:
            try:
                conn.close()
            except OSError:
                pass

        text = raw.decode("utf-8", "replace")
        if status != 200:
            raise IlinkError(f"{endpoint}: HTTP {status} {redact(text)[:300]}", status=status, body=text[:800])
        try:
            payload = json.loads(text or "{}")
        except ValueError:
            raise IlinkError(f"{endpoint}: non-JSON response", body=text[:200]) from None
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _base_info() -> dict[str, Any]:
        return {"channel_version": UA_VERSION, "bot_agent": UA_VERSION}

    # -- auth ------------------------------------------------------------
    def get_qrcode(self, bot_type: str = "3") -> dict[str, Any]:
        return self._request("GET", f"ilink/bot/get_bot_qrcode?bot_type={bot_type}")

    def poll_qr_status(self, qrcode: str, timeout: float = LONG_POLL_TIMEOUT_S) -> dict[str, Any]:
        try:
            return self._request(
                "GET", f"ilink/bot/get_qrcode_status?qrcode={urllib.parse.quote(qrcode)}", timeout=timeout
            )
        except IlinkError as exc:
            if not exc.retryable:
                raise
            return {"status": STATUS_WAIT}

    # -- messaging -------------------------------------------------------
    def get_updates(self, buf: str, timeout: float = LONG_POLL_TIMEOUT_S) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "ilink/bot/getupdates",
            {"get_updates_buf": buf, "base_info": self._base_info()},
            timeout=timeout,
        )
        return payload

    def send_message(self, to_user: str, text: str, context_token: str = "", client_id: str | None = None) -> dict[str, Any]:
        cid = client_id or gen_client_id("arb")
        msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user,
            "client_id": cid,
            "message_type": 2,   # BOT
            "message_state": 2,  # FINISH
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
        if context_token:
            msg["context_token"] = context_token
        return self._request(
            "POST", "ilink/bot/sendmessage", {"msg": msg, "base_info": self._base_info()}, timeout=API_TIMEOUT_S
        )

    def notify(self, lifecycle: str) -> None:
        endpoint = "ilink/bot/msg/notifystart" if lifecycle == "start" else "ilink/bot/msg/notifystop"
        try:
            self._request("POST", endpoint, {"base_info": self._base_info()}, timeout=8.0)
        except (IlinkError, OSError):
            pass


def render_qr(text: str) -> bool:
    """Best-effort terminal QR. Returns False when no renderer is available."""
    try:
        import qrcode  # optional dependency
    except ImportError:
        return False
    try:
        qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(text)
        qr.make(fit=True)
        qr.print_ascii(out=sys.stderr, invert=True)
        return True
    except Exception:  # pragma: no cover - renderer is cosmetic
        return False


def save_qr_png(text: str, path: str) -> bool:
    """Write the login QR to a PNG so it can be shown in a chat/GUI."""
    try:
        import qrcode
    except ImportError:
        return False
    try:
        qr = qrcode.QRCode(border=3, box_size=8, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        img.save(path)
        return True
    except Exception:  # pragma: no cover
        return False


def login(
    cfg: Config,
    *,
    timeout_s: float = 300.0,
    on_status: Callable[[str], None] | None = None,
    client: IlinkClient | None = None,
    show_qr: bool = True,
    qr_png: str = "",
) -> IlinkCredentials:
    """Interactive QR login. Blocking; returns credentials on success."""
    say = on_status or (lambda m: log_stderr(m))
    api = client or IlinkClient(cfg.ilink_base_url)
    started = time.time()
    qr_refreshes = 0

    while time.time() - started < timeout_s:
        info = api.get_qrcode(cfg.ilink_bot_type)
        if int(info.get("ret", 0) or 0) != 0 or not info.get("qrcode"):
            raise IlinkError(f"get_bot_qrcode failed: {redact(json.dumps(info, ensure_ascii=False))}")
        qrcode_id = str(info["qrcode"])
        content = str(info.get("qrcode_img_content") or "")

        log_stderr("")
        if qr_png and save_qr_png(content, qr_png):
            log_stderr(f"二维码已保存: {qr_png}")
        if show_qr and not render_qr(content):
            log_stderr("（未安装可选依赖 qrcode，无法在终端画二维码）")
        if show_qr:
            log_stderr("请用手机微信扫描上方二维码，并在手机上确认授权。")
        log_stderr(f"备用：把下面的内容贴到任意二维码生成器扫描 ——\n{content}\n")

        scanned = False
        while time.time() - started < timeout_s:
            status = api.poll_qr_status(qrcode_id)
            state = str(status.get("status") or "").lower()

            if state == STATUS_WAIT:
                continue
            if state == STATUS_SCANED:
                if not scanned:
                    say("已扫码，请在手机上确认…")
                    scanned = True
                continue
            if state == STATUS_EXPIRED:
                qr_refreshes += 1
                if qr_refreshes > 4:
                    raise IlinkError("二维码多次过期，请重新运行 arb login")
                say("二维码已过期，正在刷新…")
                break
            if state == STATUS_BINDED_REDIRECT:
                host = str(status.get("redirect_host") or "").strip()
                if host:
                    api.base_url = host if host.startswith("http") else f"https://{host}"
                existing = load_ilink(cfg)
                if existing.valid:
                    say("该微信号已绑定过本机（binded_redirect），沿用现有凭据。")
                    return existing
                raise IlinkError("服务端报告已绑定但本机没有可用凭据，请先在旧设备解绑或使用新的微信号登录")
            if state == STATUS_CONFIRMED:
                token = str(status.get("bot_token") or "")
                account_id = str(status.get("ilink_bot_id") or "")
                if not token or not account_id:
                    raise IlinkError("登录已确认，但服务端未返回 bot_token / ilink_bot_id")
                creds = IlinkCredentials(
                    token=token,
                    account_id=account_id,
                    base_url=str(status.get("baseurl") or api.base_url),
                    logged_in_at=time.time(),
                    bot_user_id=str(status.get("ilink_user_id") or ""),
                )
                save_ilink(cfg, creds)
                say(f"登录成功：bot={account_id}")
                return creds
            # unknown status: keep polling
        # inner loop ended -> refresh QR

    raise IlinkError("登录超时")


class IlinkChannel(Channel):
    name = "ilink"

    def __init__(self, cfg: Config, store=None):
        self.cfg = cfg
        self.store = store
        self.creds = load_ilink(cfg)
        self.client = IlinkClient(self.creds.base_url or cfg.ilink_base_url, self.creds.token)
        self._healthy = False
        self._stop = threading.Event()
        self._auth_error = ""
        self._last_error = ""
        self._lock = threading.Lock()
        self._ctx_tokens: dict[str, str] = {}

    # -- health ----------------------------------------------------------
    def healthy(self) -> bool:
        return self._healthy and bool(self.creds.token)

    @property
    def auth_error(self) -> str:
        return self._auth_error

    @property
    def last_error(self) -> str:
        return self._last_error

    def _set_healthy(self, ok: bool, err: str = "") -> None:
        with self._lock:
            was = self._healthy
            self._healthy = ok
            if err:
                self._last_error = err
            if ok:
                self._auth_error = ""
        if was != ok and self.store is not None:
            self.store.audit("channel_health", "channel", {"healthy": ok, "error": err})

    # -- inbound loop ----------------------------------------------------
    def start(self, on_message: OnMessage, stop_event: threading.Event) -> None:
        if not self.creds.token:
            self._set_healthy(False, "not logged in")
            raise ChannelError("WeChat channel not logged in: run `arb login`", auth_invalid=True, retryable=False)

        buf = ""
        if self.store is not None:
            buf = self.store.kv_get("ilink.buf", "")
        timeout = LONG_POLL_TIMEOUT_S
        failures = 0
        self.client.notify("start")
        try:
            while not stop_event.is_set() and not self._stop.is_set():
                try:
                    resp = self.client.get_updates(buf, timeout=timeout)
                    errcode = int(resp.get("errcode", 0) or 0)
                    ret = int(resp.get("ret", 0) or 0)
                    if errcode == STALE_TOKEN_ERRCODE or ret == STALE_TOKEN_ERRCODE:
                        self._set_healthy(False, "token expired (errcode -14)")
                        self._auth_error = "微信登录已失效（errcode -14），请重新运行 arb login"
                        return
                    if errcode or ret:
                        raise IlinkError(
                            f"getupdates ret={ret} errcode={errcode} errmsg={resp.get('errmsg', '')}",
                            errcode=errcode,
                        )

                    new_buf = str(resp.get("get_updates_buf") or "")
                    if new_buf:
                        buf = new_buf
                        if self.store is not None:
                            self.store.kv_set("ilink.buf", buf)
                    suggested = int(resp.get("longpolling_timeout_ms") or 0)
                    if suggested > 0:
                        timeout = max(5.0, min(60.0, suggested / 1000.0))

                    for msg in resp.get("msgs") or []:
                        parsed = self._parse(msg)
                        if parsed is None:
                            continue
                        if parsed.context_token:
                            self._ctx_tokens[parsed.from_user] = parsed.context_token
                            if self.store is not None:
                                self.store.kv_set(f"ilink.ctx.{parsed.from_user}", parsed.context_token)
                        try:
                            on_message(parsed)
                        except Exception as exc:  # never kill the channel thread
                            log_stderr(f"[ilink] handler error: {exc}")

                    failures = 0
                    self._set_healthy(True)
                except IlinkError as exc:
                    failures += 1
                    self._set_healthy(False, str(exc))
                    if exc.auth_invalid:
                        self._auth_error = "微信凭据失效，请重新运行 arb login"
                        return
                    backoff = min(30.0, 1.5 ** min(failures, 8))
                    if failures >= 3:
                        backoff = 30.0
                    log_stderr(f"[ilink] poll failed ({failures}x): {exc}; retrying in {backoff:.0f}s")
                    stop_event.wait(backoff)
                except OSError as exc:
                    failures += 1
                    self._set_healthy(False, str(exc))
                    stop_event.wait(min(30.0, 2.0 * failures))
        finally:
            self._set_healthy(False, "stopped")
            self.client.notify("stop")

    @staticmethod
    def _parse(msg: dict[str, Any]) -> InboundMessage | None:
        if not isinstance(msg, dict):
            return None
        mtype = msg.get("message_type")
        if mtype is not None and int(mtype) != 1:  # 1 = USER; ignore our own BOT echoes
            return None
        from_user = str(msg.get("from_user_id") or "")
        if not from_user:
            return None
        items = msg.get("item_list") or []
        text = ""
        ref_svr = ""
        for item in items:
            if not isinstance(item, dict):
                continue
            if int(item.get("type") or 0) == 1 and isinstance(item.get("text_item"), dict):
                text += str(item["text_item"].get("text") or "")
            ref = item.get("ref_msg")
            if isinstance(ref, dict):
                ref_svr = str(ref.get("svr_id") or "")
        if not text.strip():
            return None
        msg_id = str(msg.get("message_id") or "")
        if not msg_id:
            msg_id = f"local-{from_user}-{int(time.time() * 1000)}-{random.randrange(1 << 20)}"
        return InboundMessage(
            msg_id=msg_id,
            from_user=from_user,
            text=text,
            ref_svr_id=ref_svr,
            context_token=str(msg.get("context_token") or ""),
        )

    # -- outbound --------------------------------------------------------
    def _audience(self) -> list[str]:
        return list(self.cfg.owner_ids)

    def send(self, text: str, to: str | None = None) -> str:
        if not self.creds.token:
            raise ChannelError("WeChat channel not logged in", auth_invalid=True, retryable=False)
        targets = [to] if to else self._audience()
        if not targets:
            raise ChannelError("no paired WeChat user yet: send `/pair <code>` from WeChat first", retryable=False)
        last_exc: Exception | None = None
        for user in targets:
            ctx = self._ctx_tokens.get(user)
            if not ctx and self.store is not None:
                ctx = self.store.kv_get(f"ilink.ctx.{user}", "")
                if ctx:
                    self._ctx_tokens[user] = ctx
            try:
                resp = self.client.send_message(user, text, context_token=ctx)
                errcode = int(resp.get("errcode", 0) or 0)
                if errcode == STALE_TOKEN_ERRCODE:
                    self._auth_error = "微信凭据失效，请重新运行 arb login"
                    raise ChannelError("WeChat token expired", auth_invalid=True, retryable=False)
                msg_id = str(resp.get("message_id") or "") or f"out-{int(time.time() * 1000)}"
                self._set_healthy(True)
                return msg_id
            except ChannelError as exc:
                last_exc = exc
                self._set_healthy(False, str(exc))
                if exc.auth_invalid:
                    raise
        raise last_exc or ChannelError("send failed")

    def close(self) -> None:
        self._stop.set()
