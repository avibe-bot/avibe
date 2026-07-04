# Avibe 命令参考手册

这是一份尽量完整的 Avibe 命令参考文档。

它覆盖两大类命令：

- 在 Slack、Discord、Telegram、微信、飞书里直接发给机器人的聊天命令
- 在安装了 Avibe 的宿主机上执行的 `vibe` CLI 命令

相关文档：

- [CLI 参考手册](./CLI_ZH.md)
- [Slack 配置指南](./SLACK_SETUP_ZH.md)
- [Telegram 配置指南](./TELEGRAM_SETUP_ZH.md)

## 1. 命令总览

Avibe 当前有两套命令面：

1. 聊天命令
   - 从 IM 里发给 bot
   - 例如：`/start`、`/resume`、`/setcwd ~/repo`、`/setup`
2. 宿主机 CLI 命令
   - 在运行 Avibe 的那台机器上执行
   - 例如：`vibe`、`vibe status`、`vibe task add ...`

这两类命令解决的问题不同：

- 聊天命令负责会话控制、工作目录切换、恢复历史会话、DM 绑定、后端认证修复
- CLI 命令负责本地服务进程、诊断、升级、定时任务和异步 hook

## 2. 聊天命令

### 2.1 当前支持的聊天命令

控制器当前注册了这些命令：

| 命令 | 作用 |
| --- | --- |
| `/start` | 显示欢迎面板 / 控制入口 |
| `/new` | 开启一个全新会话 |
| `/clear` | `/new` 的别名 |
| `/cwd` | 查看当前工作目录 |
| `/setcwd <路径>` | 设置工作目录 |
| `/set_cwd <路径>` | 内部风格别名，也能用 |
| `/resume` | 恢复最近会话 |
| `/setup` | 修复后端登录 / 认证 |
| `/settings` | 打开设置 UI |
| `/stop` | 中断当前后端执行 |
| `/bind <绑定码>` | 在 DM 里绑定当前用户 |
| `bind <绑定码>` | 仅限未绑定 DM 用户的纯文本别名 |

### 2.2 权限模型

权限由 `core/auth.py` 统一处理。

#### 已授权频道里的普通用户可用

- `/start`
- `/new`
- `/clear`
- `/cwd`
- `/resume`
- `/stop`

#### 仅管理员可用

- `/setcwd <路径>`
- `/set_cwd <路径>`
- `/settings`
- `/setup`

补充说明：

- `/setup` 相关按钮回调，例如 `Reset OAuth`，也属于管理员操作。
- 如果当前平台已经存在管理员，非管理员不能执行这些管理员命令。

#### DM 绑定例外

- `/bind <绑定码>` 允许未绑定的 DM 用户执行。
- `bind <绑定码>` 也允许未绑定的 DM 用户执行，用来兼容某些平台对 `/` 开头消息的不便场景。

### 2.3 平台差异

#### Slack

- 目前原生 Slack slash command 只公开了 `/start` 和 `/stop`。
- 其它命令通常以“普通消息 + bot 定向”的方式触发，例如：
  - `@Avibe /resume`
  - `@Avibe /setcwd ~/work/repo`
- 在 DM 中，未绑定用户可以直接发送 `bind <code>`。

#### Discord

- 命令通过普通消息解析，要求消息以 `/` 开头。
- `/resume` 在有 interaction 上下文时会优先打开 Discord 原生恢复选择器。

#### Telegram

- 命令通过普通消息解析，要求消息以 `/` 开头。
- `/resume`、`/settings` 会优先走当前聊天里的内联按钮交互。

#### 飞书 / Lark

- 命令通过普通消息解析，要求消息以 `/` 开头。
- `/resume`、`/settings` 等会优先走卡片 / modal 交互。

#### 微信

- 命令通过普通文本解析，要求消息以 `/` 开头。
- `/resume` 是纯文本交互，不走 modal。
- `/resume 1`、`/resume more`、`/resume latest ...`、手动 session_id 恢复，主要是为微信文本流设计的。

### 2.4 解析规则与别名

共享解析器 `modules/im/base.py` 当前有这些归一化规则：

- `/setcwd /tmp/work` 会被归一化为内部动作 `set_cwd`
- `/set_cwd /tmp/work` 也能正常工作，因为 `set_cwd` 本身已经注册为命令
- `bind abc123` 只有在允许 plain bind 的 DM 场景下才会被识别

`/resume` 和 `/setup` 使用的 backend 别名：

