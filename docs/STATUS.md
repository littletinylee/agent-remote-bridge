# 验证矩阵（真机证据）

本文件只记录**有证据**的结论。每一条都附了复现命令，任何人可以重跑。
状态只有三种：`已验证`（真机跑过）、`仅单测`（代码+测试，未真机）、`未做`。

最后更新：2026-10-22

## 基础设施

| 项 | 状态 | 证据 / 复现 |
|---|---|---|
| Hub 常驻 | 已验证 | `ssh root@120.78.79.184 systemctl is-active arb-hub` → `active` |
| TLS 监听 | 已验证 | `ss -lntp \| grep 18443` → `0.0.0.0:18443` |
| 跨公网可达 | 已验证 | `curl -k https://120.78.79.184:18443/v1/health` → `{"ok":true,...}` |
| 证书指纹固定 | 已验证 | 观测 `5121E30E…3F1D581A` == 节点配置里的钉住值；不一致则拒绝连接 |
| 安全组放行 | 已验证 | 本机 TCP 探测 18443：`filtered` → 放行后 `OPEN` |
| 安装（pip，无 PYTHONPATH） | 已验证 | 干净目录 `pip install --target` → `arb --version` → `agent-remote-bridge 0.4.1` |
| Hub 独占微信 | 已验证 | 本机 daemon 停止时 `wechat_logged_in=true`，且能推送 |

## 路由与安全

| 项 | 状态 | 证据 / 复现 |
|---|---|---|
| 决策码路由到提问机器 | 已验证 | 落库记录：`questions.N63C.node_id=VNyXQQZs8nE`（workbench） |
| 未知编号拒绝 | 已验证 | 单测 + 真机：回 `#ZZZZ` 得到「没有 #ZZZZ」 |
| 多待决拒猜 | 已验证 | 真机微信：裸回 `1` 三次 → 每次都收到「有 2 个待决策，请带编号回复」 |
| 节点拥有问题状态 | 已验证 | Hub 库中 `state` 保持 pending 直到节点 `/v1/question/close` |
| 跨机隔离 | 仅单测 | `test_hub.TestIsolationAndBuffering` |
| 离线缓冲补发 | 仅单测 | `test_hub.TestIsolationAndBuffering` |
| 未授权发送者忽略 | 已验证 | 真机：`来源ID: hub` 被拒（后修复为转发真实 wxid） |
| 只读命令自动放行 | 已验证 | 真实 hook：`ls -la` → `permissionDecision: allow`，无提问 |
| 管道只读放行 | 已验证 | 真实 hook：`ls ... 2>&1 \| head -50` → `allow` |
| 链式命令仍拦截 | 已验证 | 真实 hook：`ls && rm -rf x` 阻塞 8s 等人工批准 |
| 敏感路径不自动放行 | 仅单测 | `test_safe_auto.test_secrets_are_never_auto_approved` |

## 功能

| 项 | 状态 | 证据 / 复现 |
|---|---|---|
| 微信冷推送 | 已验证 | 真机收消息（多人轮次，含 60 分钟空闲后） |
| 危险操作显式批准 | 已验证 | `#3BRE`：裸 `1` 被拒，`yes` 通过并执行写文件 |
| 真实 Claude Code 审批 | 已验证（直连版） | 直连微信时期跑通；**经 Hub 的版本未完成**（见下） |
| 真实 Codex 插件 | 已验证 | `codex plugin list` → installed/enabled；钩子触发推送 |
| DSH MCP 插件 | 已验证 | DSH 启动后拉起 `python -m arb mcp --agent dsh`（PID 4728）；`ask_human` 返回 `answered` |
| Hub 提问到手机 | 已验证 | `#HUBQ` 推送成功，回 `1` 被节点取到 |
| **引用回复路由** | 仅单测 | `test_hub.test_quoted_reply_binds_to_the_exact_message`；真机待一次引用回复 |
| **经 Hub 的完整审批闭环** | 未完成 | 尝试多次，均卡在人工回复环节（见"已知缺陷"） |

## M3 顾问 / M4 主动建议

| 项 | 状态 | 证据 |
|---|---|---|
| DeepSeek 调用 | 已验证 | 真实 API：问"几台机器"→「共 2 台机器，全部在线」 |
| 无审批权 | 仅单测 | 提示词契约 + `test_hub_chat.test_system_prompt_forbids_approval` |
| 限流 | 仅单测 | `test_hub_chat.test_rate_limit` |
| 已部署 | 已验证 | 日志 `[hub] chat advisor enabled (model=deepseek-chat)` |
| 主动建议（M4） | 仅单测 | `test_hub_proactive` 8 个用例；真机未触发过 |

## 已知缺陷（按发现顺序，均已修）

1. `- id: mcp-arb` 被 DSH 当作"覆写不存在的 id"静默跳过 → 改用 `- insert:`。
2. DSH 解析不到包 → profile 补 `@deepseek-ai/dsh-mcp-client` 依赖。
3. `arb hub` 子命令缺失 → systemd 重启循环。
4. Hub 提前关闭问题并回「✅已收到」，即使本地拒绝了答案（假成功）→ 改为节点拥有状态。
5. 引用回复在 Hub 路径上被忽略 → 存 `questions.msg_id` + 解析 `ref_msg.svr_id`。
6. relay 把发送者写成字面量 `hub` → 本地允许列表拒绝所有经 Hub 的答复 → 改为转发真实 wxid。
7. 同一问题推送两遍 → relay 模式只由 Hub 推送。
8. 快照取的是守护进程 cwd（用户主目录）→ 改为优先会话里的 git 仓库。
9. Hub 启动日志写死 `nodes=0` → 改为真实数量。
10. `ls ... | head` 这类只读管道仍会打扰 → 规则扩展为"每段都是只读白名单"。

## 复现全部测试

```powershell
$env:PYTHONPATH="src"; python -m unittest discover -s tests -p "test_*.py"
```
