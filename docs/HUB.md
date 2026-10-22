# Hub：多机共用一个微信机器人

一台 Windows 电脑直接连微信没问题；但当**多台**机器要共用同一个机器人时，必须有
唯一一个"接消息的人"——因为 iLink 的 bot token 只有一个、`get_updates_buf` 游标是共享的，
两个进程同时长轮询会互相抢消息。

Hub 就是这个唯一进程，跑在阿里云 ECS 上；各台开发机**出站**连它，不需要公网 IP。

```
手机微信 ⟷ iLink ⟷ Hub (ECS, 独占 token+游标, TLS 18443)
                     ├─ 机器注册表（名字 + token hash + 在线状态）
                     ├─ 每机独立 inbox（离线缓冲，重连补发）
                     └─ 决策码 → 机器 的索引（只路由，不存答案）
                          ▲ 出站长轮询
        ┌─────────────────┼─────────────────┐
   台式机 workbench    笔记本 laptop2      第三台…
   (SQLite 状态机)     (SQLite)            …
```

## 部署（服务器）

> `pip install "git+https://github.com/littletinylee/agent-remote-bridge.git"` 之后，
> 把 systemd 的 `ExecStart` 写成 `arb hub serve /etc/arb/hub.json` 并去掉 `Environment=PYTHONPATH=`。
> 下面的 clone 方式保留给没有 pip 的机器。

```bash
# 1) 代码 + 目录
git clone https://github.com/littletinylee/agent-remote-bridge.git /opt/arb
mkdir -p /etc/arb /var/lib/arb && chmod 700 /etc/arb /var/lib/arb

# 2) 自签证书（节点按指纹固定，不需要域名）
openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
  -keyout /etc/arb/hub.key -out /etc/arb/hub.crt -subj "/CN=arb-hub" \
  -addext "subjectAltName=IP:<公网IP>,DNS:arb-hub"
openssl x509 -in /etc/arb/hub.crt -noout -fingerprint -sha256   # 记下来

# 3) 配置（微信凭据 + 允许的微信用户）
cat >/etc/arb/hub.json <<'JSON'
{ "port": 18443, "state": "/var/lib/arb/hub.sqlite3",
  "ilink_creds": "/etc/arb/ilink.json",
  "tls_cert": "/etc/arb/hub.crt", "tls_key": "/etc/arb/hub.key",
  "owner_ids": ["<你的微信 id>"], "name": "hub" }
JSON
chmod 600 /etc/arb/*.json

# 4) systemd（仓库里 scripts/hub_setup_remote.sh 会自动做这些）
systemctl enable --now arb-hub
curl -k https://127.0.0.1:18443/v1/health
```

**安全组**必须放行 TCP 18443（入方向）。没有放行时表现为 `filtered`/连接超时。

## 接入（每台开发机）

```powershell
arb node-join https://<公网IP>:18443 --name workbench --fingerprint <证书指纹>
arb node setup        # 装 Claude Code / Codex / DSH 的插件
arb start
```

`node-join` 会校验证书指纹、注册机器、把 `channel` 切成 `relay` 并写入 `hub_*` 配置。

## 回复的优先级（关键）

多条消息同时到达时，怎么保证答对人？

| 优先级 | 证据 | 说明 |
|---|---|---|
| ① | **微信「引用」某条提问** | 按被引用消息的 id 精确定位，最强。引用已处理过的会被忽略并告知 |
| ② | `#CODE 内容` | 显式编号 |
| ③ | 只有一个待决策时裸回复 | 允许 |
| ④ | 多个待决策 + 裸回复 | **拒绝**并列出编号（绝不猜） |

推送文案里会提示"或引用本条消息直接回"，就是为了这个。

## 安全不变量（有测试守着）

1. **Hub 从不回答决策**，只转发文本；答案必须来自你的微信账号。
2. **聊天层永远不能批准**（M3）。
3. 决策按 `question_id` 路由；未知编号直接拒绝。
4. 多待决 + 无证据 → 拒绝，不猜。
5. **机器之间隔离**：A 机的 inbox 不会出现 B 机的消息（测试断言）。
6. **节点拥有问题状态**：Hub 只在节点真正受理答复后才关闭该码，避免"假成功"。
7. 节点收到答复后会重新包成 `#CODE 文本` 走**本地** Router，过期/危险门禁/一次性迁移全部本地复验 ——
   Hub 无法绕过任何规则。

## 运维

```bash
systemctl status arb-hub            # 服务状态
journalctl -u arb-hub -n 50         # 日志
curl -k https://127.0.0.1:18443/v1/health   # {"ok":true,"nodes":N,"wechat_logged_in":true}
python3 scripts/hub_ask_once.py --code HUBQ --prompt "..." --option A --option B  # 运维提问
```

**升级**：`cd /opt/arb && git fetch --depth 1 origin main && git reset --hard origin/main && systemctl restart arb-hub`

**状态库**：`/var/lib/arb/hub.sqlite3`（nodes / inbox / questions / seen / audit）。

## 已知限制

- 微信凭据搬到服务器上了：Hub 只放 bot token，**不放**代码、GitHub 凭据、模型 key。
- 单 Hub 是单点：它挂了微信就断（各机器的本地状态不受影响，恢复后继续）。
- 群里不支持，只处理与 bot 的一对一会话。

## 服务器安装：两种方式（2026-10-22 实测）

在 Ubuntu 22.04 / pip 22.0.2 上：

- `pip install "git+https://…"`：pip 打印完 `Preparing metadata (pyproject.toml): done` 后**静默中断**，没有报错也没有安装（疑似该 pip 版本与 PEP 517 后端交互问题，未深究）。
- **可行方式**：本地 `python -m pip wheel . --no-deps -w dist`，把生成的 wheel 传到服务器，`pip install --no-deps <wheel>`；随后 systemd 的 `ExecStart` 用 `/usr/local/bin/arb hub serve /etc/arb/hub.json`，并删掉 `Environment=PYTHONPATH=`。

当前 ECS 就是第二种方式：`systemctl is-active arb-hub` = active，日志进程名是 `arb[...]`（pip 入口）而不是 `python3 -m arb`，说明已不依赖仓库路径。