| 别名 | 对应后端 |
| --- | --- |
| `oc` | `opencode` |
| `open-code` | `opencode` |
| `cc` | `claude` |
| `claude-code` | `claude` |
| `cx` | `codex` |

## 3. 聊天命令详细说明

### `/start`

显示欢迎消息，以及当前频道或 DM 作用域下的控制入口。

#### 语法

```text
/start
```

#### 作用

- 展示当前平台
- 展示当前作用域解析出来的 backend
- 在适用场景下展示当前频道名
- 列出主要文本命令
- 在支持按钮的平台上展示交互式菜单

#### 常见用法

```text
@Avibe /start
```

#### 备注

- 如果用户不确定当前配置，先执行 `/start` 最稳妥。
- 某些平台会把命令回复发回频道，而不是发在线程里。

### `/new`

重置当前会话状态，让下一条消息从一个全新的对话开始。

#### 语法

```text
/new
```

#### 作用

- 清除当前作用域的活动会话状态
- 不会删除仓库
- 不会修改路由和工作目录

### `/clear`

`/new` 的别名。

#### 语法

```text
/clear
```

#### 作用

- 实际上会直接走 `/new` 的处理逻辑

#### 建议

- 面向用户的文档建议优先写 `/new`
- `/clear` 保留为兼容和习惯用法

### `/cwd`

查看当前频道或 DM 作用域绑定的工作目录。

#### 语法

```text
/cwd
```

#### 作用

- 输出当前绝对路径
- 提示该目录是否存在
- 提醒用户这里就是 Agent 执行命令的位置

#### 典型场景

- 在让 Agent 改代码之前，先确认当前 scope 指向的是正确仓库。

### `/setcwd <路径>`

为当前频道或 DM 作用域设置工作目录。

#### 语法

```text
/setcwd <路径>
```

也支持：

```text
/set_cwd <路径>
```

#### 作用

- 自动展开 `~`
- 自动转成绝对路径
- 如果目录不存在，会尝试创建
- 把这个路径保存到当前 settings scope

#### 示例

```text
/setcwd ~/projects/myapp
/setcwd /srv/repos/api
/set_cwd ../another-repo
```

#### 权限

- 仅管理员

#### 备注

- 在频道里，作用域是当前频道。
- 在 DM 里，作用域是当前用户。

### `/resume`

从当前工作目录里恢复最近的原生 Agent 会话。

#### 基础语法

```text
/resume
```

#### 文本模式子命令

```text
/resume 1
/resume more
/resume latest
/resume latest oc
/resume latest cc
/resume latest cx
/resume <backend> <session_id>
```

#### 支持的 backend 写法

- `oc`
- `opencode`
- `open-code`
- `cc`
- `claude`
- `claude-code`
- `cx`
- `codex`

#### 各平台行为

##### Slack

- `/resume` 在有交互上下文时会直接打开恢复选择器。
- 如果当前消息没有 modal trigger，bot 会提示用户回到菜单里点 `Resume Session`。

##### Discord

- `/resume` 在有 interaction 上下文时会打开原生恢复选择流程。

##### Telegram

- `/resume` 会在当前聊天里打开内联按钮选择器。

##### 飞书

- `/resume` 优先走卡片 / modal 流程。

##### 微信

- `/resume` 走纯文本交互。
- `/resume 1` 恢复当前列表中的第 1 项。
- `/resume more` 用于翻页。
- `/resume latest [backend]` 恢复最近会话。
- `/resume <backend> <session_id>` 用于手动恢复指定会话。

#### 典型用法

```text
/resume
```

然后在微信文本流里继续：

```text
/resume 1
/resume latest cc
/resume codex 123e4567-thread-id
```

#### 备注

- 恢复范围只看当前工作目录下的会话。
- 如果工作目录已经变了，之前展示出来的编号列表会过期。

### `/setup`

通过 IM 完成后端登录或 provider auth 修复。

#### 语法

```text
/setup
/setup claude
/setup codex
/setup opencode
/setup cc
/setup cx
/setup oc
/setup code <value>
/setup code <backend> <value>
```

#### 作用

- 如果没有显式 backend，就使用当前作用域绑定的 backend
- 启动对应后端的认证修复流程
- 把浏览器链接、device code、后续输入提示发回聊天
- 等待流程完成后做状态校验

#### 分 backend 行为

##### Claude

- 启动 Claude 登录流程
- 把浏览器授权 URL 发回聊天
- 如果 Claude 后续要求输入粘贴 code，用户在聊天里继续发：

```text
/setup code <value>
```

##### Codex

