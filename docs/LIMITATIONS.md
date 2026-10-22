# 能力边界（已实现 / 已实测 / 部分支持 / 不可实现）

这份文件的原则：**不把模拟当实测，不把"能编译"当"能用"。**

## 一、已经在本机实测通过（有记录文件）

| 能力 | 证据 |
|---|---|
| Claude Code：`PreToolUse` 拦下 Bash → 微信提问 → `yes` → **同一轮继续执行**并真的写文件 | [verification/e2e-claude.json](verification/e2e-claude.json) |
| Claude Code：`UserPromptSubmit` / `Stop` 自动推送（Stop 带最后一段回复） | 同上（daemon 输出里可见"开始新任务"与"一轮任务结束 DONE"） |
| Codex：`notify` 事件 → 推送，按 `thread-id` 归并会话 | [verification/e2e-codex.json](verification/e2e-codex.json) |
| Codex：MCP `ask_human` → 回答 → Codex 回复 "Postgres" | 同上 `stage2a_stdout_tail` |
| Codex：CLI 兜底 `arb ask` → 回答 → Codex 回复 "Postgres" | 同上 `stage2b_stdout_tail` |
| 并发非阻塞：A/C 在 B 等待期间完成，B 最后才结束 | [verification/e2e-concurrent.json](verification/e2e-concurrent.json) 时间线 |
| 双问题并发不串线：`#RQF4`→Postgres 给 Codex-A，`#DSZY`→File 给 Codex-B | 同上 phase2 |
| iLink 协议：扫码登录流程、长轮询收、发消息、`-14` 失效、去重 | `tests/test_ilink.py`（对着本地假服务器，真实 HTTP） |
| iLink 真实端点可达（`get_bot_qrcode` 返回 `ret=0`，~100ms） | `arb doctor` |
| 路由/审批/超时/重启恢复/投递失败 | `tests/test_router.py`、`tests/test_daemon.py`、`tests/test_store.py` |
| MCP 协议（initialize / tools/list / tools/call / Content-Length 与换行两种分帧） | `tests/test_mcp.py` |

## 二、需要你本人操作才能完成的一步（当前未完成）

**用真实个人微信收发消息** —— 需要你手机扫码一次。技术链路已就绪：

- 协议实现已有本地假服务器测试（真实 HTTP，覆盖登录/收/发/-14）；
- 真实端点已确认可达；
- 但"你的微信真的收到了一条消息、你回复后 Agent 继续"这一段，**必须扫码后由我们共同验证**，
  我不会拿 console 通道的结果冒充。

扫码后要补的三项验证（`docs/VERIFICATION.md` 里的 W1/W2/W3）：
W1 主动推送到达手机微信；W2 手机回复能驱动提问的 Agent 继续；W3 长时间无交互后仍可主动推送。

## 三、部分支持 / 有明确限制

| 项 | 现状 | 限制 |
|---|---|---|
| Codex 远程审批工具调用 | **不支持** | Codex 没有可拦截工具审批的 hook；它只能通知 + 通过 MCP/CLI 主动提问。不伪造。 |
| Codex MCP 工具调用 | 需放宽审批 | 默认 `approval_policy=never` 会直接拒绝 MCP 工具调用，需 `--approve-for-me` 或交互批准一次。见 [AGENTS.md](AGENTS.md) |
| DSH / OpenCode / 其他 | 设计上支持 MCP/CLI | 未在本机实测（本机只装了 Claude Code 与 Codex）。不声称已验证。 |
| 微信富媒体 | 只发纯文本 | 图片/文件/语音下行需要 CDN 上传 + AES-128-ECB（协议已知，未实现）；上行语音/图片未处理 |
| 群聊 | 不支持 | 只处理与 bot 的一对一会话 |
| 多机 | 未实现 | 一个 ARB_HOME 对应一个守护进程；同一 bot 多机需要额外路由 |
| 长文本 | 1500 字符分段 | 微信无 Markdown 渲染，表格/代码块会被压成纯文本 |
| 审批时限 | 受 hook 超时约束 | Claude 的 hook `timeout` 必须 ≥ `approval_timeout_s`，否则先被 Claude 掐断（此时按"本机确认"处理） |
| MCP 阻塞时长 | 受 MCP 客户端超时约束 | Claude Code 可用 `MCP_TIMEOUT` 调整；也可以改用 `check_answer` 轮询 |

## 四、明确不做的事（设计取舍，不是缺陷）

- **不做 Agent 调度 / DAG / 工作流引擎**：Agent 自己的子任务机制更好，重造只会打架。
- **不解析 Agent 的终端输出**（除了 hook 给的 JSON 和 Stop 时读 transcript 尾部 80 行）。
- **不做会话恢复**：`claude --resume` / `codex resume` 是它们自己的事；我们只需要"不要打断它"。
- **不部署云端中转**：没有需求。见 [ARCHITECTURE.md](ARCHITECTURE.md)。
- **不做 Web 管理后台**：`arb status` / `/status` 够用。

## 五、如果 iLink 通道不可用（平台风险）

按影响从小到大：

1. **凭据失效（-14）**：`arb login` 重新扫码即可，期间 pending 问题依然可答（本机 `/api/answer`）。
2. **通道临时故障**：outbox 会一直重试并在恢复后补发，不会丢决策请求。
3. **通道被平台关闭**：改用备用通道（架构上已有抽象）：
   - **企业微信自建应用 + 微信插件**：需要公网回调地址（可用阿里云 ECS），`Channel` 接口已预留；
   - **Server酱 / PushPlus（单向推送）**：只能通知，回答改在本机用 `arb answer` 或 `arb pending`；
   - **telegram/飞书等**：同 tlive 路线，写一个 channel 即可（本项目只实现了 ilink 与 console）。

具体迁移步骤见 [DEPLOY.md](DEPLOY.md#备用消息通道)。
