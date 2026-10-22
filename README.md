# Agent Remote Bridge

**用普通个人微信远程监督 AI 编程智能体，并参与关键决策。**

不是聊天机器人，也不是 GitHub 提交监控 —— 它是 **AI Agent Remote Control / Human-in-the-Loop Bridge**：
把 Agent 的"需要人拍板"变成一条微信消息，把你的回复准确送回**最初那个会话**，其他 Agent 继续跑。

```
┌─────────────┐  hooks / MCP   ┌──────────────────────┐   HTTPS 长轮询   ┌──────────────┐
│ Claude Code │───────────────►│                      │◄────────────────►│  微信 iLink   │
│ Codex       │  ask_human     │  agent-remote-bridge │   sendmessage    │  Bot 通道     │
│ DSH         │◄───────────────│  (本机，Python)       │─────────────────►│              │
│ 任意 MCP 端 │   答案回传      │  SQLite + 状态机      │                  └──────┬───────┘
└─────────────┘                └──────────────────────┘                         │
                                                                         手机个人微信
```

**核心事实：本机不需要公网 IP，不需要端口映射，不需要云服务器。** 收发都是出站连接。

---

## 为什么是这个方案

调研了 Happy / tlive / claude-telegram-mirror / Tencent openclaw-weixin / wechat-claude-code 后确定：

- **微信通道复用腾讯官方 `openclaw-weixin` 使用的 iLink Bot 协议**（扫码登录 + 长轮询收 + 主动推送发），
  个人微信双向、无 48 小时窗口限制。见 [docs/RESEARCH.md](docs/RESEARCH.md)。
- **Agent 接入复用厂商自己的 hook 机制 + MCP 标准**（tlive 验证过的路线），不接管会话、不改你的工作流。
- 因此**不部署阿里云 ECS**：没有任何一端需要公网入口。

## 能力

| 能力 | 状态 |
|---|---|
| 任务开始 / 完成 / 异常主动推送 | ✅ 实测（真机微信） |
| 微信提问 + 选项 + 上下文 | ✅ 实测（真机微信） |
| 微信回复 → 回到原始会话继续 | ✅ 实测：Claude Code、Codex |
| 危险操作远程审批（拦在工具执行前） | ✅ 实测：真机微信里裸 `1` 被拒、`yes` 放行 |
| 多 Agent / 多子智能体并发 | ✅ 实测（4 个真实进程） |
| 多问题并发、按 `#CODE` 或"引用回复"精确归属 | ✅ 实测（不串线） |
| 等待人工时其他 Agent 继续执行 | ✅ 实测（时间线可查） |
| 危险操作必须显式批准，超时=拒绝 | ✅ 真机 + 测试 |
| 断线重连、重启后待处理问题恢复 | ✅ 测试覆盖 |
| 重复投递 / 重复审批去重 | ✅ 测试覆盖 |
| 零第三方依赖、Windows / Linux / macOS | ✅ |
| **长时间（≥1h）无交互后仍能推送** | ⏳ 待复测（冷推送已成功，机制成立） |

没做的、做不到的、需要条件的，都写在 [docs/LIMITATIONS.md](docs/LIMITATIONS.md)；实测细节与时间线在
[docs/VERIFICATION.md](docs/VERIFICATION.md)。

### 实测时间线（`scripts/e2e_concurrent.py`）

```
 0.61s  A=Claude Code 启动      C=Codex 启动
 1.62s  B=Codex 启动并阻塞在 ask_human
11.46s  微信收到问题 #S5VY；A 完成任务（文件已写）
17.27s  C 完成任务；此时我才回复 #S5VY
25.82s  B 带着答案继续并结束
```

---

## 快速开始（Windows）

前置：Python ≥ 3.9（`python --version` 可用即可）。

```powershell
git clone https://github.com/littletinylee/agent-remote-bridge.git
cd agent-remote-bridge
powershell -ExecutionPolicy Bypass -File scripts\install.ps1

arb init                                   # 生成配置（打印绑定码）
arb login                                  # 手机微信扫码
arb start                                  # 后台起守护进程
# 在微信里给机器人发： /pair <绑定码>
arb test-push                              # 主动推送自检
```

