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

调研了 Happy / tlive / claude-telegram-mirror / Tencent openclaw-weixin / wechat-claude-code 后确认：

- **微信通道复用腾讯官方 `openclaw-weixin` 使用的 iLink Bot 协议**（扫码登录 + 长轮询收 + 主动推送发），个人微信双向、无 48 小时窗口限制。见 [docs/RESEARCH.md](docs/RESEARCH.md)。
- **Agent 接入复用两家厂商自己的 hook 机制 + MCP 标准**（tlive 验证过的路线），不接管会话、不改你的工作流。
- 因此**不部署阿里云 ECS**：没有任何一端需要公网入口。

## 能力

| 能力 | 状态 |
|---|---|
| 任务开始 / 完成 / 异常主动推送 | ✅ |
| 微信提问 + 选项 + 上下文 | ✅ |
| 微信回复 → 回到原始会话继续 | ✅（阻塞式 hook / MCP tool result） |
| 多 Agent / 多子智能体并发 | ✅ |
| 多问题并发、按 `#CODE` 或"引用回复"精确归属 | ✅ |
| 防止串线（歧义时拒绝而非猜测） | ✅ |
| 危险操作必须显式批准，超时=拒绝 | ✅ |
| 断线重连、重启后待处理问题恢复 | ✅ |
| 重复投递 / 重复审批去重 | ✅ |
| 零第三方依赖、Windows / Linux / macOS | ✅ |

完整验证记录见 [docs/VERIFICATION.md](docs/VERIFICATION.md)，未实现项见 [docs/LIMITATIONS.md](docs/LIMITATIONS.md)。

---

## 快速开始（Windows）

前置：Python ≥ 3.9（`python --version` 可用即可）。

```powershell
git clone <this-repo> ; cd agent-remote-bridge

# 1) 生成配置（会打印绑定码）
.\scripts\arb.cmd init

# 2) 扫码登录微信机器人（手机微信扫终端里的二维码）
.\scripts\arb.cmd login

# 3) 启动守护进程（后台）
.\scripts\arb.cmd start

# 4) 在微信里给刚出现的机器人发一条消息：
#    /pair <上一步的绑定码>
.\scripts\arb.cmd status
```

> 首次必须先由你给机器人发一条消息完成绑定 —— Bot 协议只在你发送过消息后才拿到你的 `user_id`。

安装 `qrcode` 可选依赖可以让登录时直接在终端画二维码：

```powershell
python -m pip install "qrcode[pil]"
```

## 接入 Agent

### Claude Code（事件 + 远程审批 + MCP）

```bash
# MCP：让 Claude 能主动问你
claude mcp add arb -- python -m arb mcp --agent claude-code
```

`~/.claude/settings.json` 增加 hooks（完整示例见 [hooks/claude-settings.example.json](hooks/claude-settings.example.json)）：

```json
{
  "hooks": {
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "python -m arb hook claude-code" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python -m arb hook claude-code" }] }],
    "Notification":     [{ "hooks": [{ "type": "command", "command": "python -m arb hook claude-code" }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "python -m arb hook claude-code" }] }],
    "PreToolUse": [{
      "matcher": "Bash|Write|Edit|MultiEdit",
      "hooks": [{ "type": "command", "command": "python -m arb hook claude-code", "timeout": 900 }]
    }]
  }
}
```

远程审批默认关闭（只通知）。要用远程批准工具调用：

```powershell
# 编辑 %APPDATA%\agent-remote-bridge\config.json
#   "approval_mode": "approve"
```

此时 Claude 每要执行 Bash/写文件，都会先发微信问你是否允许；你回 `yes` 或 `/approve #CODE` 才放行。
**桥接不可用时永远回退到本机确认，绝不自动放行。**

### Codex（完成通知 + MCP）

`~/.codex/config.toml`：

```toml
notify = ["python", "-m", "arb", "hook", "codex"]

[mcp_servers.arb]
command = "python"
args = ["-m", "arb", "mcp", "--agent", "codex"]
```

> Codex 目前没有可拦截工具审批的 hook，因此**只支持通知 + MCP 主动提问**，不支持"远程批准 Codex 的工具调用"。这是明确的能力边界，不伪造。

### 其他 Agent（DSH / OpenCode / 任意 MCP 客户端）

- **MCP**：注册 `python -m arb mcp --agent <名字>`，即可用 `ask_human` / `notify` / `progress`。
- **无 MCP**：`python -m arb ask "问题" --option 允许 --option 拒绝 --timeout 600`
  （退出码 0=已回答, 3=超时, 4=桥接不可用）。

详见 [docs/AGENTS.md](docs/AGENTS.md)。

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

- 只有**一个**待处理问题时，直接回复 `1` / `同意` 即可。
- **多个**同时等待时，裸回复会被拒绝并列出编号，必须带 `#CODE` —— 这是防串线的关键。
- 在微信里**引用回复**某条提问，会按消息 ID 精确命中那个问题（优先级最高）。

## 设计要点

- **不重造调度**：Agent 自己的子任务/子智能体机制原样保留，桥接只搬运"需要人"的那一步。
- **阻塞只阻塞提问者**：`ask_human` 是那个 Agent 的一次工具调用，其他进程完全不受影响。
- **归属靠三层证据**：引用消息 ID > 显式 `#CODE` > "唯一待处理"。都不成立就拒绝。
- **一次性状态迁移**：`UPDATE ... WHERE state='pending'` 的 `rowcount` 决定谁赢，并发回复只有一个生效。
- **失败即拒绝**：投递失败、通道异常、超时，都不会被当成"批准"。

## 安全

- 本地 API 只监听 `127.0.0.1`，需 `X-ARB-Token`；绑定码只在未授权设备上不显示。
- 未授权微信 ID 的消息会被忽略并告警给机主。
- 凭据存于 `%APPDATA%\agent-remote-bridge\ilink.json`（0600），不写入仓库。
- 审计日志落在 SQLite 的 `audit` 表。

## 开发

```powershell
$env:PYTHONPATH="$PWD\src"
python -m unittest discover -s tests -p "test_*.py"
```

74 个离线测试覆盖：路由与串线、并发审批、超时、去重、投递失败、重启恢复、iLink 协议（本地假服务器）、MCP 协议、hook 解析。

## 许可

MIT。协议实现参考了 Tencent [openclaw-weixin](https://github.com/Tencent/openclaw-weixin)（MIT）与 [wechat-claude-code](https://github.com/Conn-Ho/wechat-claude-code)（Apache-2.0）；架构思路参考 [tlive](https://github.com/y49/tlive)（MIT）。详见 [NOTICE](NOTICE)。
