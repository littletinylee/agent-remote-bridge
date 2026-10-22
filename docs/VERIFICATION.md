# 验证记录

环境：Windows 11 + Python 3.14.5，2026-10-22。
Agent：Claude Code 2.1.181、Codex CLI 0.162.0-alpha.17.2。
通道：`console`（把推送打到守护进程 stdout，便于逐行取证；与 ilink 走同一个 Router / outbox / 状态机）。

> 为什么先用 console：它让"推送内容 + 时间线"能被机器断言。微信 leg 见文末 W1–W3，那一步必须你手机扫码，
> 我不会用 console 的结果冒充。

## 0. 自动化测试（离线，可重复）

```
$ python -m unittest discover -s tests -p "test_*.py"
Ran 74 tests in 10.2s
OK
```

覆盖：会话归并、编号唯一性、并发回答只生效一次、过期后不可回答、outbox 去重与重试、入站去重、
路由优先级（引用 > #CODE > 唯一 pending > 拒绝）、危险操作需显式批准、跨会话不串线、
投递失败不算批准、重启后 pending 仍可回答、HTTP API 鉴权、iLink 协议（本地假服务器）、
MCP 协议（两种分帧）、各 Agent 的 hook 载荷解析。

## 1. Claude Code：远程审批 → 同一轮继续（通过）

脚本 `scripts/e2e_claude.py`，记录 `docs/verification/e2e-claude.json`。

```
[daemon] 🚨 需要你决策  #9JBB
[daemon] 项目: project (claude-code)
[daemon] 问题: 请求使用工具 Bash $ echo E2E-OK > result.txt
[daemon]  1) allow   2) deny
[human] -> yes  (approving #9JBB)
[daemon] ✅ 已收到 #9JBB → allow
[daemon] ✅ 一轮任务结束 … DONE
[e2e] claude exit=0 in 8.2s
```

断言结果：`approved=true`、`artifact_exists=true`、`artifact="E2E-OK"`、`stats.answered=1`。

意义：Bash 调用被 hook **真的挡住**，只有在收到远程 `yes` 之后才执行；Claude 在同一个
`session_id`（`90e051b7-…`）里继续，没有重启、没有新会话。

## 2. Codex：notify 推送 + MCP 提问 + CLI 兜底（通过）

脚本 `scripts/e2e_codex.py`，记录 `docs/verification/e2e-codex.json`。

| 阶段 | 结果 |
|---|---|
| `notify` → 推送 | ✅ "✅ 任务完成（Codex） / 项目: project (codex) / DONE" |
| MCP `ask_human` | ✅ 回答 `Postgres` → Codex 输出 `The chosen option was "Postgres".` |
| CLI `arb ask` | ✅ 回答 `Postgres` → Codex 输出 `Postgres` |

过程中发现并修掉的两个真实问题（都在真实 CLI 上才暴露）：

1. **Codex 的 notify 载荷走 argv，不走 stdin** → `arb hook` 现在先看 argv 再看 stdin。
2. **`.sandbox-bin` 里的 Codex 0.162.0-alpha.2 缺少 helper 二进制**（`codex-command-runner.exe`、
   `codex-code-mode-host.exe`），任何工具调用都会 `host executable was not found`。改用同机完整的
   0.162.0-alpha.17.2 构建后一切正常 —— 这是环境问题，不是桥接问题。

## 3. 两条接入路径都已实测（Claude Code MCP + Codex）

脚本 `scripts/e2e_claude_mcp.py`，记录 `docs/verification/e2e-claude-mcp.json`。

```
[daemon] 🚨 需要你决策  #XAH4 … 1) Postgres  2) SQLite
[human]  -> 1
[daemon] ✅ 已收到 #XAH4 → Postgres
[claude] The option chosen was **"Postgres"**   (exit 0, 4.6s)
```

这是 **MCP 路径**（`--mcp-config` 注入、无 hooks），与第 1 节的 **hooks 路径**互补。
至此每个 Agent 的两条接入面都被真实 CLI 验证过：

| Agent | hooks / notify | MCP `ask_human` | CLI `arb ask` |
|---|---|---|---|
| Claude Code 2.1.181 | ✅ `PreToolUse` 审批 + `Stop` 通知 | ✅ #XAH4 | ✅（同一入口，Codex 侧已证） |
| Codex 0.162.0-alpha.17.2 | ✅ `notify` | ✅ 回答 "Postgres" | ✅ 回答 "Postgres" |

## 4. 并发 / 非阻塞 / 不串线（通过）

脚本 `scripts/e2e_concurrent.py`，记录 `docs/verification/e2e-concurrent.json`。一个守护进程，四个真实进程：

```
 0.61s  A=Claude Code 启动      C=Codex 启动
 1.62s  B=Codex 启动并阻塞在 ask_human
11.46s  推送问题 #S5VY ；A 完成（文件 plan_a.md = PLAN-A-OK）
17.27s  C 完成（note_c.txt）；此时人类才回答 #S5VY
25.82s  B 带着答案继续并结束
```

phase2（两个问题同时等待）：

```
#RQF4  "Which database?"  回答 1 → Codex-A 回复 Postgres
#DSZY  "Which cache?"     回答 2 → Codex-B 回复 File
```

