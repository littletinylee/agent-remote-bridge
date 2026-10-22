# 配置项参考

配置文件：`%APPDATA%\agent-remote-bridge\config.json`（Linux/macOS 为
`~/.config/agent-remote-bridge/config.json`；可用 `ARB_HOME` 改目录）。

改完**不需要重启**的项会在下一次用到时生效，表里注明。JSON 支持 UTF-8 BOM（记事本保存也不会坏）。

| 键 | 默认 | 作用 | 生效方式 |
|---|---|---|---|
| `port` | `8848` | 本机回环 API 端口 | 重启守护进程 |
| `api_token` | 自动生成 | 回环 API 的 `X-ARB-Token`。**为空时拒绝一切请求**，不会退化成无鉴权 | 重启 |
| `pairing_code` | 自动生成 | `/pair` 绑定码，绑定成功后自动轮换 | 即时（`arb status` 查看） |
| `owner_ids` | `[]` | 允许发指令的微信用户 ID；扫码登录会自动写入 | 即时 |
| `channel` | `ilink` | `ilink`（个人微信）或 `console`（终端，用于调试/无微信环境） | 重启（`arb serve --channel console` 可临时覆盖） |
| `ilink_base_url` | `https://ilinkai.weixin.qq.com` | 微信 bot 服务地址 | 重启 |
| `ilink_bot_type` | `"3"` | 登录二维码的 bot 类型参数 | 下次 `arb login` |
| `ask_timeout_s` | `900` | `ask` 的默认等待时长 | 即时 |
| `question_ttl_s` | `1800` | 问题最长存活；到期按 `expired` 处理 | 即时 |
| `notify_min_interval_s` | `20` | `progress`/`info` 类通知的最小间隔（防刷屏） | 即时 |
| `max_push_chars` | `1500` | 单条微信消息的最大字符数，超出按行分段 | 即时 |
| `outbox_retry_max_s` | `300` | 发送失败的最大重试间隔（指数退避上限） | 即时 |
| `channel_retry_s` | `30` | 通道断开后重连间隔 | 即时 |
| `approval_mode` | `notify` | `notify` 只推送；`approve` 会拦下命中的工具调用等你批准 | 下次 hook 调用 |
| `approval_tools` | `Bash, Write, Edit, MultiEdit, NotebookEdit, WebFetch` | 需要远程批准的工具名（支持正则） | 下次 hook 调用 |
| `approval_timeout_s` | `600` | 等待批准的最长时间，必须 ≤ Claude hook 的 `timeout` | 下次 hook 调用 |
| `on_timeout` | `deny` | 批准超时后的决策：`deny` / `ask`（回退本机确认）/ `allow`（**不建议**） | 下次 hook 调用 |
| `approval_max_per_session_per_min` | `12` | 预留的限流参数 | — |
| `quiet` | `false` | 预留 | — |
| `log_level` | `INFO` | 预留 | — |

## 常用改动

**只推送、不拦截**（默认，推荐日常使用）

```json
{ "approval_mode": "notify" }
```

**离开电脑时打开远程审批**

```powershell
arb approval on
```

**让提问等更久**（默认 15 分钟）

```json
{ "ask_timeout_s": 3600, "question_ttl_s": 7200 }
```

**把 `Write` 也纳入审批，但放过只读命令**

```json
{ "approval_tools": ["Bash", "Write", "Edit", "MultiEdit"] }
```

**临时用终端代替微信**（不需要扫码，适合排查问题）

```powershell
arb serve --channel console
```

## 不要手改的东西

- `ilink.json`：微信凭据（bot token）。用 `arb login` / `arb logout` 管理。
- `arb.sqlite3`：会话/问题/审计数据库。备份直接复制（先 `arb stop`）。

## 环境变量

| 变量 | 作用 |
|---|---|
| `ARB_HOME` | 覆盖配置目录（多实例、测试用） |
| `ARB_SESSION_ID` | 让 Agent 的会话 ID 显式指定（区分主/子智能体） |
| `ARB_SESSION_LABEL` | 微信里显示的会话名 |
| `ARB_PARENT_SESSION` | 标记子智能体从属的主会话 |
| `ARB_AGENT` | 默认 Agent 名（用于通用的 `arb hook` / MCP） |
| `ARB_DEBUG` | 打印完整异常栈到 stderr |
| `MCP_TIMEOUT` | Claude Code 侧 MCP 工具超时（毫秒），长等待时调大 |
