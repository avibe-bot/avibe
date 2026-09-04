# Avibe CLI 参考手册

## 快速开始

```bash
vibe              # vibe start 的别名
vibe start        # 按需启动 Avibe（打开 Web UI）
vibe status       # 查看服务状态
vibe memory status # 通过运行中的控制器查看本地记忆状态
vibe remote       # 引导式配置 Avibe Cloud 远程访问
vibe screenshot   # 截取本机桌面截图
vibe stop         # 停止所有服务
```

## 命令详解

### 有界列表输出

面向 Agent 的集合命令共用同一套分页契约：`vibe agent list`、`vibe agent models`、
`vibe runs list`、`vibe session list`、Vault 的 list/find/tags、Show Page 的
list/marks、`vibe data query`、`vibe task list` 和 `vibe watch list`。默认返回
20 条，支持 `--page` 和 `--limit`，单页最多 100 条，且不提供无分页的 `--all`
绕过。使用 `pagination.next_command` 继续翻页；完整记录详情通过对应的 `show`
或 `get` 命令获取。

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

**已知限制 —— 部分重启后的记忆设置页。** Web UI 与主服务之间通过一个每次启动
现生成的凭据来校验本地记忆读取。该凭据只经 stdin 传给子进程，不会落盘，因此
`vibe start` 只能让它自己拉起的进程保持一致。当主服务已在运行、只有 Web UI 是
新启动的时候，两侧没有共享凭据，记忆设置页会显示记忆不可用，直到两个进程一起
重启为止；CLI 会打印恢复步骤 —— 先执行 `vibe stop`，再执行 `vibe`。反过来的情况
无需处理：主服务如果是新启动的，会顺带重启仍在运行的 Web UI，使新的一对共享同一
凭据。`vibe memory ...` 走的是另一套会话级授权，不受影响。

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

### `vibe skill`

Avibe 为 Claude、Codex 和 OpenCode 提供统一的托管 Skill Catalog，并在由 Avibe
发起的 Turn 中关闭各 backend 自带的 Skill Catalog。

```bash
vibe skill list [--page N]
vibe skill load -- <name>
```

`list` 按稳定顺序输出当前可用的名称和描述，每页最多 25 个。第 1 页也会注入
Agent 的 system prompt；如有更多 Skill，按输出中的命令查看下一页。`load` 只在
`skill_content` 标签中输出所选 Skill 的正文。标签的 `directory` 属性是绝对路径，
Agent 可以据此读取 `SKILL.md` 同目录下的 reference 或运行 script。

Avibe 会直接发现现有 Skill，无需迁移：

- 项目级：从工作目录到 Session 绑定的 Avibe 项目根目录逐层查找 `.agents/skills`、
  `.codex/skills`、`.claude/skills` 和 `.opencode/skills`。该边界可以位于嵌套 Git
  仓库之上；未绑定项目的独立命令则以遇到的第一个 Git 根目录为边界；
- 全局：查找 `~/.agents/skills`、Codex 与 Claude 配置的 Skill 目录，以及
  `XDG_CONFIG_HOME` 下的 OpenCode 目录和已启用的 Claude 插件 Skill 目录；
- Avibe 内置 Skill，以及 Codex 自带的 system Skill。

同名冲突时，内置 Skill 优先，其次是项目级，再次是全局。项目内更近的目录优先；
同一层级依次为 `.agents`、`.codex`、`.claude`、`.opencode`。用户 Skill 优先于
Codex 自带的默认项；已启用的 Claude 插件 Skill 排在四个静态用户目录之后，但同样
优先于这些默认项。新的全局 Skill 默认安装到 `~/.agents/skills/<name>`，项目级 Skill
安装到 `<project>/.agents/skills/<name>`。

每次命令都会从磁盘重新解析，每个由 Avibe 发起的新 Turn 也会重新生成 Catalog。
因此新增、修改或删除 Skill 后，已有 Session 无需重启 Avibe，也无需新建 Session；
历史对话内容不会被重写。

### `vibe memory`