断言：`A_done`、`C_done`、`A_and_C_overlapped_B_wait`、`B_finished_last`、`B1_said_postgres`、
`B2_said_file` 全为 true。

意义：这正好是验收场景（A 写代码、B 要决策、C 干别的）—— **A 和 C 在 B 阻塞期间完成**，
B 最后带着你的答复继续，且两个并发问题的答案没有互换。

## 5. 安全性质（离线测试）

| 性质 | 测试 |
|---|---|
| 未授权微信 ID 无法回答问题，机主会收到告警 | `TestAuthorization.test_unknown_sender_is_rejected_and_owner_warned` |
| 绑定码错误不会绑定；正确才绑定且持久化 | `test_pairing_requires_the_right_code` |
| 多问题并发时裸回复被拒绝（不猜） | `TestAmbiguity.test_two_pending_refuses_bare_text` |
| 危险操作裸序号不算批准 | `TestDangerousGate.test_bare_number_is_not_enough_for_dangerous` |
| 第二次回答被拒，第一次生效 | `test_second_answer_is_rejected` |
| 通道故障不会变成"已批准" | `test_failed_delivery_never_counts_as_an_answer` |
| 重复投递不重复生效 | `test_duplicate_inbound_message_id_is_ignored` |

## 6. 真实个人微信（已实测）

机主于 12:00 前后用手机微信扫码登录（bot `ffbb…@im.bot`），随后完成了下列真实交互。
逐条证据见 `docs/verification/wechat-transcript.json`（数据库原始记录，已脱敏：微信号与账号 id 只保留前 4 位）。

| 时间 | 谁 | 内容 | 结果 |
|---|---|---|---|
| 11:59 | 桥接 | 「测试推送 / 如果你在微信里看到这条消息…」 | ✅ 手机收到（**冷推送**：登录后机主还没发过言，无 `context_token`） |
| 11:59:35 | 机主 | `你好` | ✅ 机器人回「当前没有等待回复的问题…」 |
| 11:59:57 | 机主 | `/status` | ✅ 机器人回状态卡片 |
| 12:01 | 桥接 | `🚨 需要你决策 #DJDT`（两项选择） | ✅ 手机收到 |
| 12:02:04 | 机主 | `1` | ✅ `answered:DJDT → 继续执行`，**阻塞中的进程 exit 0** |
| 12:02 | 桥接 | `🚨 需要你决策 #3BRE` 危险操作：`Bash $ echo WECHAT-E2E-OK > wechat_result.txt` | ✅ 手机收到（真实 Claude Code 的 `PreToolUse` 被卡住） |
| 12:02:54 | 机主 | `1` | ⛔ **被拒绝**：`dangerous_needs_explicit`，机器人在微信里回「必须明确批准」 |
| 12:03:31 | 机主 | `yes` | ✅ `answered:3BRE → allow` |
| 12:03:3x | Claude Code | 执行命令 | ✅ 生成 `wechat_result.txt` = `WECHAT-E2E-OK`，`claude_exit=0`，耗时 67.8s |

记录文件：[verification/e2e-wechat-agent.json](verification/e2e-wechat-agent.json)。

这一次交互同时验证了四件事：

1. **主动推送**（W1）—— 不需要你先说话（无 `context_token` 的冷推送也成功，这正是"长时间无交互仍可推送"的机制）。
2. **准确路由 + 原会话继续**（W2）—— 你的 `yes` 只交给了等在那里的那个 Claude Code 会话，它带着批准继续执行并真的落了盘。
3. **危险操作门禁** —— 同一个问题，裸序号 `1` 被拒、明确 `yes` 才放行。这不是测试桩，是你手机上的真实操作。
4. **中文全链路** —— 问题、选项、答复、确认消息都是中文，编码无损。

### W3：长时间无微信交互后的推送

- 已证明的机制：`sendmessage` 是 bot 主动推送，本项目的冷推送（无 `context_token`）成功，说明不依赖 48 小时客服窗口。
- 仍待实测的一步：间隔 ≥1 小时完全不发消息，再执行 `arb test-push`，确认仍然送达。
  这一项被安排在稍后用定时提醒复测，结果会追加到本节。

## 7. 结论

| 验收项 | 状态 |
|---|---|
| 多个 Agent 同时运行（真实进程） | ✅ |
| B 遇到问题主动微信提问 | ✅ 真机 |
| 等待期间 A、C 继续执行 | ✅ 时间线 |
| 微信回复后 B 在原上下文继续 | ✅ 真机（Claude Code 的同一 session） |
| 任务完成后主动推送结果 | ✅ 真机 |
| 多会话无串线 | ✅ 离线 + 真机（两个问题各自归属） |
| 多并发问题准确回复 | ✅ 离线 + 真机 |
| 渠道异常不会错误批准 | ✅ 测试 + 真机（裸 `1` 被拒） |
| 断线恢复 | ✅ 测试（重启后 pending 仍可答；投递失败会重试） |
| 长时间无交互仍能推送 | ⏳ W3 待 1 小时后复测（机制已成立） |
| 核心代码按里程碑提交 | ✅ 本地 13 个提交（push 待 GitHub 仓库创建） |
| 两种以上 Agent 真实验证 | ✅ Claude Code + Codex，hooks / MCP / CLI 三条路径 |
