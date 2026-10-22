# 接入各 Agent

三种接入方式，按侵入性从低到高：

| 方式 | 需要 Agent 支持 | 能给什么 | 本机验证状态 |
|---|---|---|---|
| **MCP** | MCP 客户端（几乎所有新一代 Agent） | 主动提问（阻塞式 `ask_human`）、通知、进度 | ✅ Claude Code、Codex 实测 |
| **Hooks** | 厂商自己的 hook 机制 | 自动推送事件、**远程审批工具调用** | ✅ Claude Code 实测 |
| **CLI** | 能执行 shell 命令即可 | 主动提问、通知 | ✅ Codex 实测（通用兜底） |

> 只有 MCP 就能完成"问人 → 拿答案 → 继续"。hooks 是让它**自动**在关键时刻通知你、并在危险操作前
> 拦下来问你。CLI 是没有任何扩展机制的 Agent 的兜底。

---

## Claude Code（2.x）

### 1) MCP：让 Claude 能主动问你

```powershell
arb hooks install     # 顺带生成 %ARB_HOME%\arb-mcp.cmd（自带 PYTHONPATH）
claude mcp add arb -- "%APPDATA%\agent-remote-bridge\arb-mcp.cmd" --agent claude-code
```

如果你已经把本包 `pip install` 进环境（`arb` 在 PATH 上），也可以直接：

```bash
claude mcp add arb -- arb mcp --agent claude-code
```

之后你在对话里说"需要我决定时用 arb 的 ask_human 问我"，或直接在 `CLAUDE.md` / 项目说明里写死这条规则。

> MCP 工具调用默认有客户端超时。若你希望它等很久，设置 `MCP_TIMEOUT`（毫秒），例如
> `set MCP_TIMEOUT=900000`。等太久不划算时，可以让 Agent 用 `check_answer` 轮询。

### 2) Hooks：自动通知 + 远程审批

一条命令搞定（幂等、可撤销、自动备份原文件）：

```powershell
arb hooks install      # 写入 ~/.claude/settings.json 并生成启动器
arb hooks status       # 查看
arb hooks uninstall    # 撤销（只删本项目加的条目）
```

它把命令指向 `%ARB_HOME%\arb-hook.cmd` 而不是 `python -m arb ...`，因为 Claude 启动钩子时
**不会带上本仓库的 `PYTHONPATH`** —— 直接写 `python -m arb` 会 `ModuleNotFoundError`（实测踩过）。

想手工维护的话，[hooks/claude-settings.example.json](../hooks/claude-settings.example.json) 是等价的手写版。
要点：

| 事件 | 作用 |
|---|---|
| `SessionStart` / `UserPromptSubmit` | 推送"开始新任务" |
| `Notification` | Claude 需要你注意时推送 |
| `Stop` / `SubagentStop` | 推送"一轮结束"，附带最后一段回复 |
| `SessionEnd` | 会话结束 |
| `PreToolUse` | **远程审批**（只在 `approval_mode=approve` 时生效） |

一个 hook 命令长这样（`arb hooks install` 会自动写成这个形式）：

```json
{ "type": "command", "command": "\"C:\\Users\\you\\AppData\\Roaming\\agent-remote-bridge\\arb-hook.cmd\"", "timeout": 900 }
```

`timeout` 必须 ≥ 配置里的 `approval_timeout_s`，否则 Claude 会先超时。

### 3) 打开远程审批

```json
// %APPDATA%\agent-remote-bridge\config.json
{ "approval_mode": "approve", "approval_timeout_s": 600, "on_timeout": "deny" }
```

之后 Claude 每次要跑 `Bash` / 写文件，微信会收到一条带 `#CODE` 的危险操作询问：

- `/approve #CODE` 或回复 `yes` → 放行
- `/deny #CODE` 或超时 → 拒绝（超时永远按拒绝处理）
- 桥接挂了 → hook 返回 `"ask"`，回退到本机终端确认（**绝不静默放行**）

`approval_tools` 可以精确控制拦哪些工具（默认 `Bash/Write/Edit/MultiEdit/NotebookEdit/WebFetch`，
支持正则）。

---

## Codex（0.16x）

### 1) 完成通知：`notify`