通过现有 mode-0600 控制器 socket 读取当前范围内的本地记忆，或提交内容进行尽力而为的进程内捕获——既包括用户明确要求记住的内容，也包括 Agent 从对话以及在本机工作中主动提炼的结论（含在文件或工具输出中遇到的持久环境、账户事实）。接受请求不保证提供方投递或持久化。该命令不会启动服务，也没有清空、配置、导出或删除子命令。

`status` 可在普通终端中使用。`profile`、`list`、`search` 和 `remember` 必须在
Avibe 已注入当前 Session 上下文的合规 Agent shell 中运行；从普通终端运行会返回
`memory_access_denied`。

```bash
vibe memory status [--json]
vibe memory profile [--json]
vibe memory list [--project <slug>] [--page N] [--limit 1..100] [--json]
vibe memory search <查询> [--project <slug>] [--limit 1..100] [--json]
vibe memory remember <文本> [--project <slug>] [--json]
```

`list` 按时间倒序返回有效且已处理的事件。页码严格采用 EverOS 从 1 开始的语义，每页
默认 20 条；JSON 会包含每条事件的不透明 entry id。Agent CLI 只接受 `default` 或目录中
已有的具名项目，`--project all` 仅供设置页使用。该命令用于显式检查，不会加入注入的
个人记忆 prompt。

### `vibe doctor`

运行配置诊断检查。

```bash
vibe doctor
```

显式运行安全的一期修复：

```bash
vibe doctor repair --dry-run
vibe doctor repair home-migration --yes
vibe doctor repair duplicate-service-processes --yes
vibe doctor repair stale-install-runtime --yes
vibe doctor repair stale-restart-state --yes
vibe doctor repair askill --yes
vibe doctor repair avault --yes
vibe doctor repair git-runtime --yes
vibe doctor repair show-runtime --yes
vibe doctor repair tmux --yes
```

**检查内容：**
- 配置文件有效性
- Slack token 配置
- Agent CLI 可用性（Claude Code、OpenCode、Codex）
- runtime home 迁移状态
- runtime 进程、安装来源和重启元数据状态
- 通过统一依赖诊断组检查 askill、avault、Git Runtime、Show Runtime、tmux 和 Node.js
- `vibe doctor --deep` 还会在不下载正文的情况下探测缺失依赖的精确地址
- 托管下载会对临时 HTTP、DNS、超时和连接故障执行有界退避重试

### `vibe remote`

启动 Avibe Cloud 远程访问的引导式配置流程。

```bash
vibe remote
```

