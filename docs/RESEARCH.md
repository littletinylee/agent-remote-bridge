# 开源项目调研（阶段一）

调研时间：2026-10-22。所有数据来自实际 `git clone --depth 1` 与 GitHub API，不是凭印象总结。

| 项目 | Stars | 语言 | License | 最近推送 | 定位 |
|---|---|---|---|---|---|
| [slopus/happy](https://github.com/slopus/happy) | 24,255 | TypeScript | MIT | 2026-10-21 | 手机 App 远程操控 Claude Code / Codex |
| [slopus/happy-server](https://github.com/slopus/happy-server) | 368 | TypeScript | 无 LICENSE | 2026-02-14（**已归档**） | Happy 的云端 relay |
| [y49/tlive](https://github.com/y49/tlive) | 215 | TypeScript | MIT | 2026-10-14 | claude/codex 的远程审批 + live 监控（Telegram/飞书/Web） |
| [robertelee78/claude-telegram-mirror](https://github.com/robertelee78/claude-telegram-mirror) | 15 | Rust | 无 LICENSE | 2026-10-06 | 把 Claude 会话镜像到 Telegram |
| [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin) | 965 | TypeScript | MIT（MIT badge，API 元数据为 NOASSERTION） | 2026-10-13 | **腾讯官方**的 OpenClaw 微信渠道插件 |
| [Conn-Ho/wechat-claude-code](https://github.com/Conn-Ho/wechat-claude-code) | 32 | JavaScript | Apache-2.0 | 2026-03-27 | 微信远程控制 Claude Code（扫码登录） |

## 逐项结论

### 1. Happy / Happy Server —— 架构参考，不可直接复用微信

- Happy 是 React Native 手机端 + CLI wrapper + relay 的三段式：agent 侧通过 `happy` 包装器启动会话，手机端通过自有协议同步。
- **happy-server 已于 2026-02 归档且无 LICENSE 文件**，作为基础设施直接依赖有法律与维护双重风险。
- 它解决的是"用它的 App 远程看会话"，而不是"用个人微信收发"。要接微信等于重写整个客户端协议。

**结论：只借鉴其"本地会话 ↔ 远端客户端"的分层，不复用代码。**

### 2. tlive —— 与本项目目标最接近的架构（但只支持 Telegram/飞书）

- v2 起明确转向 **"骑在两家 Agent 官方 hook 机制上"**：不接管会话、不驱动模型，只做通知与审批卡片。
- 双层集成：hooks-only（不包装进程）与 wrapped（PTY + Web 终端）。这正是"不改变用户现有工作流"的正确做法。
- 支持 `mode: notify` 与 `mode: full`（远程 Allow/Deny 工具调用），并能"IM 回复 → 继续"。
- 缺失：**没有微信通道**；实现是 TypeScript + Node ≥22 + 大量运行时依赖；仓库 11MB，fork 后要把一个 IM 渠道塞进它的 5.x 代码库，长期合并成本高。

**结论：复用它的"设计"（hook 驱动、审批卡片、notify/full 两档），不复用代码。** 本项目在 `docs/ARCHITECTURE.md` 中明确致谢。

### 3. claude-telegram-mirror —— 仅作通知镜像参考

Rust 实现、仅 Telegram、15 stars、无 LICENSE 文件。它监听 Claude 的 JSONL transcript 做镜像。监听 transcript 是"事后读取"，无法承载"阻塞等人回答"的审批语义，因此不适合作主干。

### 4. Tencent/openclaw-weixin —— **本次调研的关键发现**

这是腾讯官方维护的 OpenClaw 微信渠道插件，暴露了个人微信的 **iLink Bot HTTP 协议**：

| 端点 | 作用 |
|---|---|
| `GET ilink/bot/get_bot_qrcode?bot_type=3` | 获取登录二维码（返回 `qrcode` + `qrcode_img_content`） |
| `GET ilink/bot/get_qrcode_status?qrcode=…` | 长轮询扫码状态：`wait/scaned/confirmed/expired/binded_redirect` |
| `POST ilink/bot/getupdates` | **长轮询收消息**（`get_updates_buf` 断点续传） |
| `POST ilink/bot/sendmessage` | **主动发消息**（`to_user_id` + 可选 `context_token`） |
| `POST ilink/bot/sendtyping` / `msg/notifystart` / `msg/notifystop` | 输入状态、上下线告知 |

关键性质（我实测 + 读源码确认）：

1. **纯客户端发起**：收消息是客户端出站长轮询 → 本机**不需要公网 IP、不需要端口映射、不需要云端中转**。
2. **无 48 小时客服窗口**：`sendmessage` 是 bot 主动推送，长时间无交互后依然可以主动送达（这与公众号客服消息完全不同）。
3. **双向**：用户在微信里直接回复即可，`getupdates` 立刻拿到，且带 `context_token`、`message_id`、`ref_msg.svr_id`（引用回复的那条消息 ID）。
4. **凭据有效期**：`errcode -14` 表示会话失效，需要重新扫码；插件对此的处理是暂停会话并提示重新登录。

不可直接当库用的原因：它是 **OpenClaw 的 channel 插件**，入口依赖 `openclaw/plugin-sdk`、`openclaw ≥ 2026.5.12`、Node ≥ 22.13。要复用它就得把整个 OpenClaw Gateway 装进用户的编程环境——这违背"轻量、不改变现有工作流"。

**结论：复用"协议 + 语义"，自研一个 ~400 行、零第三方依赖的 Python 客户端。** 这是本项目唯一的"重复开发"，且是实现目标的最小必要量。

### 5. wechat-claude-code —— 协议的第二份独立验证

Apache-2.0，JS，2026-03 后基本停更。但它的 `src/ilink-api.ts` / `src/auth.ts` 与 openclaw 完全一致的 7 个端点，**独立印证了 iLink 协议**；同时它示范了 hook（`pre-tool-use.sh` / `stop.sh`）+ MCP server + 审批白名单的组合。

缺点：只面向 Claude Code、无多 Agent 路由、无并发问题关联、无断线恢复。

**结论：作为协议交叉验证与交互设计的参考，不复用代码。**

### 6. 消息中转服务

调研对象里唯一"现成 relay"是 happy-server，已归档。而 iLink 协议本身是出站长轮询，**不需要中转服务**。

> 因此本项目**不部署阿里云 ECS**。这是刻意的减法：少一个服务、少一份凭据、少一处故障点。ECS 方案保留在 `docs/DEPLOY.md` 里作为"多机/离线兜底"的可选项，但默认不启用。

## 复用决策矩阵

| 需求 | Happy | tlive | tg-mirror | openclaw-weixin | wechat-cc | 本项目做法 |
|---|---|---|---|---|---|---|
| Agent 事件监听 | wrapper | hooks | transcript | — | hooks | **hooks + MCP**（两者都支持） |
| 人工审批 | App | ✅ | ❌ | — | ✅ | ✅ hook 拦截 + MCP 阻塞调用 |
| 原会话继续 | ✅ | ✅ | ❌ | — | ✅ | ✅ 答案作为 tool result / hook 决策回传 |
| 多 Agent / 子智能体 | 部分 | 部分 | ❌ | ✅ 会话隔离 | ❌ | ✅ session 身份 + 代码路由 |
| Windows 兼容 | 部分 | ✅ | 需编译 | ✅ | ✅ | ✅ 纯 Python stdlib |
| 个人微信双向 | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ 复用 iLink 协议 |
| 主动推送（无窗口限制） | — | — | — | ✅ | ✅ | ✅ 实测 |
| 现成中转服务 | 归档 | 自带 web | ❌ | 需 OpenClaw | 无需 | **无需**（出站长轮询） |

## 最终技术选型

```
本地 Agent（Claude Code / Codex / DSH / OpenCode / 任意 MCP 客户端）
        │  hooks（事件、审批） + MCP stdio（ask_human 阻塞提问）
        ▼
agent-remote-bridge 守护进程（Python 3.9+，零第三方依赖，SQLite）
        │  出站 HTTPS 长轮询 + sendmessage
        ▼
ilinkai.weixin.qq.com（腾讯官方 iLink Bot 通道）
        ▼
手机个人微信
```

选择理由：

1. **最少自研**：不写 Agent 调度引擎，不写 IM 协议栈，只写"事件 → 路由 → 决策状态机"这一层。
2. **最稳通道**：iLink 是腾讯自家插件的通道，不是逆向 hook 微信客户端，账号风险显著低于 itchat/wechaty 类方案。
3. **最少基础设施**：0 台服务器、0 个公网端口、0 个 npm 依赖、1 个 Python 进程。
4. **最强兼容**：MCP 是事实标准（Claude Code、Codex、DSH、OpenCode 都支持），新增 Agent 只需加一个 hook 配方。

## 已知平台限制（详见 docs/LIMITATIONS.md）

- iLink 是 **bot 身份**：用户扫码后微信里出现一个机器人联系人，它的推送不是"文件传输助手"那种对话。
- 凭据会失效（`-14`），需要重新扫码；本项目会明确告警而不是静默失败。
- 微信文本渲染：不解析 Markdown，过长消息需分段（已按 1500 字符切分）。
- 语音/图片上行暂未处理（图片下行需要 CDN 上传 + AES，属于可选增强）。
