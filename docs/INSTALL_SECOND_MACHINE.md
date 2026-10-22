# 第二台机器接入（Windows）

目标：这台机器上的 Claude Code / Codex / DSH 都能通过**同一台 Hub** 收发微信，
不需要公网 IP、不需要开放任何入站端口（全部是出站连接）。

## 0. 前置

需要 **Python 3.10+**（本包零依赖，不需要虚拟环境）：

```powershell
python --version
```

没有的话装一次即可：`winget install Python.Python.3.12`。

## 1. 安装

```powershell
pip install "git+https://github.com/littletinylee/agent-remote-bridge.git"
arb --version
```

装完 `arb` 就在 PATH 上，**不需要设置 PYTHONPATH**。

> 如果 `pip install git+…` 在这台机器上失败（旧版 pip 的构建问题），用等价的两步：
>
> ```powershell
> git clone https://github.com/littletinylee/agent-remote-bridge.git $env:USERPROFILE\arb-src
> pip install $env:USERPROFILE\arb-src
> ```

## 2. 加入 Hub

```powershell
arb init
arb node-join https://120.78.79.184:18443 --name <这台机器的名字> --fingerprint 5121E30EAB068C9E3C2C6151560266FF7D74C19AF03C931BFBC5EFA73F1D581A
```

- `--name` 建议用电脑名（不填则自动取 `COMPUTERNAME`），它会显示在网页控制台和微信推送的 `[机器名]` 前缀里；
- `--fingerprint` 是 Hub 自签证书的 SHA-256；**不匹配会拒绝连接**（防止中间人）。当前 Hub 的指纹就是上面那串，换过证书要同步更新；
- 注册是开放的（无邀请码），但只有你微信号的答复能批准操作。

成功输出类似：

```
Hub 可达: {"ok":true,"version":"0.1.0","nodes":1,"wechat_logged_in":true}
证书指纹校验通过: 5121E30EAB068C9E…
已注册为 <名字> (xxxxxxxx) ，通道切换为 relay
```

## 3. 给各 Agent 装插件

```powershell
arb node setup
```

它会做三件事（幂等、可回滚、自动备份原文件）：

| Agent | 装什么 |
|---|---|
| Claude Code | `~/.claude/settings.json` 的 hooks（SessionStart / Stop / PreToolUse 审批…） |
| Codex | 本地插件市场 + `.codex-plugin`（MCP 服务器与 hooks） |
| DSH | profile 的 `cordis.patch.yml` 里插入 MCP 插件条目（用 `pythonw`，不开窗口） |

只装其中某几个：

```powershell
arb node setup --only codex,dsh
arb node setup --dry-run        # 先看看会改什么
arb node status                 # 查看当前状态
```

> **DSH 需要重启一次**才会加载新的插件配置；Codex 插件用 `codex plugin list` 确认。

## 4. 启动并自检

```powershell
arb start
arb status
arb doctor
```

`arb status` 应显示 `通道: relay`、`已登录=True`。`arb doctor` 会检查配置、通道、钩子、
数据库等。

## 5. 开机自启（可选）

```powershell
arb service install
```

## 6. 从别处确认它真的接上了

- 网页控制台：`https://120.78.79.184:18443/`（账号密码 `q045204`）→「项目状态」里应多出这台机器；
- 健康检查：`https://120.78.79.184:18443/v1/health` → `nodes` 数量 +1；
- 让它随便跑一个 Agent 任务，微信应收到 `[这台机器的名字] …` 的推送。

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `证书指纹不匹配` | Hub 换过证书；用 `openssl x509 -in /etc/arb/hub.crt -noout -fingerprint -sha256` 取新值 |
| `连不上 Hub` | 安全组/防火墙没放行 18443，或 IP 变了 |
| `通道: relay / 启动中` | 首次长轮询还没返回，等 5–30 秒；持续不动看 `arb doctor` |
| 微信里收到 `[机器名]` 但没内容 | 该机器的 daemon 没起：`arb status` → `arb start` |
| DSH 启动后弹出终端窗口 | 该机器的 profile 里 command 被写成 `python.exe`；重跑 `arb node setup --only dsh` 会改成 `pythonw` |
| 想退出这台机器 | `arb node uninstall --only codex,dsh,claude` 然后 `arb service uninstall`（Hub 上的注册记录可留可删） |
