# 安装

本包**零运行时依赖**（纯标准库），所以没有任何 `pip install -r` 的纠结：
Python ≥3.10 即可，服务器上也不需要虚拟环境。

## 任意一台开发机

```bash
pip install "git+https://github.com/littletinylee/agent-remote-bridge.git"
arb --version
```

装完 `arb` 就在 PATH 上，**不需要 `PYTHONPATH`**（这是之前最大的摩擦点：
Claude/Codex 启动钩子时不带仓库路径，所以以前要靠生成的 `.cmd` 包一层）。

接着二选一：

```bash
# A) 这台机器直接连微信（单机模式）
arb init
arb login          # 扫码
arb start

# B) 加入已有的 Hub（多机模式，推荐）
arb node-join https://<hub-ip>:18443 --name workbench --fingerprint <证书指纹>
arb node setup     # 装 Claude Code / Codex / DSH 插件
arb start
```

验证：`arb status` 应显示 `通道: relay`（B）或 `通道: ilink`（A）。

## 服务器（Hub）

```bash
pip install "git+https://github.com/littletinylee/agent-remote-bridge.git"
mkdir -p /etc/arb /var/lib/arb && chmod 700 /etc/arb /var/lib/arb
# 证书 + 配置见 docs/HUB.md，然后：
arb hub serve /etc/arb/hub.json
```

systemd 单元见 `docs/HUB.md`（`scripts/hub_setup_remote.sh` 会自动完成
clone / 证书 / systemd 三件事；用 pip 安装时把 `ExecStart` 改成 `arb hub serve /etc/arb/hub.json`，
并去掉 `Environment=PYTHONPATH=`）。

## 校验安装

```bash
arb --version            # agent-remote-bridge <version>
arb doctor               # 环境、配置、通道、钩子一把检查
arb status               # 守护进程 / 通道 / 会话 / 队列
```

## 卸载

```bash
arb hooks uninstall      # 先撤掉各家 Agent 的钩子（只删本包加的条目）
arb node uninstall --only codex,dsh,claude
arb service uninstall    # 去掉开机自启
pip uninstall agent-remote-bridge
```

配置与数据库在 `%APPDATA%\agent-remote-bridge\`（Windows）或 `~/.config/agent-remote-bridge/`，
卸载包不会删除它们 —— 需要彻底清理时手动删该目录。

## 为什么不用 Docker

客户端必须能调用**本机**的 Agent（Claude/Codex/DSH 都在宿主机上，钩子是宿主机进程），
容器化会把这件事变复杂而不增加安全。Hub 端确实可以容器化，但它只是 4 个 .py 文件 +
一个 systemd 单元，容器带来的收益很小。
