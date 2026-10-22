# 安全模型

这个工具把"能否在你机器上执行命令"的决策权搬到了微信上，所以安全边界必须写清楚。

## 信任边界

```
[微信账号] ──► iLink 服务 ──► 本机守护进程 ──► Agent（可执行任意代码）
   ①              ②                  ③                    ④
```

| # | 边界 | 控制手段 |
|---|---|---|
| ① | 谁能发指令 | **允许列表**：只有 `owner_ids` 里的微信用户 ID 才算数；其他来源的消息被忽略并告警给机主 |
| ② | 传输 | HTTPS；bot token 存本机 `ilink.json`（0600），不入库不入仓 |
| ③ | 本机 API | 只监听 `127.0.0.1`，每个请求要 `X-ARB-Token`（32 字节随机，配置里 0600） |
| ④ | Agent 行为 | 危险操作必须显式批准；桥接故障时回退本机确认，绝不自动放行 |

## 首次绑定（配对码）

1. `arb login` 扫码后，服务端会返回扫码者自己的 `ilink_user_id`，本机直接登记为 owner。
2. 其它情况下用 `/pair <绑定码>`：绑定码在 `arb status` 可见，**只在本机显示**，不会发给未授权的发送者。
3. 未授权发送者的消息：忽略 + 给机主发一条告警（含来源 ID 与内容摘要），每小时最多一条。

> 也就是说：别人就算拿到了机器人的联系方式，也只能触发"告警"，不能让你的 Agent 做任何事。

## 危险操作

"危险"由提问方声明（`dangerous: true`），典型来源是 Claude Code 的 `PreToolUse` 审批。

| 行为 | 普通问题 | 危险问题 |
|---|---|---|
| 裸序号回复（`1`） | 接受 | **拒绝**（要求明确表态） |
| `#CODE 1` | 接受 | **拒绝** |
| `yes` / `允许` | 接受 | 接受 |
| `/approve #CODE` | 接受 | 接受 |
| 超时 | 标记 expired，不做处理 | **标记 expired = 拒绝** |
| 通道故障 | 保持 pending | 保持 pending，到期即拒绝 |
| 桥接进程不可用 | 返回 `unavailable` | 返回 `unavailable` + Claude 回退本机确认 |

代码位置：`Router._answer()`（危险判断）、`cli._claude_approval()`（hook 决策）。

## 幂等与重放

- **入站去重**：`inbound.msg_id` 是主键。长轮询重放、微信重投都不会让同一条回复生效两次。
- **一次性状态迁移**：`UPDATE ... WHERE state='pending'` 的 `rowcount` 决定唯一赢家。
- **发送幂等**：outbox 用 `dedupe` 唯一键，同一个问题只会推送一次（重启不会刷屏）。
- **过期回复**：`expires_at` 到期的回复直接被拒，不会"补批"。

## 凭据与敏感数据

| 数据 | 位置 | 保护 |
|---|---|---|
| 微信 bot token | `%APPDATA%\agent-remote-bridge\ilink.json` | 0600；只在进程内存与请求头里出现 |
| 本地 API token | `config.json` | 0600；仅回环可达 |
| 审计/问题/会话 | `arb.sqlite3` | 本机文件；不含 token |
| 日志 | `arb.log` | 出错信息经过 `redact()`，token/`sk-…` 会被打码 |

`.gitignore` 排除 `config.json` / `ilink.json` / `*.sqlite3` / `*.log` / `qr-*.png`。

## 已知残余风险（如实说明）

1. **bot 通道本身由腾讯控制**：如果该通道被封禁或协议变更，桥接会失效（不会因此放行任何操作，但你会
   失去远程监督能力）。见 [LIMITATIONS.md](LIMITATIONS.md) 的备用方案。
2. **手机被他人解锁**：那就是拿到了你的微信，等价于拿到了你的机器。建议开启微信锁。
3. **Prompt injection**：Agent 可能被仓库里的文本诱导去调用 `ask_human` 问奇怪的问题，但它无法伪造
   你的批准；危险操作仍然要你点头。
4. **本机同用户进程**：能读 `config.json` 的本机进程就能调用 API。这是同一信任域内的假定，没有
   额外的进程隔离。
5. **审计是本地可信的**：拥有 root/管理员权限的人可以改库。它不是防篡改日志。

## 建议

- 生产使用时把 `on_timeout` 保持为 `deny`（默认），不要为了"方便"改成 `allow`。
- `approval_tools` 不要包含只读工具（`Read`/`Grep`），否则会被无关审批淹没，反而容易误批。
- 定期看一眼 `audit` 表：`sqlite3 arb.sqlite3 "select ts,actor,action,detail from audit order by id desc limit 20"`。
- 怀疑泄露时：`arb logout`（清凭据）→ GitHub/腾讯侧吊销 → `arb login` 重新绑定。
