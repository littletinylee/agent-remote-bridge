# 架构

## 一句话

**Agent 自己决定"我要问人"，桥接只负责把这个问题送到微信、把答复送回来，不接管 Agent。**

这句话决定了几乎所有设计选择：不写调度器、不解析 Agent 输出、不恢复会话 —— 因为每个 Agent 的
hook / MCP 机制已经提供了"在正确的上下文里阻塞并拿到返回值"的能力。

## 分层

```
┌──────────────────────────── 进程边界 ────────────────────────────┐
│                                                                  │
│  Agent 进程 A            Agent 进程 B           Agent 进程 C      │
│  (Claude Code)           (Codex)               (DSH/任意)        │
│      │  hook stdin           │  MCP stdio          │  CLI          │
│      ▼                      ▼                     ▼               │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  arb hook  /  arb mcp  /  arb ask   （适配层，进程内）     │    │
│  └──────────────────────────┬───────────────────────────────┘    │
│                             │ HTTP 127.0.0.1 + X-ARB-Token       │
│  ┌──────────────────────────▼───────────────────────────────┐    │
│  │  守护进程 (arb serve)                                     │    │
│  │  Router ──► SQLite(WAL) ──► Outbox worker ──► Channel     │    │
│  │    ▲           sessions/questions/inbound/audit           │    │
│  │    └──────── Channel inbound ◄── 长轮询 ──┐               │    │
│  └───────────────────────────────────────────┼───────────────┘    │
└──────────────────────────────────────────────┼────────────────────┘
                                               │ 出站 HTTPS
                                    ilinkai.weixin.qq.com
                                               │
                                          手机个人微信
```

四个进程角色，三条边界：

| 边界 | 协议 | 为什么 |
|---|---|---|
| Agent ↔ 适配层 | hook JSON（stdin/argv）/ MCP stdio | 厂商原生机制，零侵入 |
| 适配层 ↔ 守护进程 | 回环 HTTP + token | 一次提问可能等 15 分钟，不能让 Agent 直接持有长连接 |
| 守护进程 ↔ 微信 | 出站 HTTPS 长轮询 | 无公网 IP 的唯一可行方式 |

## 为什么不需要中转服务器

iLink 协议的两端都是**客户端发起**：

- 收：`POST ilink/bot/getupdates` 长轮询（35s 超时），一次最多返回一批消息，带 `get_updates_buf` 断点。
- 发：`POST ilink/bot/sendmessage`（bot 主动推送，无 48h 客服窗口）。

所以本机只要能出网即可。调研里唯一现成的 relay（happy-server）已归档，而 Cloudflare/ECS 中转
在本架构里除了增加一台要维护的机器、多一份凭据、多一个故障点之外没有任何收益。

## 数据模型

```sql
sessions(id, agent, project, cwd, label, parent_id, status, session_key, meta, ...)
   -- session_key 是 Agent 自己的会话 ID（Claude session_id / Codex thread-id），
   -- 唯一索引保证"同一个 Agent 会话在多次 hook 调用之间收敛到同一行"

questions(id, code, session_id, kind, prompt, options, dangerous, state,
          answer, answer_by, answer_raw, expires_at, outbound_msg_id, delivered_at, ...)
   -- code 是 4 位人类可读编号（无 0/O/1/I/L），只在 pending 之间唯一

outbox(id, dedupe UNIQUE, kind, text, question_id, next_try, attempts, sent_at, sent_msg_id, ...)
   -- 决策通知绝不丢弃：发送失败就重试，通道断开就等

inbound(msg_id PRIMARY KEY, from_user, text, ref_svr_id, context_token, ...)
   -- 主键天然去重：微信重投 / 长轮询重放不会二次生效

audit(ts, actor, action, detail)   -- 只追加
kv(k, v)                           -- get_updates_buf、context_token 缓存
```

## 问题状态机

