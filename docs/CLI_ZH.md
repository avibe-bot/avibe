# Avibe CLI 参考手册

## 快速开始

```bash
vibe              # vibe start 的别名
vibe start        # 按需启动 Avibe（打开 Web UI）
vibe start --no-open-browser  # 启动/复用 Avibe，但不打开浏览器
vibe status       # 查看服务状态
vibe remote       # 引导式配置 Avibe Cloud 远程访问
vibe screenshot   # 截取本机桌面截图
vibe stop         # 停止所有服务
```

## 命令详解

`vibe` CLI 主要由 Agent 调用，因此所有命令遵循同一套语法和同一条输出规则。

### 语法

- **指定 Session。** 只用 `--session-id <id>` 指定 Session。作用于单个 Session 的
  命令（`vibe session get`、`update`、`send-now`、`queue list`、`queue remove`
  以及各个 `vibe show` 命令）和 [Session 归属的列表](#session-归属的列表) 一样，
  默认使用 `AVIBE_SESSION_ID` 对应的调用方 Session。在 Avibe Agent shell 之外
  没有默认值，命令以 `missing_caller_session` 失败。
- **指定其他对象。** Agent 名称，run、task、watch、Vault secret 的 id，以及排队
  消息的 id 都是位置参数：`vibe session queue remove <message-id> --session-id <id>`。
- **消息文本。** 所有发送或保存消息的命令都使用 `--message` 或 `--message-file`。
- **可见性。** `--visible` 让 `vibe agent run` 新建或 fork 的 Session 面向用户
  （新建的委派 Session 默认在后台）。`vibe session update` 使用 `--visible` 或
  `--hidden`。
- **只有一种写法。** 没有别名、隐藏的兼容参数或已弃用的命令；已移除的写法会作为
  未知参数报错。

### 输出

- **面向 Agent 的命令**（`agent`、`runs`、`harness`、`session`、`task`、`watch`、
  `show`、`vault`、`data query`、`data skill-usage`、`skill list` 和 `status`）
  在 stdout 输出一个 JSON 对象：
  `{"schema_version": 1, "ok": true, "kind": "<kind>", ...字段}`。它们接受
  `--json`，但不改变任何行为。
- **失败**：这些命令的失败以及所有参数错误，都会在 stderr 输出同样的信封，其中
  `"ok": false`、`"kind": "error"`，并带有稳定的 `code`、`error` 文本，必要时还有
  `hint`、`help_command` 和 `details`。退出码非零。
- **内容型命令**直接输出内容：`vibe skill load` 输出 Skill 正文，`vibe computer`
  输出精简的观察格式（`--json` 返回驱动的原始负载），`vibe vault run` 原样转出所运行
  命令的输出，`vibe debug prompt export` 输出它的导出文档。
- **运维命令**面向终端前的用户，输出文本。`version`、`check-update`、`doctor`、
  `runtime`、`remote` 子命令、`screenshot` 和 `data retention` 加 `--json` 时输出
  JSON 信封。服务生命周期命令 `start`、`stop`、`restart`、`upgrade` 以及引导式
  `vibe remote` 配置只输出进度文本，不接受 `--json`。
- **语言。** CLI 的帮助、错误、提示和通知始终是英文，与配置的界面语言无关。IM、
  Web UI 和通知遵循配置的语言；Web 诊断面板用该语言显示与 `vibe doctor` 相同的诊断。

### 有界列表输出

面向 Agent 的集合命令共用同一套分页契约：`vibe agent list`、`vibe agent models`、
`vibe runs list`、`vibe session list`、`vibe session queue list`、Vault 的
list/find/tags、Show Page 的 list/marks、`vibe data query`、`vibe task list` 和
`vibe watch list`。默认返回 20 条，支持 `--page` 和 `--limit`。单页最多 100 条：
更大的 `--limit` 按 100 读取，`pagination.limit` 报告实际使用的上限。小于 1 的
`--limit` 或 `--page` 返回 `invalid_pagination` 错误。不提供无分页的 `--all` 绕过。
使用 `pagination.next_command` 继续翻页；完整记录详情通过对应的 `show` 或 `get`
命令获取。`vibe skill list` 与 system prompt 中的 Skill 目录分页一致，因此只接受
`--page`。

列表行只携带决定下一步所需的字段，并省略 null、空值和零值字段，因此缺失的字段即表示
未设置。`vibe task show`、`vibe watch show`、`vibe runs show`、`vibe session get`
和 `vibe agent show` 输出完整记录；加上 `--brief` 则只输出对应 list 命令打印的那一行。

### Session 归属的列表

`vibe harness status`、`vibe runs list`、`vibe task list`、`vibe watch list`
和 `vibe show list` 读取归属于某个 Agent Session 的记录，共用同一套范围语法：

```bash
vibe watch list                          # 调用方 Session（AVIBE_SESSION_ID）
vibe watch list --session-id sesk8m4q2p7x   # 其他 Session
vibe watch list --global                 # 所有 Session
```

在 Avibe Agent shell 内默认读取调用方 Session。在 Agent shell 之外没有默认值：
需要传 `--session-id` 或 `--global`，否则命令以 `missing_caller_session` 失败。
两个参数不能同时使用。每个响应都用 `session_id` 和 `global` 说明实际读取的范围，
`pagination.next_command` 会重复你传入的范围参数。

Session 的 Run 包括在它里面运行的、由它的 Agent 派发的，以及结果回调给它的 Run，
因此委派出去的工作对委派方 Session 仍然可见。Watch 归属于它回调的 Session，
Task 归属于创建它的 Session（创建方未知或已不存在时归属于它运行所在的 Session），
Show Page 按 Session id 归属；Watch 和 Task 的归属与 Workbench 的“只看本会话”一致。
`vibe harness status` 会保留控制器故障，以及涉及该 Session 的工作目录冲突。

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
- 从比最近一次激活更旧的安装运行时只给出警告：裸 `vibe` 也是 systemd 和 launchd 的启动入口，因此它不会拒绝启动

### `vibe start`

按需启动 Avibe。默认会在浏览器中打开 Web UI；也可以显式抑制。

```bash
vibe start
vibe start --no-open-browser
vibe start --allow-downgrade
```

**行为：**
- 如果主服务与 Web UI 已在运行，则复用现有进程
- 打开设置向导 `http://127.0.0.1:5123`
- 使用 `--no-open-browser` 时，服务和 Web UI 的启动/复用逻辑不变，但不会额外拉起系统浏览器窗口
- **保留已运行的进程** — 需要明确重启时请使用 `vibe restart`
- `vibe start` 和 `vibe restart` 会拒绝运行比最近一次激活更旧的安装，例如 `PATH` 中更靠前的旧 `vibe`。错误信息会写明两个版本以及应改用的启动器；如确实要运行旧版本，请加 `--allow-downgrade`。桌面应用只给出警告。
- 由桌面应用启动时，只复用或替换该应用自己 Runtime 的主服务和 Web UI。如果这里运行的是另一个 Avibe，会说明原因并以状态码 3 退出，那个 Avibe 继续运行。

### `vibe stop`

完全停止所有 Avibe 服务。

```bash
vibe stop
```

**行为：**
- 停止主服务
- 停止 Web UI 服务器
- **终止 OpenCode 服务器** — 当你需要重启 OpenCode 时使用此命令
- 在桌面应用的 Runtime 内运行时（例如由它启动的 Agent），`vibe stop` 和 `vibe restart` 只作用于该 Runtime。如果这里运行的是另一个 Avibe，则什么都不停止，说明原因并以状态码 3 退出；在 Web UI 中重启也会显示同样的拒绝，而不会重启。

### `vibe status`

显示当前服务状态。

```bash
vibe status
```

**输出示例：**
```json
{
  "schema_version": 1,
  "ok": true,
  "kind": "service_status",
  "state": "running",
  "running": true,
  "pid": 12345
}
```

如果正在运行的服务比最近一次激活的安装更旧，输出还会包含 `generation_downgrade`
对象：其中有正在运行的 `version` 和 `generation`、`activated_version` 和
`activated_generation`，以及选中较新安装的 `launcher`。通过这个启动器重启即可运行新版本。

### `vibe skill`

Avibe 为 Claude、Codex 和 OpenCode 提供统一的托管 Skill Catalog，并在由 Avibe
发起的 Turn 中关闭各 backend 自带的 Skill Catalog。

```bash
vibe skill list [--page N]
vibe skill load -- <name> [<name> ...]
```

`list` 按稳定顺序以 `skills` 返回当前可用的名称和描述，并附带 `pagination` 对象。
第 1 页也会注入 Agent 的 system prompt；如有更多 Skill，按
`pagination.next_command` 查看下一页。`load` 按给定顺序，把每个 Skill 的正文
分别放在各自的 `skill_content` 标签中输出。标签的 `directory` 属性是绝对路径，
Agent 可以据此读取 `SKILL.md` 同目录下的 reference 或运行 script。遇到未知名称时，
已加载的 Skill 仍会输出，随后以 `skill_not_found` 错误失败，并列出 `missing` 和
`loaded` 的名称。

Avibe 会直接发现现有 Skill，无需迁移：

- 项目级：从工作目录到 Session 绑定的 Avibe 项目根目录逐层查找 `.agents/skills`、
  `.codex/skills`、`.claude/skills` 和 `.opencode/skills`。该边界可以位于嵌套 Git
  仓库之上；未绑定项目的独立命令则以遇到的第一个 Git 根目录为边界；
- 全局：查找 `~/.agents/skills`、Codex 与 Claude 配置的 Skill 目录，以及
  `XDG_CONFIG_HOME` 下的 OpenCode 目录；
- Avibe 内置 Skill，以及 Codex 自带的 system Skill。

同名冲突时，内置 Skill 优先，其次是项目级，再次是全局。项目内更近的目录优先；
同一层级依次为 `.agents`、`.codex`、`.claude`、`.opencode`。用户 Skill 优先于
Codex 自带的默认项。Claude 插件中的 Skill 不属于静态目录。新的全局 Skill 默认安装到
`~/.agents/skills/<name>`，项目级 Skill
安装到 `<project>/.agents/skills/<name>`。

每次命令都会从磁盘重新解析，每个由 Avibe 发起的新 Turn 也会重新生成 Catalog。
因此新增、修改或删除 Skill 后，已有 Session 无需重启 Avibe，也无需新建 Session；
历史对话内容不会被重写。

#### 本地 Skill 统计与隐私

Skill 统计**默认开启**；升级后，若配置中没有
`runtime.skill_observability_enabled`，也会采用开启状态。Avibe 在本地记录
Catalog 提供情况及加载结果，用于未来的 Harness 优化；提供或加载过某个 Skill，
并不能证明 Agent 遵循了它，也不能证明任务已经完成。

记录包括 Skill 名称（含私有项目及全局 Skill）、来源类别、SHA-256 身份/描述/
内容版本哈希、时间戳、加载结果、耗时、正文的字节数，以及可确认的 Session/
项目/Turn、backend/platform 和 Avibe 版本关联。名称与 Session 关联仍可识别，
哈希并不使这些统计成为匿名数据；无法确认的执行归属保持未知。统计保存在本地
SQLite 数据库的 `agent_events` 与 `skill_usage_daily` 中，没有统计面板，也不
上传云端。此功能不会额外保存 Skill 正文、描述、prompt、凭据或明文文件路径。
正常向已配置的 Agent/model 提供所加载 Skill 的行为不变。

若要停止新增记录，请在当前 Avibe 状态根目录下的 `config/config.json` 中，将
`runtime.skill_observability_enabled` 设为 JSON 布尔值 `false`。显式配置了
`AVIBE_HOME` 时，根目录为 `$AVIBE_HOME`；否则为 `$HOME/.avibe`（新目录不存在
时兼容旧的 `$HOME/.vibe_remote`）。请把下面的字段合并到现有配置中，不要用
这个片段覆盖整个文件：

```json
{
  "runtime": {
    "skill_observability_enabled": false
  }
}
```

记录器每次写入前都会重新读取此设置，无需重启。关闭统计不影响 Skill 发现或
加载，也不会删除已有历史。若希望既不再记录，也不保留历史，请先关闭，再清除。

### `vibe data query`

对本地 SQLite 状态数据库执行一条受保护的只读 SQL 语句。结果默认每页 20 行，最多 100 行。

```bash
vibe data query --sql "select group_concat(name, ', ') as tables from pragma_table_list where schema = 'main'"
vibe data query --sql "select group_concat(name, ', ') as columns from pragma_table_info('run_definitions')"
vibe data query --sql "PRAGMA table_info(run_definitions)" --limit 100   # 含类型与主键
vibe data query --sql "select id, status, created_at from agent_runs order by created_at desc"
vibe data query --sql-file query.sql --limit 50
```

连接是只读的：写入、schema 变更、`ATTACH` 和事务都会被拒绝。schema PRAGMA
`table_list`、`table_info`、`table_xinfo`、`index_list`、`index_info` 和
`foreign_key_list` 可以作为语句执行，也可以作为 `pragma_*` 表值函数使用；其他
PRAGMA 一律拒绝。`no such table` 或 `no such column` 错误会带上 `hint`，给出最接近的
名称以及表清单，或所引用表的列。Task 和 Watch 都是 `run_definitions` 的行
（`definition_type` 为 `scheduled` 或 `watch`）。

### `vibe data skill-usage`

仅限实例所有者使用的本地维护命令，返回 JSON 诊断信息，不是 Skill 热度排名。
通过 `vibe data query` 读取这些统计也受相同的所有者权限限制。

```bash
vibe data skill-usage --json
vibe data skill-usage --clear --yes --json
```

第一条命令返回 `enabled`、原始事件及每日统计行数、保留窗口、首次记录标记和
`cleared_through`。关闭后应确认 `enabled` 为 `false`。清除命令必须同时提供
`--clear` 和 `--yes`，仅删除 Skill 统计，保留对话及其他事件类型，并返回删除
行数。清除水位会拒绝延迟到达的清除前记录。清除不会关闭统计：设置仍开启时，
后续活动可以立即产生新记录。

原始 Skill 事件的保留窗口为 90 天；每日统计保留含当天在内的 365 个 UTC 日期。
每日统计仍关联 Session，并不是匿名的全局汇总。控制器运行期间会在后台分批清理，
因此过期数据可能要等维护任务追上后才被删除。这些窗口不受 Skill 统计开关或
工具 trace 保留开关影响。归档 Session 不删除其统计；物理清除 Session 会删除
与它关联的 Skill 事件及每日统计。

清除与保留清理都是逻辑删除，不代表安全擦除或保证释放磁盘空间；旧数据可能
仍存在于 SQLite WAL、空闲页或备份中。恢复数据库备份也会恢复其中的历史统计
及清除水位，必要时应在恢复后再次清除。此功能不会扫描或回填历史对话。

### `vibe doctor`

运行配置诊断检查。

```bash
vibe doctor
vibe doctor --json
```

显式运行安全的一期修复：

```bash
vibe doctor repair --dry-run
vibe doctor repair home-migration --yes
vibe doctor repair duplicate-service-processes --yes
vibe doctor repair stale-install-runtime --yes
vibe doctor repair stable-launchers --yes
vibe doctor repair stale-restart-state --yes
vibe doctor repair askill --yes
vibe doctor repair avault --yes
vibe doctor repair git-runtime --yes
vibe doctor repair model-hub-engine --yes
vibe doctor repair show-runtime --yes
vibe doctor repair tmux --yes
```

**检查内容：**
- 配置文件有效性
- Slack token 配置
- Agent CLI 可用性（Claude Code、OpenCode、Codex）
- runtime home 迁移状态
- runtime 进程、安装来源和重启元数据状态
- 稳定 `vibe` 启动器是否选中了不同的已安装版本（失败时列出两边的路径和版本；`stable-launchers` 会把它们都指向最新版本；两个安装同为最后激活时不做任何改动）
- 正在运行的服务是否比最近一次激活的安装更旧（警告）
- 通过统一依赖诊断组检查 askill、avault、Git Runtime、Model Hub 引擎（CPA）、Show Runtime、tmux 和 Node.js
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

这些子命令加 `--json` 时输出 JSON 信封。

### `vibe screenshot`

截取本机桌面并保存为 PNG 文件。

```bash
vibe screenshot
vibe screenshot --output /tmp/screen.png
vibe screenshot --json
```

**行为：**
- 默认保存到 `~/.vibe_remote/screenshots/`
- 默认输出保存路径；加 `--json` 时输出 JSON 信封
- 只作为 CLI 层能力存在；不新增 IM 命令、bot 按钮，也不注入 Agent prompt

### `vibe session`

列出、查看、更新并操作 Agent 会话。`list` 与 `get` 是只读视图；`update` 修改标题、可见性或 scope。已归档会话视为软删除，任何情况下都不会被列出。

```bash
vibe session list                       # 未归档会话，默认每页 20 条，按最近活跃倒序
vibe session list --type slack          # 按平台过滤（avibe = Web/Workbench）
vibe session list --page 2 --limit 50   # 第 2 页，每页 50 条（最多 100）
vibe session get                        # 在 Avibe Agent shell 内查看调用方 Session
vibe session get --session-id sesk8m4q2p7x                   # 其他会话的完整明细
vibe session update --title 'Release review'                 # 在 Avibe Agent shell 内
vibe session update --session-id sesk8m4q2p7x --title ''     # 清空标题
vibe session update --session-id sesk8m4q2p7x --hidden       # 只在后台；--visible 恢复显示
vibe session queue list --session-id sesk8m4q2p7x            # 按 FIFO 顺序列出排队消息
vibe session queue remove msg_queued123 --session-id sesk8m4q2p7x
vibe session send-now --session-id sesk8m4q2p7x              # 提升精确的队头
```

除 `list` 外，每个命令都作用于一个 Session：`--session-id`，或在 Avibe Agent shell
内默认的调用方 Session（见 [语法](#语法)）。`--type` 取平台 id：`avibe`（Web/Workbench）、`slack`、`discord`、`telegram`、`lark`、`wechat`。需要更高级的筛选——按 Agent、时间段、消息内容或跨表联查——请使用 `vibe data query`。

### `vibe runs`

列出和查看 Agent run 记录。

```bash
vibe runs list                         # 在 Avibe Agent shell 内查看调用方 Session 的 run
vibe runs list --global --definition-id <task-or-watch-id>   # 跨每次运行各自的 Session 查看全部触发
vibe runs show run_abc123
vibe runs show run_abc123 --brief      # 只看状态、时间、错误和回调字段
vibe runs show                         # 在 Avibe Agent shell 内查看调用方 Run
```

`vibe runs list` 遵循 [Session 归属的列表](#session-归属的列表) 的范围语法。
`vibe runs show` 在 Avibe 已注入 caller context 的 Agent run 内
可以省略 run id，并默认使用 `AVIBE_RUN_ID`。它输出完整记录：发给 Agent 的文本在
`message`，回复在 `result_text`；`--brief` 只输出与 `vibe runs list` 相同的精简行，
用于查看状态。

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
对应的调用方 Session；输出的 definition 中的 `session_id` 就是解析后的目标。
显式 `--session-id` 和 session creation 参数仍然优先。要投递到还没有 Session 的
IM 频道或话题，请用 `--create-session`（或 `--create-session-per-run`）配合
`--scope-id` 创建。

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

### `vibe agent run`

直接运行一个 Agent。Run 默认异步，并且不会创建持久化任务定义。只有终端需要等待完成时才使用
`--sync`。

```bash
vibe agent run --no-callback --agent release-reviewer --message 'Review the latest deployment result.'
vibe agent run --sync --agent release-reviewer --message 'Review the latest deployment result and print it here.'
vibe agent run --no-callback --session-id sesk8m4q2p7x --message 'The export finished. Share the summary.'
vibe agent run --session-id sesk8m4q2p7x --queue --message 'Do this after the current turn.'
vibe agent run --no-callback --fork-session sesk8m4q2p7x --message 'Explore this alternate fix from the current context.'
vibe agent run --session-id sesworker123 --callback-session-id sescaller456 --message 'Run the delegated investigation.'
vibe agent run --no-callback --scope-id slack::channel::C999 --visible --agent release-reviewer --message 'Post the deployment summary.'
vibe agent run --close-after --no-callback --agent release-reviewer --message 'Run a disposable batch review.'
```

没有 `--session-id` 也没有 fork 参数时，这次运行会为 `--agent` 新建一个后台
Session；`--same-scope` 或 `--scope-id` 决定它的位置，`--visible` 让它面向用户。

指定现有 `--session-id` 时，默认是 P1：新消息会 steering 进活动 native Turn，
Session 空闲时立即启动；只有明确拒绝后才回退到 P3。它不会打断活动 Turn，也不会
提升更早的排队消息。`--queue` 则把新 Run 放进活动 Turn 之后的 P3 队列。
`vibe session send-now` 是无内容 P1 操作，只提升现有的精确 FIFO 队头，不新增消息。
过期队头会被拒绝，而不会改为提升下一条；两个命令都不会调用 Stop。

当一个新 Agent Session 需要从现有 Session 的 native backend 上下文分叉，而不是空白开始时，
使用 `--fork-session <session-id>`。新 Session 会保持源 Session 的 backend。
只有 backend 不变时，才可以通过 `--agent`、`--model`、`--reasoning-effort`
覆盖 fork 后 Session 的 Agent、模型或推理强度；跨 backend fork 会被拒绝。
不要把 `--fork-session` 和 `--session-id` 混用。

`--model` 和 `--reasoning-effort` 可以为这次运行创建的任何新 Session（空白新
Session 或 fork）设置模型，该 Session 之后的轮次会沿用它。
它们不能与 `--session-id` 一起使用；已有 Session 的模型在 Web UI 的 Session 模型
选择器中修改。新 Session 实际运行的组合（覆盖值加上它原本沿用的默认值）会按 Agent
的模型目录检查：Vibey 的 Model Hub 菜单不提供的模型或推理强度，或菜单无法加载时，
会被拒绝；claude 和 codex 的未知值只给出警告，因为它们内置的目录可能落后于新版本。
`vibe agent models <agent>` 会列出可用的值。

命令输出一个 `run` 对象：它的 `id`、`status`、`run_type`、`agent_name`，以及 Avibe
为它解析出的 `session_id`、`scope_id`、`visibility` 和 `callback_session_id`。
`--sync` 会用结束后的 run 记录替换它。只有当 run 落在调用方自己的 Session、
结果已直接回到那里因而不再回调时，才会附带 `callback_notice`。

异步 run 需要明确 callback 策略，除非命令运行在 Avibe 已注入 caller context
的 Agent 环境内。当最终结果文本需要回到调用方 Session 时，使用
`--callback-session-id`；当你有意不自动回调、后续会通过 `vibe runs show`
或 runs 列表/轮询查看结果时，使用 `--no-callback`。Agent 内部发起的 Harness
调用会默认把 callback 指向当前调用方 Session。这个 callback 与普通投递相互独立：
即使目标 run 已经把结果发到了自己的 IM scope，调用方 Session 仍然会收到结果并触发一次
跟进 Agent 消息。system、tool call、assistant 中间过程消息不会包含在 callback 里。

对于新建或 fork 的 Session，如果希望 Run 结束后释放 Agent runtime，同时保留
Session、transcript 和 Run 记录，可以使用 `--close-after`。它不能和
`--session-id` 一起使用，因为现有 Session 自己管理 runtime 生命周期。

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

`vibe task list` 和 `vibe watch list` 默认读取调用方 Session（见
[Session 归属的列表](#session-归属的列表)），每页返回 20 条定义；还有下一页时，
响应会包含 `pagination.next_command`。已启用的定义排在前面，每组内按最近活动时间倒序。
`--q <text>` 搜索与 Workbench 搜索相同的文本字段（id、名称、Agent、Session、message
和命令），并会带进 `next_command`。默认隐藏成功结束的一次性定义；使用
`--include-finished` 分页查看历史。列表输出始终有上限，不提供无分页的 `--all` 模式。
task 和 watch 命令用 `definition` 表示单条记录、用 `definitions` 表示列表，
不会再通过命令专属别名重复输出同一份记录。

list 和 show 与 Workbench 读取同一份 Harness 投影：`lifecycle_state` 就是 lifecycle；
已结束的定义带 `lifecycle_detail` 和 `lifecycle_finished_at`，仍在进行的定义带
`next_run_at`、`waiting_since` 或 `running_since`。列表行用 `message_preview` 或
`command_preview` 标识未命名的定义，并携带 `last_error` 和非零的 `last_exit_code`。
列表行和 `show` 只有当结论不是 `healthy` 时才输出 `health`（watch 还有
`processing_health`），失败计数只在非零时输出。
只有已启用 watch 的已观测 waiter 退出时，watch 行才会附带 `process_alive: false`；
`vibe watch show` 输出观测到的 `process_alive` 和 `runtime`，从未观测到 waiter 时两者都省略。
task 和 watch 的 add、update、pause、resume 输出与 `show` 相同的 `definition`，
有警告时再附带 `warnings`。

`vibe task show` 和 `vibe watch show` 也能读取已删除的 definition：返回同一份投影，
其中 `lifecycle_state: "removed"`，并带有 `removed_at`。列表从不包含已删除的
definition。对已删除的 definition 执行 `pause`、`resume`、`update`、`run` 或
`remove` 会以 `task_removed` / `watch_removed` 失败，`details.removed_at` 给出删除
时间；`task_not_found` / `watch_not_found` 表示该 id 从未存在。

waiter 命令放在 `--` 后面；或者通过 `--shell` 传入一整段 shell 字符串。
完整参数请看 `vibe watch add --help`，包括 `--timeout`、`--lifetime-timeout`、
`--forever`、`--retry-exit-code`、`--retry-delay`、`--name` 和 session creation 参数。
watch 与 `vibe task` 共用 `--session-id`、`--create-session`、
`--create-session-per-run`、`--same-scope` 和 `--scope-id` 语义；`vibe agent run`
在没有 `--session-id` 也没有 fork 时总会新建 Session。需要可管理、可暂停、可查看的
后台等待任务时，优先使用 `vibe watch`，不要随手起 `nohup`。
`--timeout` 默认是 21600 秒；显式传入 `--timeout 0` 会关闭单次 cycle 的
超时限制，任何正数值都会原样持久化。waiter 以 `--retry-exit-code` 中的退出码退出
表示“仍在等待”：watch 把它记录为 `last_exit_code`，`last_error` 只留给真正的失败。

### `vibe version`

显示已安装的版本。

```bash
vibe version
vibe version --json
```

### `vibe check-update`

检查是否有新版本可用。

```bash
vibe check-update
vibe check-update --json
```

### `vibe upgrade`

升级到最新版本。

```bash
vibe upgrade
```

如果 Avibe 已在运行，该命令会安排一次受控重启，让服务和 Web UI 切换到升级后的代码。
如果 Avibe 原本是停止状态，则保持停止，下次启动时使用新版本。

激活新版本时，所有已经选中 Avibe 安装的稳定 `vibe` 启动器会一起切换到新版本，
因此任何 `PATH` 查找都不会留在旧版本上。指向其他位置的启动器永远不会被修改。
参见 [Upgrading](UPGRADING.md#stable-launchers)。

当 Avibe 运行在 uv 管理的 CPython 上时，升级会先用
`uv python install --upgrade --no-bin` 把这个 Python 小版本更新到最新补丁版本，
让 CPython 和 OpenSSL 的安全修复随 Avibe 版本一起到达。它不会改变小版本，也不会在
`PATH` 中添加 `python3.x` 命令。与 `uv python upgrade` 相同，其他跟随该小版本托管
Python 的 uv 环境也会一起使用新补丁。uv 只知道在它自身版本发布之前的补丁；uv 低于
0.9.10、离线或下载失败时，升级会继续使用已安装的补丁，并在日志中说明原因。运行
`uv self update` 即可获得更新的补丁。

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
| `~/.vibe_remote/runtime/opencode/generations/` | 每个运行中的 OpenCode 服务器代各一条记录 |

## 另请参阅

- [Slack 配置指南](SLACK_SETUP_ZH.md)
- [Telegram 配置指南](TELEGRAM_SETUP_ZH.md)
- [Codex 配置指南](CODEX_SETUP.md)