在 Organization 的保守发布版本中，远程工作台仍可用于 Organization 管理，以及按权限
查看 Project、Session、消息和历史记录。Agent 对话与运行控制、Harness 定义修改与自主执行、
终端和文件操作仅允许可信本机调用；如需执行这些操作，请在运行 Avibe 的机器上打开。

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
vibe session list                       # 未归档会话，默认每页 20 条，按最近活跃倒序
vibe session list --type slack          # 按平台过滤（avibe = Web/Workbench）
vibe session list --page 2 --limit 50   # 第 2 页，每页 50 条（最多 100）
vibe session get sesk8m4q2p7x           # 单个会话的完整明细
vibe session get                        # 在 Avibe Agent shell 内查看调用方 Session
vibe session update sesk8m4q2p7x --title 'Release review'   # 传 "" 可清空标题
vibe session update --title 'Release review'                 # 在 Avibe Agent shell 内
```

`--type` 取平台 id：`avibe`（Web/Workbench）、`slack`、`discord`、`telegram`、`lark`、`wechat`。需要更高级的筛选——按 Agent、时间段、消息内容或跨表联查——`list` 与 `get` 的返回都会引导你使用 `vibe data query`。
当 `get` 或 `update` 运行在 Avibe 已注入 caller context 的 Agent shell 内时，
可以省略 session id，并默认使用 `AVIBE_SESSION_ID` 对应的调用方 Session。

### `vibe runs`

列出和查看 Agent run 记录。

```bash
vibe runs list --session-id sesk8m4q2p7x --brief
vibe runs show run_abc123
vibe runs show                         # 在 Avibe Agent shell 内查看调用方 Run
```

`vibe runs list` 无过滤参数时仍保持全局列表语义；传入 `--session-id` 等参数
才会筛选。`vibe runs show` 在 Avibe 已注入 caller context 的 Agent run 内
可以省略 run id，并默认使用 `AVIBE_RUN_ID`。

### `vibe task`

创建、查看、更新、立即执行、暂停、恢复或删除定时任务。

```bash
vibe task add --session-id sesk8m4q2p7x --cron '0 * * * *' --message 'Share the hourly summary.'
vibe task add --cron '0 * * * *' --message 'Share the hourly summary.'   # 在 Avibe Agent shell 内
vibe task add --name nightly-sync --cron '0 3 * * *' --shell './scripts/sync.sh'   # 命令任务，不触发 Agent turn
vibe task list
vibe task update <task-id> --cron '*/30 * * * *'
vibe task run <task-id>
vibe task remove <task-id>
```

更完整的参数说明请直接看 `vibe task add --help` 和 `vibe task update --help`。其中重点包括：

- 用 `--session-id` 指定要延续的 Agent Session
- 用 `--create-session`、`--create-session-per-run`、`--same-scope` 和 `--scope-id` 控制 Session placement
- 用 `--cron` / `--at` 控制定时方式
- 以及 `--name`、`--timezone`、`--message-file` 等参数
- 用 `--shell` 或写在 `--` 之后的 argv 创建不触发 Agent turn 的定时命令任务，
  可搭配 `--on-failure {none,agent}`、按次执行的 `--timeout`
  （默认 21600 秒，`0` 表示不限制），以及指定命令执行目录的 `--cwd`

当 `vibe task add` 运行在 Avibe 已注入 caller context 的 Agent shell 内时，
可以省略 `--session-id`。Avibe 会把任务目标默认到 `AVIBE_SESSION_ID`
对应的调用方 Session，并在命令输出里报告这次默认。显式 `--session-id`、
session creation 参数和 delivery 参数仍然优先。

纯命令任务（`--on-failure none`，即默认值）不会套用上面这条调用方 Session 默认，
也不接受 session、scope 或 agent 相关参数。执行成功时完全静默；执行失败会记录一条
持久化的失败通知，并在通知中写明命令与退出码。若使用
`--on-failure agent --message '<处理说明>'`，失败会改为触发一次携带失败报告的 Agent
turn，并由该 turn 取代这次执行的失败通知。`vibe task update` 可以修改命令任务的
`--shell`、argv、`--timeout` 或 `--cwd`，但在 message 形态与 command 形态之间切换、
或修改 `--on-failure`，都会被拒绝——请删除任务后重新创建。

`--cwd` 指定命令的执行目录。它是否还会影响 Session，取决于该定义是否要「创建」
Session：

- 绑定到已存在的 Session（`--session-id`，或调用方 Session 默认值），或已经预留过
  可复用 Session 时：该参数只作用于命令。升级 Session 保留自己的工作目录——这正是
  引入该参数要解决的场景：命令任务绑定 Session 是为了让 `--on-failure agent`
  有地方落地，而不是为了指定命令在哪里执行。
- 需要创建 Session 时（`--create-session`、`--create-session-per-run`）：该参数
  同时决定这个 Session 的目录，也就是升级 turn 会和命令跑在同一个目录。如果希望
  Session 继承目录，请改用 `--same-scope` / `--scope-id` 指定 scope 并省略 `--cwd`。

不传该参数时，绑定了 Session 的命令会跟随那个 Session 的目录（该目录在触发时实时
读取，因此在那个会话里执行 `/setcwd` 会让任务换个地方运行），其他命令则记录你执行
`vibe task add` 时所在的目录。对 message 任务，`--cwd` 仍然用于放置它创建的
Session，且在目标 Session 已存在时仍然会被拒绝。

`--session-key` 仍兼容旧脚本，但新任务应使用当前 Avibe prompt
里展示的 Agent Session ID。

### `vibe agent run`

直接运行一个 Agent。Run 默认异步，并且不会创建持久化任务定义。只有终端需要等待完成时才使用
`--sync`。

```bash
vibe agent run --no-callback --agent release-reviewer --message 'Review the latest deployment result.'
vibe agent run --sync --agent release-reviewer --message 'Review the latest deployment result and print it here.'
vibe agent run --no-callback --session-id sesk8m4q2p7x --message 'The export finished. Share the summary.'
vibe agent run --session-id sesk8m4q2p7x --send-now --message 'Apply this correction in the current turn.'
vibe agent run --no-callback --fork-session sesk8m4q2p7x --message 'Explore this alternate fix from the current context.'
vibe agent run --session-id sesworker123 --callback-session-id sescaller456 --message 'Run the delegated investigation.'
vibe agent run --no-callback --create-session --scope-id slack::channel::C999 --agent release-reviewer --message 'Post the deployment summary.'
```

`--send-now` 只能和现有 `--session-id` 一起使用，它显式选择普通的带内容 P1
语义：新消息会 steering 进活动 native Turn，Session 空闲时立即启动；只有明确
拒绝后才回退到 P3。它不会提升更早的排队消息。`vibe session send-now` 是无内容
P1 操作，只提升现有的精确 FIFO 队头，不新增消息。过期队头会被拒绝，而不会改为
提升下一条；两个命令都不会调用 Stop。

当一个新 Agent Session 需要从现有 Session 的 native backend 上下文分叉，而不是空白开始时，
使用 `--fork-session <session-id>`。新 Session 会保持源 Session 的 backend。
只有 backend 不变时，才可以通过 `--agent`、`--model`、`--reasoning-effort`
覆盖 fork 后 Session 的 Agent、模型或推理强度；跨 backend fork 会被拒绝。
不要把 `--fork-session` 和 `--session-id` 或 `--create-session` 混用。

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

vibe watch list
vibe watch show <watch-id>
vibe watch update <watch-id> --name 'Watch deployment' --timeout 1200
vibe watch pause <watch-id>
vibe watch resume <watch-id>
vibe watch remove <watch-id>
```