想直接在终端画二维码：`python -m pip install "qrcode[pil]"`（可选）。
无二维码时 `arb login` 会打印可复制的登录串，用任意二维码生成器扫也行。

## 接入 Agent

| Agent | 通知 | 主动提问 | 远程审批 |
|---|---|---|---|
| **Claude Code** | hooks | MCP | ✅ hooks `PreToolUse` |
| **Codex** | `notify` | MCP（需放宽审批）或 CLI | ❌ 无审批 hook |
| 其他 MCP 客户端（DSH / OpenCode…） | — | MCP | 视 Agent 是否有 hook |

**Claude Code**

```bash
claude mcp add arb -- python -m arb mcp --agent claude-code
```

再把 [hooks/claude-settings.example.json](hooks/claude-settings.example.json) 合并进 `~/.claude/settings.json`。

**Codex**（`~/.codex/config.toml`）

```toml
notify = ["python", "-m", "arb", "hook", "codex"]

[mcp_servers.arb]
command = "python"
args = ["-m", "arb", "mcp", "--agent", "codex"]
```

**任何能跑命令的 Agent**

```bash
python -m arb ask "用 Postgres 还是 SQLite?" --option Postgres --option SQLite --timeout 300
```

逐项配方、踩过的坑（Codex 的 argv 载荷、MCP 审批策略）见 [docs/AGENTS.md](docs/AGENTS.md)。

## 微信里怎么用

```
/status             运行状态
/pending            待处理问题
/answer #K7MQ 1     回答某个问题
/approve #K7MQ      批准（危险操作必须这样，或回复 yes）
/deny #K7MQ         拒绝
/cancel #K7MQ       取消
/projects           会话列表
/mute /unmute       静音/恢复
```

- 只有**一个**待处理问题时，直接回 `1` / `同意` 即可。
- **多个**同时等待时，裸回复会被拒绝并列出编号，必须带 `#CODE` —— 这是防串线的关键。
- 在微信里**引用回复**某条提问，按消息 ID 精确命中（优先级最高）。

## 设计要点

- **不重造调度**：Agent 自己的子任务/子智能体机制原样保留，桥接只搬运"需要人"的那一步。
- **阻塞只阻塞提问者**：`ask_human` 只是那个 Agent 的一次工具调用。
- **归属靠三层证据**：引用消息 ID > 显式 `#CODE` > "唯一待处理"。都不成立就拒绝，不猜。
- **一次性状态迁移**：`UPDATE ... WHERE state='pending'` 的 `rowcount` 决定谁赢。
- **失败即拒绝**：投递失败、通道异常、超时，都不会被当成批准。

细节：[架构](docs/ARCHITECTURE.md) ｜ [安全](docs/SECURITY.md) ｜ [部署运维](docs/DEPLOY.md)

## 开发

```powershell
$env:PYTHONPATH="$PWD\src"
python -m unittest discover -s tests -p "test_*.py"       # 74 个离线测试
python scripts\e2e_claude.py                              # 真实 Claude Code
python scripts\e2e_codex.py                               # 真实 Codex
python scripts\e2e_concurrent.py                          # 并发验收
```

代码结构：

```
src/arb/
  channel/     微信 iLink 协议（长轮询/发送/登录）、console 通道
  router.py    入站消息 → 动作（四层归属证据、命令、危险门禁）
  store.py     SQLite：sessions/questions/outbox/inbound/audit
  daemon.py    守护进程：回环 API、outbox 重试、过期清扫、重启恢复
  mcp.py       MCP stdio 服务器（ask_human 等 5 个工具）
  agents.py    Claude Code / Codex hook 载荷 → 统一事件
  cli.py       init/login/serve/status/doctor/ask/notify/hook/mcp
```

## 许可

MIT。协议实现参考 Tencent [openclaw-weixin](https://github.com/Tencent/openclaw-weixin)（MIT）与
[wechat-claude-code](https://github.com/Conn-Ho/wechat-claude-code)（Apache-2.0），架构思路参考
[tlive](https://github.com/y49/tlive)（MIT）。见 [NOTICE](NOTICE)。
