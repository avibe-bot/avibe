# Avibe CLI 参考手册

## 快速开始

```bash
vibe              # vibe start 的别名
vibe start        # 按需启动 Avibe（打开 Web UI）
vibe status       # 查看服务状态
vibe remote       # 引导式配置 Avibe Cloud 远程访问
vibe screenshot   # 截取本机桌面截图
vibe stop         # 停止所有服务
```

## 命令详解

## 远程访问 Web UI

默认情况下，Web UI 只监听在运行 Avibe 的那台机器的 `127.0.0.1:5123`。

如果你希望从另一台设备打开 Web UI，或者把 Avibe 安装在远端服务器上，请使用引导式远程访问配置：

```bash
vibe remote
```

这个命令会引导你登录 `https://avibe.bot`、创建 remote-access bot、领取个人专属域名、粘贴一次性 pairing key，并自动启动安全 tunnel。


### `vibe`

`vibe start` 的别名。

```bash
vibe
```

**行为：**
- 按需启动 Avibe
- 复用已运行的进程
- 在浏览器中打开 Web UI

### `vibe start`

按需启动 Avibe。会在浏览器中打开 Web UI。

```bash
vibe start
```

**行为：**
- 如果主服务与 Web UI 已在运行，则复用现有进程
- 打开设置向导 `http://127.0.0.1:5123`
- **保留已运行的进程** — 需要明确重启时请使用 `vibe restart`

### `vibe stop`

完全停止所有 Avibe 服务。

```bash
vibe stop
```

**行为：**
- 停止主服务
- 停止 Web UI 服务器
- **终止 OpenCode 服务器** — 当你需要重启 OpenCode 时使用此命令

### `vibe status`

显示当前服务状态。

```bash
vibe status
```

**输出示例：**
```json
{
  "state": "running",
  "running": true,
  "pid": 12345
}
```

### `vibe doctor`

运行配置诊断检查。

```bash
vibe doctor
```

**检查内容：**
- 配置文件有效性
- Slack token 配置
- Agent CLI 可用性（Claude Code、OpenCode、Codex）
- 运行时环境

### `vibe remote`

启动 Avibe Cloud 远程访问的引导式配置流程。

```bash
vibe remote
```

**流程：**
- CLI 会先解释远程访问的作用，不会一上来就要求输入配对码。
- 打开 `https://avibe.bot`，注册或登录，创建新的 remote-access bot，领取自己的个人域名，然后复制一次性 pairing key。
- 回到 CLI 按 Enter，粘贴 pairing key，Avibe 会自动保存配置并启动托管 tunnel。
- 启动成功后，CLI 会展示远程访问链接，并给出查看状态、重新启动、停止远程访问的后续命令。打开链接时，请使用同一个 avibe.bot 账号登录。

如果你已经拿到 pairing key，也可以用直接配对命令：

```bash
vibe remote pair vrp_abc123
```

常用后续命令：

```bash
vibe remote status
vibe remote start
vibe remote stop
```

这些子命令都支持 `--json` 输出，便于脚本调用。

### `vibe screenshot`

截取本机桌面并保存为 PNG 文件。

```bash
vibe screenshot
vibe screenshot --output /tmp/screen.png
vibe screenshot --json
```

**行为：**
- 默认保存到 `~/.vibe_remote/screenshots/`
- 默认输出保存路径；加 `--json` 时输出机器可读的 JSON
- 只作为 CLI 层能力存在；不新增 IM 命令、bot 按钮，也不注入 Agent prompt

### `vibe session`

列出、查看并重命名 Agent 会话。`list` 与 `get` 是只读视图；`update` 只改标题。已归档会话视为软删除，任何情况下都不会被列出。

```bash
vibe session list                       # 未归档会话，每页 10 条，按最近活跃倒序
vibe session list --type slack          # 按平台过滤（avibe = Web/Workbench）
vibe session list --page 2              # 翻到下一页（固定每页 10 条；没有 --limit）
vibe session get sesk8m4q2p7x           # 单个会话的完整明细
vibe session update sesk8m4q2p7x --title 'Release review'   # 传 "" 可清空标题
```

`--type` 取平台 id：`avibe`（Web/Workbench）、`slack`、`discord`、`telegram`、`lark`、`wechat`。需要更高级的筛选——按 Agent、时间段、消息内容或跨表联查——`list` 与 `get` 的返回都会引导你使用 `vibe data query`。

### `vibe task`