`vibe task list` 和 `vibe watch list` 默认每页返回 20 条定义；还有下一页时，
响应会包含 `pagination.next_command`。默认隐藏成功结束的一次性定义；使用
`--include-finished` 分页查看历史。列表输出始终有上限，不提供无分页的 `--all` 模式。
task 和 watch 命令用 `definition` 表示单条记录、用 `definitions` 表示列表，
不会再通过命令专属别名重复输出同一份记录。
list 和 show 与 Workbench 读取同一份 Harness 投影：`lifecycle_state`、
`lifecycle_detail`、`next_run_at`、`waiting_since` 和 `running_since`；
watch 还会返回 `process_alive`。对 watch 来说，`process_alive: null`
表示从未观测到 waiter runtime，`false` 表示曾观测到的 waiter 已退出。
旧的 `state` 和 task 的 `last_status` 仅作为兼容展示字段保留，不定义 lifecycle。

waiter 命令放在 `--` 后面；或者通过 `--shell` 传入一整段 shell 字符串。
完整参数请看 `vibe watch add --help`，包括 `--timeout`、`--lifetime-timeout`、
`--forever`、`--retry-exit-code`、`--retry-delay`、`--name` 和 session creation 参数。
watch 与 `vibe task`、`vibe agent run` 共用 `--session-id`、`--create-session`、
`--same-scope` 和 `--scope-id` 语义；`--create-session-per-run`
只属于 `vibe task` 和 `vibe watch` 这类 stored definitions。需要可管理、可暂停、可查看的
后台等待任务时，优先使用 `vibe watch`，不要随手起 `nohup`。
`--timeout` 默认是 21600 秒；显式传入 `--timeout 0` 会关闭单次 cycle 的
超时限制，任何正数值都会原样持久化。

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
