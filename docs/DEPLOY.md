# 部署与运维

## 1. Windows 本机安装

```powershell
git clone https://github.com/littletinylee/agent-remote-bridge.git
cd agent-remote-bridge
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

`install.ps1` 做四件事：

1. 检查 Python ≥ 3.10；
2. 尝试装可选依赖 `qrcode[pil]`（终端画二维码；装不上不影响运行）；
3. 在 `%USERPROFILE%\.local\bin` 放一个 `arb.cmd` 垫片（并把该目录加进用户 PATH）；
4. 运行 `arb init`，打印绑定码。

不想装垫片也行：直接用 `scripts\arb.cmd <命令>`。

## 2. 首次绑定

```powershell
arb login        # 手机微信扫码，终端里会出现二维码；也可 --qr-png 存图
arb start        # 后台起守护进程
arb status       # 看绑定码 / 通道健康
```

然后在微信里给机器人发：`/pair <绑定码>`。
收到"绑定成功"后跑 `arb test-push` 做一次主动推送自检。

> Bot 协议只在**你先发过一条消息**之后才拿得到你的 `user_id`，所以这一步不能省。

## 3. 开机自启（已实现，一条命令）

```powershell
arb service install      # 装到当前用户的启动文件夹（免管理员权限）
arb service status       # 看是否安装
arb service uninstall    # 移除
```

它会在 `%APPDATA%\agent-remote-bridge\arb-serve.cmd` 写一个前台启动脚本，并在
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\` 放一个隐藏窗口的 `.vbs` 启动项。
下次登录 Windows 时会悄悄把守护进程拉起来。**本机已实测**：停掉守护进程 → 运行该启动项 → `arb status` 恢复健康。

为什么不用 `schtasks /SC ONLOGON`：那条命令需要管理员权限（实测返回 `Access is denied`），
而启动文件夹方案不需要。若你要"崩溃自动重启"，再用管理员权限建任务计划替代 `.vbs`：

```powershell
# 需要管理员终端
$action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument '/c "%APPDATA%\agent-remote-bridge\arb-serve.cmd"'
$trigger = New-ScheduledTaskTrigger -AtLogOn
$set     = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "AgentRemoteBridge" -Action $action -Trigger $trigger -Settings $set
```

## 3.5 接入 Claude Code（一条命令）

```powershell
arb hooks install    # 合并 7 个 hook 事件，先备份原文件；幂等、可撤销
arb hooks status
```

再注册 MCP（让 Claude 能主动问你）：

```powershell
claude mcp add --scope user arb -- "$env:APPDATA\agent-remote-bridge\arb-mcp.cmd" --agent claude-code
claude mcp list      # 应显示 arb ... Connected
```

撤销：`arb hooks uninstall` 与 `claude mcp remove arb`。

> 为什么不直接写 `python -m arb hook claude-code`：Claude 启动钩子时不带本仓库的 `PYTHONPATH`，
> 那条命令会 `ModuleNotFoundError`。`arb hooks install` 生成带环境的 `arb-hook.cmd` / `arb-mcp.cmd`。

## 4. 日常运维

| 操作 | 命令 |
|---|---|
| 状态 | `arb status`（健康、会话数、待处理、待发队列） |
| 自检 | `arb doctor`（Python/配置/SQLite/凭据/iLink 可达/守护进程/通道） |
| 日志 | `arb.log`（守护进程 stdout/stderr，含重连与投递失败） |
| 待处理 | `arb pending` / 微信 `/pending` |
| 本机回答 | `arb answer <CODE> <内容>`（通道故障时的兜底） |
| 停 | `arb stop` |
| 会话列表 | `arb sessions` / 微信 `/projects` |

**数据库**：`%APPDATA%\agent-remote-bridge\arb.sqlite3`（WAL）。备份直接复制即可（先 `arb stop`）。
只追加的 `audit` 表是最有用的排障材料。

**升级**：`git pull` → `arb stop` → `arb start`。数据库 schema 目前是 `CREATE TABLE IF NOT EXISTS`，
新增列需要手工迁移（会在 CHANGELOG 里注明）。

**卸载**：`arb stop` → 删除仓库目录 → 删除 `%APPDATA%\agent-remote-bridge` → 从 `~/.claude/settings.json`
移除 hooks、`claude mcp remove arb`、从 `~/.codex/config.toml` 移除 `notify` 与 `[mcp_servers.arb]`。

## 5. 为什么没有云端中转

iLink 通道收发都是本机**出站**（长轮询 + POST），不需要公网入口，因此：

- 不需要阿里云 ECS；
- 不需要域名/证书/备案；
- 不需要在云上存任何凭据。

**只有这三种情况才值得上中转**，那时再考虑 ECS：

1. 多台开发机共用一个微信 bot（需要服务端做设备路由与会话归属）；
2. 电脑长期关机但仍要接收/暂存决策（需要服务端持久化 + 补发）；
3. 改用企业微信/公众号（平台要求公网回调 URL）。

真要做时，最小形态是：ECS 上一个 Python 进程，暴露 `POST /wechat/callback`（平台回调）+ 本机
`GET /poll` 长轮询，复用现在的 `Channel` 接口 —— 不需要引入数据库集群或消息队列。

## 6. 备用消息通道

当 iLink 不可用（凭据失效 / 平台限制）时的降级顺序：

| 方案 | 双向 | 需要公网 | 工作量 |
|---|---|---|---|
| `arb login` 重新扫码 | ✅ | ❌ | 1 分钟 |
| console 通道（`arb serve --channel console`） | ✅ | ❌ | 立即，但只能在终端里 |
| 企业微信自建应用 + 微信插件 | ✅ | ✅ 回调 | 约 200 行新 channel |
| Server酱 / PushPlus | ❌ 单向 | ❌ | 约 50 行，只能通知；回答用 `arb answer` |
| Telegram / 飞书 | ✅ | ❌（长轮询） | 约 150 行，参考 tlive |

`Channel` 接口只有四个方法（`start/send/healthy/close`），加通道不需要动路由、存储和 Agent 适配。

## 7. 健康检查（给监控用）

```powershell
curl -H "X-ARB-Token: <token>" http://127.0.0.1:8848/api/health
```

关注字段：`channel.healthy`、`channel.auth_error`、`outbox.pending`、`outbox.failing`、
`stats.pending_questions`。

- `channel.healthy=false` 且 `auth_error` 非空 → 需要重新 `arb login`；
- `outbox.failing > 0` → 发送持续失败，看 `arb.log`；
- `outbox.pending` 持续增长 → 通道不可用，决策通知正在排队（不会丢）。