创建、查看、更新、立即执行、暂停、恢复或删除定时任务。

```bash
vibe task add --session-id sesk8m4q2p7x --cron '0 * * * *' --message 'Share the hourly summary.'
vibe task add --cron '0 * * * *' --message 'Share the hourly summary.'   # 在 Avibe Agent shell 内
vibe task list --brief
vibe task update <task-id> --cron '*/30 * * * *'
vibe task run <task-id>
vibe task remove <task-id>
```

更完整的参数说明请直接看 `vibe task add --help` 和 `vibe task update --help`。其中重点包括：

- 用 `--session-id` 指定要延续的 Agent Session
- 用 `--post-to channel` 在保留 thread 上下文的同时把消息发到父频道
- 用 `--deliver-key` 指定显式投递目标
- 用 `--cron` / `--at` 控制定时方式
- 以及 `--name`、`--timezone`、`--message-file` 等参数

当 `vibe task add` 运行在 Avibe 已注入 caller context 的 Agent shell 内时，
可以省略 `--session-id`。Avibe 会把任务目标默认到 `AVIBE_SESSION_ID`
对应的调用方 Session，并在命令输出里报告这次默认。显式 `--session-id`、
session creation 参数和 delivery 参数仍然优先。

`--session-key` 仍兼容旧脚本，但新任务应使用当前 Avibe prompt
里展示的 Agent Session ID。

### `vibe agent run`

直接运行一个 Agent。加 `--async` 时会队列化一次后台 run，但不会创建持久化任务定义。

```bash
vibe agent run --agent release-reviewer --message 'Review the latest deployment result.'
vibe agent run --async --no-callback --session-id sesk8m4q2p7x --message 'The export finished. Share the summary.'
vibe agent run --async --no-callback --fork-session sesk8m4q2p7x --message 'Explore this alternate fix from the current context.'
vibe agent run --async --session-id sesworker123 --callback-session-id sescaller456 --message 'Run the delegated investigation.'
vibe agent run --async --no-callback --create-session --deliver-key slack::channel::C999 --agent release-reviewer --message 'Post the deployment summary.'
```

当一个新 Agent Session 需要从现有 Session 的 native backend 上下文分叉，而不是空白开始时，
使用 `--fork-session <session-id>`。新 Session 会保持源 Session 的 backend。
只有 backend 不变时，才可以通过 `--agent`、`--model`、`--reasoning-effort`
覆盖 fork 后 Session 的 Agent、模型或推理强度；跨 backend fork 会被拒绝。
不要把 `--fork-session` 和 `--session-id`、`--create-session`、`--deliver-key`
或 `--post-to` 混用。

异步 run 需要明确 callback 策略，除非命令运行在 Avibe 已注入 caller context
的 Agent 环境内。当最终结果文本需要回到调用方 Session 时，使用
`--callback-session-id`；当你有意不自动回调、后续会通过 `vibe runs show`
或 runs 列表/轮询查看结果时，使用 `--no-callback`。Agent 内部发起的 Harness
调用会默认把 callback 指向当前调用方 Session。这个 callback 与普通投递相互独立：
即使目标 run 已经把结果发到了自己的 IM scope，调用方 Session 仍然会收到结果并触发一次
跟进 Agent 消息。system、tool call、assistant 中间过程消息不会包含在 callback 里。

`vibe hook send` 仅作为 deprecated 兼容入口保留。新的自动化入口应使用
`vibe agent run`。

### `vibe watch`

创建、查看、更新、暂停、恢复或删除一个被管理的后台 watch。watch 会运行一个
waiter 命令（例如构建脚本或状态轮询）。当命令进入可报告状态时，Avibe
会把 `--message` 和 waiter stdout 组合起来，并通过选定 Session 创建一次跟进
Agent Run。

```bash
vibe watch add \
  --session-id sesk8m4q2p7x \
  --message 'Test run finished. Summarize the failures and propose next steps.' \
  -- ./scripts/run_tests.sh

vibe watch add \
  --message 'Test run finished. Summarize the failures and propose next steps.' \
  -- ./scripts/run_tests.sh     # 在 Avibe Agent shell 内

# 也可以通过 --shell 传入一整段 shell 命令
vibe watch add \
  --session-id sesk8m4q2p7x \
  --message 'Build done. Summarize.' \
  --shell 'make build && ./scripts/post_build.sh'

vibe watch list --brief
vibe watch show <watch-id>
vibe watch update <watch-id> --name 'Watch deployment' --timeout 1200
vibe watch pause <watch-id>
vibe watch resume <watch-id>
vibe watch remove <watch-id>
```