- 启动 device auth
- 把浏览器 URL 和一次性 code 发回聊天
- bot 会等待完成并用 `codex login status` 做校验

##### OpenCode

- 优先根据当前 OpenCode 路由模型推断 provider
- `openai` 走 headless device 风格认证
- 常见 provider，如 `opencode`、`anthropic`，走 key 输入流
- 当 OpenCode 提示要输入 key 时，用户在聊天里发：

```text
/setup code <value>
```

#### 权限

- 仅管理员

#### 备注

- `/setup code <backend> <value>` 适合多个 setup flow 并行时显式指定 backend。
- 只有发起该 setup flow 的用户才能提交后续验证码或 key。

### `/settings`

打开当前作用域的设置 UI。

#### 语法

```text
/settings
```

#### 作用

- 打开或路由到当前平台对应的设置面板
- 通过 UI 流程修改 routing 等作用域配置

#### 权限

- 仅管理员

#### 备注

- 这个命令已经注册，但不同平台的具体 UI 形态不完全一样。
- 如果用户希望走引导式配置，而不是记命令，优先让他用这个。

### `/stop`

中断当前作用域下正在执行的 backend 任务。

#### 语法

```text
/stop
```

#### 作用

- 根据当前作用域解析 backend
- 生成 stop 请求并交给 backend adapter 执行
- 如果当前没有活动会话，会返回提示消息

#### 备注

- 在支持线程的平台里，线程内直接发 `stop` 或 `/stop` 也可能被消息流识别。
- 这个命令不会顺手改路由，也不会删除历史状态。

### `/bind <绑定码>`

用绑定码把当前 DM 用户绑定到这个 Avibe 实例。

#### 语法

```text
/bind <绑定码>
bind <绑定码>
```

#### 作用

- 校验绑定码
- 把当前 DM 用户记录为已绑定
- 保存 DM chat ID
- 在首个 bootstrap bind 等场景里，可能同时赋予管理员身份

#### 权限和上下文

- 只能在 DM 里使用
- 即使用户尚未绑定，也允许执行

#### 示例

```text
/bind vr-a3x9k2
bind vr-a3x9k2
```

#### 备注

- `bind <绑定码>` 是兼容路径，主要应对某些平台里 `/` 开头输入不方便的情况。
- 如果用户已经绑定，会返回 already bound，而不是重复绑定。

## 4. 哪些不是命令

这些是重要的用户入口，但它们不是文本命令：

- `AgentName: 你的消息`
  - 例如：`Plan: 设计一个新的缓存层`
  - 这是 subagent prefix，不是命令。
- `/start` 菜单按钮
  - 例如：`Settings`、`Resume Session`、`Change Work Dir`
  - 这些是按钮回调或 modal 流程，不是 slash command。

## 5. 宿主机 CLI 命令

`vibe` 可执行文件负责管理本地服务与异步自动化能力。

## 5.1 顶层 CLI 命令

| 命令 | 作用 |
| --- | --- |
| `vibe` | `vibe start` 的别名 |
| `vibe start` | 按需启动服务与 Web UI；已运行时复用现有进程 |
| `vibe stop` | 停止服务与 UI，同时终止 OpenCode server |
| `vibe restart` | 停止后重新启动 |
| `vibe status` | 输出运行状态 JSON |
| `vibe doctor` | 运行诊断；`vibe doctor repair` 显式执行安全修复 |
| `vibe remote` | 引导式配置 Avibe Cloud 远程 Web UI |
| `vibe screenshot` | 截取本机桌面截图 |
| `vibe version` | 查看当前版本 |
| `vibe check-update` | 检查是否有新版本 |
| `vibe upgrade` | 升级到最新版 |
| `vibe agent ...` | 管理 Avibe Agent，并直接运行 Agent |
| `vibe runs ...` | 查看和取消 Agent Run 记录 |
| `vibe task ...` | 管理定时任务 |

### `vibe`

```bash
vibe
```

- `vibe start` 的别名
- 如果主服务尚未运行，则启动主服务
- 打开 Web UI
- 保留已运行的服务、Web UI 与 OpenCode server；需要明确重启时使用 `vibe restart`

### `vibe start`

```bash
vibe start
```

- 如果主服务尚未运行，则启动主服务
- 打开 Web UI
- 保留已运行的服务、Web UI 与 OpenCode server；需要明确重启时使用 `vibe restart`

### `vibe stop`

```bash
vibe stop
```

- 停止主服务
- 停止 UI 服务
- 同时终止 OpenCode server

### `vibe restart`

```bash
vibe restart
```