```
                 create
                   │
                   ▼
              ┌─────────┐  claim_answer（条件 UPDATE）   ┌──────────┐
              │ pending │ ──────────────────────────────► │ answered │
              └────┬────┘                                └──────────┘
                   │  expires_at 到期（sweeper 每 2s）
                   ▼
              ┌─────────┐
              │ expired │   ← 危险操作在这里等于"拒绝"
              └─────────┘
                   │  /cancel
                   ▼
              ┌───────────┐
              │ cancelled │
              └───────────┘
```

**并发安全只有一处**：

```sql
UPDATE questions SET state='answered', answer=?, answer_by=?, answered_at=?
 WHERE id=? AND state='pending' AND expires_at > ?
```

`rowcount == 1` 的那次回复生效，其余全部走"已经处理过了"。这是防止重复审批/并发回复的唯一真相来源，
不是靠锁、也不是靠应用层判断。

## 回复归属：四层证据，越具体越优先

| 优先级 | 证据 | 场景 |
|---|---|---|
| 1 | `ref_msg.svr_id` == 我们推送时的 `message_id` | 你在微信里**引用回复**那条提问 |
| 2 | 显式 `#CODE` | "`#K7MQ 1`" |
| 3 | 全局唯一 pending | 只有一个问题等着，直接回 "1" |
| 4 | 多个 pending 且无证据 | **拒绝**，回一张带编号的清单 |

第 4 条是防串线的核心：宁可不回答，也不猜。危险操作还会额外要求 `/approve #CODE` 或明确的
`yes`，裸序号不算。

## 线程模型

一个守护进程，四个线程，共享 SQLite（每线程一个连接，WAL）：

| 线程 | 职责 | 阻塞点 |
|---|---|---|
| channel | 长轮询 → `Router.handle_inbound` | 35s HTTP 长轮询 |
| outbox | 取 `next_try <= now` 的行 → `channel.send` | 发送 HTTP |
| sweeper | 过期扫描 + 超时通知 | 2s 定时 |
| http | 回环 API（ThreadingHTTPServer） | 每请求一个线程 |

`ask` 的等待发生在**调用方**（hook/MCP 进程），守护进程从不为它阻塞 —— 这就是"一个 Agent 等审批，
其他 Agent 照常跑"的实现原因。

## 失败语义（fail-closed）

| 故障 | 行为 |
|---|---|
| 微信发送失败 | outbox 重试（1s→2s→…→300s），问题保持 pending；**不会**被当成已送达/已批准 |
| 通道断开 | outbox 挂起，健康标志变红；恢复后自动补发 |
| 守护进程挂了 | hook 输出 `permissionDecision: "ask"`（回退本机确认）；MCP 返回 `status=unavailable` 且 `approval=false` |
| 守护进程重启 | 未发送的 outbox 继续发；pending 问题推送一条"重启后仍有 N 个问题" |
| 凭据失效（-14） | 通道标记 unhealthy 并明确提示；守护进程每 30s 重试一次，**重新 `arb login` 后自动加载新凭据，无需重启**（通道启动时会重读 `ilink.json`） |
| 重复投递 | `inbound.msg_id` 主键冲突 → 忽略 |
| 重复回复 | 条件 UPDATE 输掉 → 回"已处理"，审计记录 `answer_rejected_race` |
| 超时 | 状态置 expired，推送超时通知；危险操作按"拒绝"处理 |

## 与开源项目的关系

- **iLink 协议**来自 Tencent/openclaw-weixin（MIT）与 Conn-Ho/wechat-claude-code（Apache-2.0）的交叉验证；
  本项目的客户端是独立 Python 实现，不是它们代码的移植。
- **"骑 hook + 审批卡片"** 的架构来自 tlive（MIT）；它验证了这条路线可行，但没有微信通道。
- 没有 fork 任何一个项目：都是 TS/Node ≥22 的运行时，fork 会把"零依赖、一个 Python 进程"变成
  "Node 工具链 + 上游合并冲突"。

详见 [RESEARCH.md](RESEARCH.md) 与仓库根目录 [NOTICE](../NOTICE)。
