# 验证矩阵（真机证据）

本文件只记录**有证据**的结论，每条附复现方式。
状态：`已验证`（真机跑过）、`仅单测/集成测试`（代码 + 自动化测试，未真机）、`未完成`。

最后更新：2026-10-22（Hub 已上线并接管微信）

## 基础设施

| 项 | 状态 | 证据 |
|---|---|---|
| Hub 常驻（systemd） | 已验证 | `systemctl is-active arb-hub` → `active` |
| Hub 跑的是 pip 安装版 | 已验证 | 日志进程名 `arb[131675]`，`ExecStart=/usr/local/bin/arb hub serve`，无 `PYTHONPATH` |
| TLS 监听 | 已验证 | `ss -lntp \| grep 18443` → `0.0.0.0:18443` |
| 跨公网可达 | 已验证 | `curl -k https://120.78.79.184:18443/v1/health` → `{"ok":true,"nodes":3,…}` |
| 证书指纹固定 | 已验证 | 观测 `5121E30E…3F1D581A` == 钉住值；不匹配则拒绝连接 |
| 安全组放行 18443 | 已验证 | 本机 TCP 探测 `filtered` → 放行后 `OPEN` |
| Hub 独占微信 | 已验证 | 本机 daemon 停止时 `wechat_logged_in=true` 且能推送 |
| pip 安装（零依赖） | 已验证 | 干净目录 `pip install --target` → `arb --version` → `0.4.1` |

## 路由与安全

| 项 | 状态 | 证据 |
|---|---|---|
| 决策码路由到提问机器 | 已验证 | Hub 库 `questions.N63C.node_id=VNyXQQZs8nE`（workbench） |
| 未知/过期编号拒绝 | 已验证 | 真机回 `#ZZZZ`→「没有该编号」；TTL 过期后不可路由（测试覆盖） |
| 多待决拒猜 | 已验证 | 真机裸回 `1` 三次，每次回「有 2 个待决策，请带编号回复」 |
| 节点拥有问题状态 | 已验证 | Hub `state` 保持 pending 直到节点 `/v1/question/close` |
| 问题 TTL 自动回收 | 已验证 | Hub 每分钟 reaper，陈旧项标记 `expired` |
| 跨机隔离 | 集成测试 | `test_hub.TestIsolationAndBuffering`（A 机流量不进 B 机 inbox） |
| 离线缓冲补发 | 集成测试 | 同上（离线入队，重连后恰好取一次） |
| 未授权发送者忽略 | 已验证 | 真机出现过「来源ID: hub 被拒」→ 已修为转发真实 wxid |
| relay 入站契约 | 集成测试 | `test_relay_channel.py`：真实发送者、`#CODE` 重包、注册与关闭凭据 |
| 只读命令自动放行 | 已验证 | 真实 hook：`ls -la` → `permissionDecision: allow`，无提问 |
| 只读管道放行 | 已验证 | 真实 hook：`ls … 2>&1 \| head -50` → `allow` |
| 链式命令仍拦截 | 已验证 | 真实 hook：`ls && rm -rf x` 阻塞等人工批准 |
| 敏感路径不自动放行 | 仅单测 | `test_safe_auto.test_secrets_are_never_auto_approved` |

## 功能

| 项 | 状态 | 证据 |
|---|---|---|
| 微信冷推送 | 已验证 | 真机多次（含 60 分钟空闲后） |
| 危险操作显式批准 | 已验证 | `#3BRE`：裸 `1` 被拒，`yes` 通过并执行写文件 |
| Claude Code 审批（直连微信） | 已验证 | 历史真机记录 |
| Codex 插件 | 已验证 | `codex plugin list` installed/enabled，钩子触发推送 |
| DSH MCP 插件 | 已验证 | 拉起 `python -m arb mcp --agent dsh`；`ask_human` 返回 `answered` |
| Hub 提问到手机 | 已验证 | `#HUBQ` 推送成功，回 `1` 与 `2` 均被节点取到 |
| 引用回复路由 | 仅单测 | `test_hub.test_quoted_reply_binds_to_the_exact_message` |
| **经 Hub 的完整审批闭环** | **未完成** | 需一次人工微信回复；此前失败于发送者身份（已修） |

## M3 顾问 / M4 主动建议

| 项 | 状态 | 证据 |
|---|---|---|
| DeepSeek 调用 | 已验证 | 真机 API：问"几台机器"→「共 2 台机器，全部在线」 |
| 无审批权 | 仅单测 | 提示词契约 + 测试断言 |
| 限流 6 次/分 | 仅单测 | `test_hub_chat.test_rate_limit` |
| 已部署 | 已验证 | 日志 `[hub] chat advisor enabled (model=deepseek-chat)` |
| M4 主动建议触发 | 已验证 | 停掉 laptop2 后 Hub 主动发「laptop2, ops-console 当前离线」 |
| M4 重启不重复 / 配额持久 | 已验证 | 修复后库中不再重复；测试覆盖 |
| M4 忽略长期死节点 | 已验证 | `ops-console` 这类一次性注册不再被念叨 |

## 已知缺陷（按发现顺序，均已修）

1. DSH `- id:` 被当作覆写不存在的 id 而静默跳过 → 改用 `- insert:`。
2. DSH 解析不到包 → profile 补 `@deepseek-ai/dsh-mcp-client` 依赖。
3. `arb hub` 子命令缺失 → systemd 重启循环。
4. Hub 提前关闭问题并回「✅已收到」（假成功）→ 改为节点拥有状态。
5. 引用回复在 Hub 路径被丢弃 → 存 `questions.msg_id` + 解析 `ref_msg.svr_id`。
6. relay 发送者写成字面量 `hub` → 所有经 Hub 的答复被本地拒绝 → 转发真实 wxid。
7. 同一问题被推送两遍 → relay 模式只由 Hub 推送。
8. 快照取守护进程 cwd（主目录）→ 优先会话里的 git 仓库。
9. Hub 启动日志写死 `nodes=0` → 改为真实数量。
10. `ls … | head` 只读管道仍打扰 → 规则扩展为"每段都是只读白名单"。
11. M4 建议每次重启后重发、且对永不重连的节点反复念叨 → 持久化信号与配额 + 30 分钟近期窗口。
12. 服务器 `pip install git+…` 在 pip 22.0.2 上静默中断 → 改为本地构建 wheel 上传安装。

## 复现全部测试

```powershell
$env:PYTHONPATH="src"; python -m unittest discover -s tests -p "test_*.py"
```