waiter 命令放在 `--` 后面；或者通过 `--shell` 传入一整段 shell 字符串。
完整参数请看 `vibe watch add --help`，包括 `--timeout`、`--lifetime-timeout`、
`--forever`、`--retry-exit-code`、`--retry-delay`、`--post-to channel`、
`--deliver-key` 和 `--name`。watch 与 `vibe task`、`vibe agent run` 共用
`--session-id`、`--post-to` 和 `--deliver-key` 语义。需要可管理、可暂停、可查看的
后台等待任务时，优先使用 `vibe watch`，不要随手起 `nohup`。

### `vibe version`

显示已安装的版本。

```bash
vibe version
```

### `vibe check-update`

检查是否有新版本可用。

```bash
vibe check-update
```

### `vibe upgrade`

升级到最新版本。

```bash
vibe upgrade
```

如果 Avibe 已在运行，该命令会安排一次受控重启，让服务和 Web UI 切换到升级后的代码。
如果 Avibe 原本是停止状态，则保持停止，下次启动时使用新版本。

## 服务生命周期

### 理解「重启」与「停止」的区别

Avibe 管理两类进程：

| 进程 | 说明 |
|------|------|
| **主服务** | 处理各聊天平台通信，并将消息路由到 Agent |
| **OpenCode 服务器** | OpenCode Agent 的后端服务（如已启用） |

命令的关键区别：

| 命令 | 主服务 | OpenCode 服务器 |
|------|--------|-----------------|
| `vibe` | 启动/复用 | 保留 |
| `vibe start` | 启动/复用 | 保留 |
| `vibe restart` | 重启 | **终止** |
| `vibe stop` | 停止 | **终止** |

### 为什么这很重要

当你运行 `vibe restart` 时：
- 主服务会被干净地重启
- UI 也会一起重启
- OpenCode 服务器会在重启过程中被终止

当你运行 `vibe stop` 时：
- **一切都会干净地停止**
- OpenCode 服务器被终止
- 更新 OpenCode 或其配置前使用此命令

## 常见场景

### 日常重启

如果是 Agent 在当前会话里触发重启，默认优先用延迟参数，用户体验更好：

```bash
vibe restart --delay-seconds 60
```

如果就是要立刻重启 Avibe：

```bash
vibe restart
```

### 更新 OpenCode 配置

修改 `~/.config/opencode/opencode.json` 后：

```bash
vibe restart --delay-seconds 60
```

### 更新 OpenCode 程序

安装新版本 OpenCode 后：

```bash
vibe restart --delay-seconds 60
```

### 更新 Avibe

```bash
vibe upgrade
# 然后重启：
vibe restart --delay-seconds 60
```

### 故障排查

如果遇到卡住的情况：

```bash
# 检查状态
vibe status

# 运行诊断
vibe doctor

# 如果是 Agent 触发，优先延迟重启
vibe restart --delay-seconds 60
```

## Web UI 控制

Web UI (`http://127.0.0.1:5123`) 提供相同的控制功能：

| 按钮 | 等效 CLI | OpenCode 行为 |
|------|---------|---------------|
| **Start** | `vibe start` | 按需启动 |
| **Restart** | `vibe restart` | 终止 |
| **Stop** | `vibe stop` | 终止 |

## 文件位置

| 路径 | 说明 |
|------|------|
| `~/.vibe_remote/config/config.json` | 主配置文件 |
| `~/.vibe_remote/state/settings.json` | 频道路由设置 |
| `~/.vibe_remote/state/scheduled_tasks.json` | 持久化的定时任务定义 |
| `~/.vibe_remote/state/task_requests/` | task run 与 hook 的请求队列 |
| `~/.vibe_remote/state/user_preferences.md` | 共享的长期用户偏好笔记 |
| `~/.vibe_remote/logs/vibe_remote.log` | 应用日志 |
| `~/.vibe_remote/logs/opencode_server.json` | OpenCode 服务器 PID 文件 |

## 环境变量

| 变量 | 说明 |
|------|------|
| `OPENCODE_PORT` | 覆盖 OpenCode 服务器端口（默认：4096） |

## 另请参阅

- [Slack 配置指南](SLACK_SETUP_ZH.md)
- [Telegram 配置指南](TELEGRAM_SETUP_ZH.md)
- [Codex 配置指南](CODEX_SETUP.md)
