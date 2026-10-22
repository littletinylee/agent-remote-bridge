# 排障手册

按**症状**找，每条都来自本项目开发过程中真实踩过的坑，附带可直接复制的诊断命令。

---

## 1. `arb start` 报 "daemon did not come up within 15s"

日志（`%APPDATA%\agent-remote-bridge\arb.log`）里如果是：

```
[arb] cannot start: [WinError 10048] 通常每个套接字地址只允许使用一次
```

说明 **8848 端口已被另一个守护进程占用** —— 通常是另一个 `ARB_HOME`（测试目录、旧配置）起的实例。

```powershell
Get-NetTCPConnection -LocalPort 8848 -State Listen | Select-Object OwningProcess
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'arb serve' } | Select-Object ProcessId,CommandLine
# 确认是自己起的残留进程后：
Stop-Process -Id <PID> -Force
```

设计上守护进程**拒绝共享端口**（`SO_REUSEADDR = False`），所以不会出现两个实例抢同一个端口、消息被劈成两半的情况。

## 2. 所有请求都 401 / `arb status` 说"未运行"但进程明明在

`api_token` 不一致。常见原因：用了不同的 `ARB_HOME`；config 被手改；从别处复制了配置。

```powershell
arb status          # 打印的是当前 ARB_HOME 的视角
echo $env:ARB_HOME  # 空 = 用默认目录
```

守护进程启动时如果发现 `api_token` 为空会自己生成一个并写盘（并且**空 token 会拒绝一切请求**），
所以不要手工把 `api_token` 置空。

## 3. 通道显示"异常 / down"，但"已登录=True"

十有八九是微信凭据失效（`errcode -14`）：`arb status` 会直接显示
`微信登录已失效（errcode -14），请重新运行 arb login`。

```powershell
arb login     # 重新扫码
arb status    # 不需要重启守护进程，最多等一次 30s 重连
```

已实测：恢复凭据后同一进程从 `down` 回到 `healthy`，`uptime` 连续。

## 4. Claude Code 的钩子"完全没反应"

第一嫌疑是 `PYTHONPATH`：Claude 启动钩子时**不会**继承本仓库的路径。
所以钩子命令必须指向包装脚本，而不是裸的 `python -m arb ...`。

```powershell
arb hooks status                      # 看装了哪些事件
# 用干净环境直接跑一次钩子（最有效的验证）：
$env:PYTHONPATH=$null; $env:ARB_HOME=$null
'{"hook_event_name":"Notification","message":"hook test","cwd":"D:/tmp"}' | & "$env:APPDATA\agent-remote-bridge\arb-hook.cmd"
echo $LASTEXITCODE                    # 0 = 钩子本身没问题
```

如果 `arb hooks status` 显示未安装：`arb hooks install`。
想撤销：`arb hooks uninstall`（只删本项目加的条目，别人的钩子不动）。

## 5. Codex 的 MCP 工具调用被拒绝

```
The tool call was rejected because it requires approval, but the approval policy is set to "never".
```

Codex 把 MCP 调用当作需要审批的操作。交互式会话里批准一次该服务器即可；`codex exec` 需要
`--approve-for-me` 或（仅信任环境）`--dangerously-bypass-approvals-and-sandbox`。

## 6. Codex 连 shell 命令都执行不了：`the execution host is missing`