`~/.codex/config.toml`：

```toml
notify = ["python", "-m", "arb", "hook", "codex"]
```

**实测契约**：Codex 把事件 JSON 作为**最后一个 argv 参数**传入（不是 stdin），形如

```json
{"type":"agent-turn-complete","thread-id":"01a161af-…","turn-id":"…",
 "cwd":"D:\\proj\\svc","client":"codex_exec",
 "input-messages":["…"],"last-assistant-message":"DONE"}
```

`arb hook codex` 已兼容 argv 与 stdin 两种方式，并用 `thread-id` 作为会话键，所以同一个 Codex 线程
的多次通知会归到同一个会话。

### 2) MCP：让 Codex 主动问你

```toml
[mcp_servers.arb]
command = "python"
args = ["-m", "arb", "mcp", "--agent", "codex"]
startup_timeout_sec = 30

[mcp_servers.arb.env]
PYTHONPATH = "D:\\path\\to\\agent-remote-bridge\\src"
ARB_HOME   = "C:\\Users\\you\\AppData\\Roaming\\agent-remote-bridge"
```

> **坑（实测）**：Codex 把 MCP 工具调用视为"需要审批"。在 `approval_policy = "never"`（`codex exec`
> 的默认）下，工具调用会被**直接拒绝**，表现为
> `The tool call was rejected because it requires approval, but the approval policy is set to "never"`。
>
> 解决办法（任选）：
> - 交互式使用时在 Codex 里批准一次该 MCP 服务器；
> - 非交互场景加 `--approve-for-me`；
> - 或用 `--dangerously-bypass-approvals-and-sandbox`（仅在你信任的环境）。

### 3) 命令行兜底（不需要 MCP）

只要 Codex 能执行 shell 命令：

```text
python -m arb ask "用 Postgres 还是 SQLite?" --option Postgres --option SQLite --timeout 300 --agent codex
```

stdout 输出 JSON：`{"status":"answered","code":"RQF4","answer":"Postgres",…}`。
退出码：`0` 已回答 / `3` 超时 / `4` 桥接不可用。

---

## DSH / OpenCode / 其他 MCP 客户端

注册一个 MCP 服务器即可：

```json
{
  "mcpServers": {
    "arb": {
      "command": "python",
      "args": ["-m", "arb", "mcp", "--agent", "dsh"],
      "env": { "PYTHONPATH": "<repo>/src", "ARB_HOME": "<你的 ARB_HOME>" }
    }
  }
}
```

工具清单：

| 工具 | 说明 |
|---|---|
| `ask_human(prompt, options?, timeout_seconds?, dangerous?, context?)` | 阻塞提问，返回 `{status, code, answer}` |
| `notify(title, body?, kind?)` | 推送通知（start/progress/done/error/attention/info） |
| `progress(message)` | 进度推送（服务端限流） |
| `check_answer(code)` | 非阻塞查询 |
| `list_pending()` | 当前所有待处理问题 |

`ask_human` 返回 `status=timeout` / `unavailable` 时**必须**当作"未批准"，工具描述里也这么写了，
模型会照做。

---

## 子智能体 / 多会话

不需要额外配置：

- 每个 Agent 进程有自己的 session（Claude 用 `session_id`，Codex 用 `thread-id`，其他用 `ARB_SESSION_ID` 或 cwd）。
- 主 Agent 和它的子智能体如果共用同一个 hook 进程，就会归到同一个会话；如果你希望子智能体独立成
  一个会话（这样微信里能区分是谁在问），在启动子智能体时设置：

```bash
set ARB_SESSION_ID=<唯一ID>
set ARB_SESSION_LABEL=子任务-重构登录
set ARB_PARENT_SESSION=<主会话ID>
```

`arb` 会把它作为独立 session 显示在 `/projects` 和每条提问的"项目:"行里。

---

## 任何能跑命令的东西（脚本、CI、定时任务）

```bash
arb notify "夜间构建完成" --body "12 个测试通过" --kind done --project nightly
arb ask "要合并到 main 吗?" --option 合并 --option 等待 --dangerous --timeout 600
```

`--dangerous` 会强制要求微信端用 `/approve` 或 `yes` 明确批准，超时视为拒绝。
