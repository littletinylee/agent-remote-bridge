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

## 3. 并发 / 非阻塞 / 不串线（通过）

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

## 4. 安全性质（离线测试）

| 性质 | 测试 |
|---|---|
| 未授权微信 ID 无法回答问题，机主会收到告警 | `TestAuthorization.test_unknown_sender_is_rejected_and_owner_warned` |
| 绑定码错误不会绑定；正确才绑定且持久化 | `test_pairing_requires_the_right_code` |
| 多问题并发时裸回复被拒绝（不猜） | `TestAmbiguity.test_two_pending_refuses_bare_text` |
| 危险操作裸序号不算批准 | `TestDangerousGate.test_bare_number_is_not_enough_for_dangerous` |
| 第二次回答被拒，第一次生效 | `test_second_answer_is_rejected` |
| 通道故障不会变成"已批准" | `test_failed_delivery_never_counts_as_an_answer` |
| 重复投递不重复生效 | `test_duplicate_inbound_message_id_is_ignored` |

## 5. 微信真实通道（待你扫码，未完成）

| # | 验证项 | 方法 | 状态 |
|---|---|---|---|
| W1 | 主动推送真的到手机微信 | `arb test-push` → 手机收到 | ⏳ 待扫码 |
| W2 | 手机回复能驱动 Agent 继续 | 微信回 `1` → 被阻塞的 Agent 继续 | ⏳ 待扫码 |
| W3 | 长时间无交互后仍能主动推送 | 隔 ≥1 小时再 `arb test-push` | ⏳ 待扫码 |

已完成的前置证据：

- `arb doctor` → `✓ iLink 接口可达 — 100ms ret=0`（真实端点，未登录状态的 `get_bot_qrcode`）；
- `tests/test_ilink.py` 用本地假服务器逐字验证了登录、长轮询、发消息、`-14` 失效、去重；
- 协议字段与腾讯官方 `openclaw-weixin`、`wechat-claude-code` 的实现交叉核对一致（见 RESEARCH.md）。

**这三项我不会用 console 通道的结果代替。** 扫码完成后本文件会被更新为实测记录（含时间戳与截图/日志摘录）。