- 停止主服务
- 停止 UI 服务
- 同时终止 OpenCode server
- 短暂等待后重新启动服务

可选的异步延迟执行：

```bash
vibe restart --delay-seconds 60
```

- 会立即打印确认信息
- 当前命令立刻返回
- 到达指定延迟后在后台执行重启

推荐用法：

- 如果是 Agent 在活跃会话里触发重启，优先使用 `vibe restart --delay-seconds 60`
- 只有用户明确要求立刻重启时，再使用普通 `vibe restart`

### `vibe status`

```bash
vibe status
```

- 输出运行状态 JSON

### `vibe doctor`

```bash
vibe doctor
```

- 校验配置
- 检查平台凭据
- 检查 backend CLI 是否可用
- 检查 runtime home 迁移状态
- 检查 runtime 进程、安装来源和重启元数据状态

修复模式需要显式执行，并支持 dry-run：

```bash
vibe doctor repair --dry-run
vibe doctor repair home-migration --yes
vibe doctor repair duplicate-service-processes --yes
vibe doctor repair stale-install-runtime --yes
vibe doctor repair stale-restart-state --yes
```

### `vibe remote`

```bash
vibe remote
```

- 启动 Avibe Cloud 远程访问的引导式配置
- 先解释远程访问的作用，再请求 pairing key
- 引导用户打开 `https://avibe.bot`，创建 remote-access bot，领取个人域名，并复制一次性 pairing key
- 配对后保存远程访问凭证，并启动安全 tunnel

常用后续命令：

```bash
vibe remote status
vibe remote start
vibe remote stop
```

如果你已经拿到 pairing key：

```bash
vibe remote pair vrp_abc123
```

### `vibe screenshot`

```bash
vibe screenshot
vibe screenshot --output /tmp/screen.png
vibe screenshot --json
```

- 截取本机桌面并保存为 PNG 文件
- 默认保存到 `~/.vibe_remote/screenshots/`
- 默认输出保存路径
- 加 `--json` 时输出机器可读的 JSON
- 只停留在宿主机 CLI 层；不新增聊天命令、bot 按钮，也不注入 Agent prompt

### `vibe version`

```bash
vibe version
```

- 输出当前安装版本

### `vibe check-update`

```bash
vibe check-update
```

- 检查 PyPI 是否有更高版本

### `vibe upgrade`

```bash
vibe upgrade
```

- 按升级计划升级 Avibe
- 如果 Avibe 已在运行，成功后安排一次受控重启
- 如果 Avibe 原本未运行，则保持停止状态

## 5.2 `vibe task`

`vibe task` 用来管理持久化的定时任务。

### 支持的子命令

| 子命令 | 作用 |
| --- | --- |
| `vibe task add` | 创建任务 |
| `vibe task update` | 更新任务 |
| `vibe task list` | 列出任务 |
| `vibe task ls` | `list` 的隐藏别名 |
| `vibe task show <task_id>` | 查看单个任务 |
| `vibe task pause <task_id>` | 暂停任务 |
| `vibe task resume <task_id>` | 恢复任务 |
| `vibe task run <task_id>` | 立即运行一次 |
| `vibe task remove <task_id>` | 删除任务 |
| `vibe task rm <task_id>` | `remove` 的隐藏别名 |

### `vibe task add`

```bash
vibe task add (--session-id <session_id> | --create-session | --create-session-per-run) (--cron <表达式> | --at <时间戳>) (--message <文本> | --message-file <文件>) [options]
```

重要参数：

- `--name`
- `--session-id`
- `--create-session`
- `--create-session-per-run`
- `--agent`
- `--same-scope`
- `--scope-id`
- `--cron`
- `--at`
- `--message`
- `--message-file`
- `--timezone`

### `vibe task update`

```bash
vibe task update <task_id> [options]
```

重要参数：

- `--name`
- `--clear-name`
- `--session-id`
- `--create-session`
- `--create-session-per-run`
- `--agent`
- `--same-scope`
- `--scope-id`
- `--reset-delivery`
- `--cron`
- `--at`
- `--message`
- `--message-file`
- `--timezone`

### `vibe task list`

```bash
vibe task list [--all] [--brief]
```

### `vibe task show`

```bash
vibe task show <task_id>
```

### `vibe task pause`

```bash
vibe task pause <task_id>
```

### `vibe task resume`

```bash
vibe task resume <task_id>
```

### `vibe task run`

```bash
vibe task run <task_id>
```

### `vibe task remove`

```bash
vibe task remove <task_id>
```