这是 **Codex 安装本身缺二进制**：`<CODEX_HOME>\.sandbox-bin\` 里没有匹配版本的
`codex-command-runner.exe` / `codex-code-mode-host.exe`。表现为任何工具调用都失败，与桥接无关。

```powershell
Get-ChildItem "$env:USERPROFILE\.codex\.sandbox-bin" | Select-Object Name
# 找一找有没有完整的一套（例如 Windows 应用商店版 / AppData\Local\OpenAI\Codex\bin\<hash>\）
Get-ChildItem "$env:LOCALAPPDATA\OpenAI\Codex\bin" -Recurse -Filter codex.exe | Select-Object FullName
```

本项目实测时就是改用同机另一套完整构建（`0.162.0-alpha.17.2`）才跑通的。升级 Codex 通常也能解决。

## 7. 微信一直收不到消息，但 outbox 显示 `pending > 0`

```powershell
arb status                       # 看 channel 状态与 outbox.failing
arb doctor                       # 端到端体检（含 iLink 端点可达性）
Get-Content "$env:APPDATA\agent-remote-bridge\arb.log" -Tail 30
```

- `outbox.pending` 持续增长 + 通道 down → 通道不可用，消息在排队（不会丢，恢复后补发）。
- `outbox.failing > 0` → 发送持续失败，`arb.log` 里有 `delivery_failed` 与原因。
- 通道健康、outbox=0、但手机没收到 → 检查 `owner_ids` 是否是你的微信 ID（`arb status` 会打印）。

> 重要：**投递失败绝不会被当成"已批准"**。危险操作只会过期（= 拒绝）。

## 8. 我回复了，但 Agent 没继续

先看这句话是不是"已经处理过了"或"已过期"——微信里会有明确回复。然后：

```powershell
arb pending                       # 还有没有在等的问题
arb sessions                      # 对应会话还在不在
```

常见原因：

- 你回复时**已经有多个问题在等**，而回复没带 `#CODE` → 桥接会拒绝并列出编号（这是防串线设计，不是 bug）。
- 提问方自己等超时放弃了（`arb ask --timeout` 到期）。问题仍在微信里可答，但那个进程不会再看。
- 回复来自**未授权的微信 ID**（只有 `owner_ids` 里的账号有效），审计里会有 `unauthorized_inbound`。

## 9. 多个项目同时提问，怎么确保不串？

不需要额外操作：只有**一个**待处理问题时裸回复即可；**多个**同时存在时必须带 `#CODE`（或在微信里
引用回复那条消息）。桥接在歧义时选择"拒绝并让你选"，而不是猜。

```powershell
arb pending        # 看所有待处理问题及其 #CODE
```

## 10. `config.json` 手改后守护进程起不来

现在能容忍 UTF-8 BOM（记事本 / PowerShell 5 的 `-Encoding utf8` 都会加），但**JSON 语法错**依然会失败：

```powershell
python -c "import json,os;json.load(open(os.path.join(os.environ['APPDATA'],'agent-remote-bridge','config.json'),encoding='utf-8-sig'));print('ok')"
```

配置项的完整列表见 [CONFIG.md](CONFIG.md)。

## 11. 终端里中文/emoji 变成乱码

- `arb` 自己会把 stdout/stderr 设成 UTF-8，输出到终端一般正常。
- **用 PowerShell 管道喂 JSON 给它**（例如手工测试钩子）时，PowerShell 会按控制台代码页转码，
  产生孤立代理字符 → 写入前会被清洗成 `?`，但不代表原始数据正确。测试时请用文件重定向：
  `cmd /c "type payload.json | ...\arb-hook.cmd"`。

## 12. 想把所有东西拆掉

```powershell
arb stop
arb hooks uninstall            # 移除 Claude Code 钩子
claude mcp remove arb          # 移除 MCP 注册
arb service uninstall          # 移除登录自启
arb logout                     # 清除微信凭据
Remove-Item "$env:APPDATA\agent-remote-bridge" -Recurse   # 配置/数据库/日志
```

## 13. 需要更细的证据

```powershell
arb doctor                                       # 九项体检
python scripts\verify.py --markdown              # 快照（文本/markdown/JSON）
sqlite3 "$env:APPDATA\agent-remote-bridge\arb.sqlite3" "select datetime(ts,'unixepoch','localtime'),actor,action,substr(detail,1,80) from audit order by id desc limit 30"
```

审计表是最有用的排障材料：谁在什么时候创建/回答/拒绝了哪个问题、投递失败原因、通道健康变化，全在里面。