## 5.3 `vibe agent`

`vibe agent` 用来管理 Avibe 自己的一等 Agent 定义，并直接运行 Agent。

Agent 名称全局唯一。创建后 `name` 和 `backend` 不可修改；description、
model、reasoning effort、system prompt 可以编辑。

### 支持的子命令

| 子命令 | 作用 |
| --- | --- |
| `vibe agent list` | 列出 Agent |
| `vibe agent show <name>` | 查看单个 Agent |
| `vibe agent models [<name>]` | 列出某个 Agent 或后端可用的模型与推理强度 |
| `vibe agent create` | 创建 Agent |
| `vibe agent update <name>` | 修改可编辑字段 |
| `vibe agent remove <name>` | 删除 Agent |
| `vibe agent import` | 导入全局 Agent 或通用 Agent 文件 |
| `vibe agent run` | 运行一次 Agent |

### `vibe agent models`

列出某个 Agent（按名）或某个后端可用的模型与推理强度。推理强度按模型返回，因为它是模型的属性
（Claude 的 `xhigh` / `max` 取决于模型；OpenCode 因 variant 而异）。对 OpenCode，列表会包含自定义
provider 与用户手动添加的模型。

```
vibe agent models <name>             # 某个 Agent：解析其后端与当前模型
vibe agent models --backend claude   # 直接查后端，用于创建 Agent 之前
vibe agent models --backend opencode --provider deepseek
```

- `<name>` 与 `--backend` 二选一
- `--provider <id>` 按 provider 过滤，仅对 OpenCode 后端有效
- `--model <id>` 只返回单个模型的推理强度

按 `<name>` 查询时，结果包含 `current`——该 Agent 配置的 `model` / `reasoning_effort` 及其是否仍然有效。
`create` / `update` 接受任意取值，但当取值不在已知集合中时会给出警告（不拒绝），并指回本命令。

### `vibe agent run`

```bash
vibe agent run (--session-id <session_id> | --create-session | --fork-session <session_id>)? (--message <文本> | --message-file <文件>) [options]
```

重要参数：

- `--agent`
- `--session-id`
- `--fork-session`
- `--create-session`
- `--same-scope`
- `--scope-id`
- `--model`
- `--reasoning-effort`
- `--sync`
- `--message`
- `--message-file`

如果不传 `--session-id` 或创建策略，run 会使用 private
no-delivery session，更适合 sub-agent 调用。

Run 默认异步：命令会队列化 run，立即返回包含 `run_id` / `session_id` 的
payload，并按 callback 策略稍后投递最终结果。只有终端需要等待完成时才使用
`--sync`。`--async` 仍兼容旧脚本，但不再需要显式传入。

`--fork-session <session_id>` 会基于源 Session 的 native backend 上下文创建一个新的
Agent Session，适合在保留上下文的同时做分支调查或委派工作，而不修改源 Session。
fork 会保持源 Session 的 backend；只有 backend 不变时，`--agent`、`--model`
和 `--reasoning-effort` 才能覆盖 fork 后 Session 的 Agent、模型或推理强度。
不要把 `--fork-session` 和 `--session-id` 或 `--create-session` 混用。

## 5.4 `vibe runs`

`vibe runs` 用来查看由 task、watch 或直接 Agent Run 产生的一次次实际执行记录。

### 支持的子命令

| 子命令 | 作用 |
| --- | --- |
| `vibe runs list` | 列出最近 run |
| `vibe runs show <run_id>` | 查看单个 run |
| `vibe runs cancel <run_id>` | 请求取消 run |

## 6. 推荐心智模型

按用途选命令：

- 想在聊天里控制会话：
  - 用 `/start`、`/resume`、`/setcwd`、`/setup`、`/stop`
- 想开通或恢复 DM 使用权限：
  - 在 DM 里用 `/bind <code>`
- 想管理本地守护进程或排查安装问题：
  - 用 `vibe`、`vibe status`、`vibe doctor`、`vibe upgrade`
- 想做异步自动化：
  - 用 `vibe task ...`、`vibe watch ...` 或 `vibe agent run ...`

## 7. 快速示例

### 聊天里

```text
@Avibe /start
@Avibe /cwd
@Avibe /setcwd ~/projects/backend
@Avibe /setup
@Avibe /setup codex
@Avibe /setup code 123456
@Avibe /stop
```

### 宿主机上

```bash
vibe
vibe status
vibe doctor
vibe task list --brief
vibe agent run --no-callback --session-id sesk8m4q2p7x --message 'Share the latest build summary.'
```
