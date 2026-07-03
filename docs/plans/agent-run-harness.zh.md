# Agent Run Harness

## 背景

Vibe Remote 需要三个面向用户的自动化入口：

- 手动或外部触发的 Agent 执行；
- 定时执行；
- 监控条件触发执行。

这三者是不同产品入口，但应该复用同一套执行 schema、状态模型、历史记录和
管理逻辑。

旧的 `vibe hook send` 更准确地说是“排队一次 Agent Run”，不应该作为独立
产品概念长期存在。这份计划用 Agent Run harness 替代 hook。

## 产品模型

```text
Agent Run = 执行一次 Agent 工作。
Task = 时间触发器，触发后创建 Agent Run。
Watch = 条件触发器，触发后创建 Agent Run。
```

Web UI 可以是清晰的三个 tab：

- Agent Runs：由 CLI、webhook、手动操作或 API 创建的 run。
- Scheduled Tasks：基于时间的 trigger definitions。
- Watches：waiter/monitor trigger definitions。

共享的底层原语是 run record，不是 task。

## 目标

- 用 `vibe agent run` 替代 `vibe hook send`。
- Agent Run 的用户消息参数使用 `--message` / `--message-file`，不用 `--prompt`。
- 支持同步和异步 Agent Run。
- 支持在已有 `session_id` 对话里继续执行。
- 支持创建新 Session，并返回预留的 Session ID。
- 让 Agent Run、Task、Watch 都支持 `--agent <name>`，作为命令级 Vibe Agent
  selector。
- 让 task 和 watch 复用同一个 run spec 和 run history。
- 为 webhook-triggered Agent Runs 预留结构。

## CLI 设计

### `vibe agent run`

异步 Agent Run（默认）：

```bash
vibe agent run --agent release-reviewer --no-callback --message "Review this diff."
```

同步 Agent Run：

```bash
vibe agent run --agent release-reviewer --sync --message-file request.md
```

继续已有 Session：

```bash
vibe agent run --session-id sesk8m4q2p7x --message "Continue the investigation."
```

在 Scope 中创建新 Session：

```bash
vibe agent run \
  --create-session \
  --scope-id slack::channel::C123 \
  --message "Start a fresh incident triage."
```

规则：

- 用户/任务消息使用 `--message` 和 `--message-file`。
- `--prompt` / `--prompt-file` 是 deprecated compatibility inputs。如果用户
  传了它们，应拒绝命令，并明确提示改用 `--message` / `--message-file`。
- 必须且只能指定一个 message source。
- `--session-id` 通过公开 Vibe Session ID 继续对话。
- `--create-session` 会预留一个新的 Vibe Session ID。带 `--scope-id`
  时，这个 Session 会放到指定 Scope；不带 scope placement 时，direct
  Agent Run 是 private/no-delivery Session，用于 agent harness/sub-agent
  场景，必须显式传 `--agent`，并在输出里返回 `session_id` 供后续继续对话。
- `--agent <name>` 为这次 run 选择 Vibe Agent。如果不传，则从 session 或
  placement Scope 的默认配置解析 Agent。
- `--same-scope` 把新 Session 放到 caller/source Scope。
- `--scope-id <scope-id>` 把新 Session 放到指定已有 Scope。
- Run 默认排队后立即返回。
- `--sync` 表示等待完成并打印结果。
- `--async` 仍作为默认异步行为的兼容别名保留。

执行控制：

- 如果存在 scope/session，run 使用 scope/session workdir；否则使用服务默认
  workdir。
- `--wait-timeout <seconds>` 控制 `--sync` 命令最多等待多久；它不终止 run。默认不设置
  固定等待上限；如果同步 run 执行超过 30 分钟，CLI 应返回 accepted 结果并把
  run 转为 async 管理。30 分钟是系统保护阈值，不是用户可见的默认 timeout。
- `--json` 是稳定的机器可读契约。非 JSON 输出面向人类，可以更简洁。

### 废弃 `vibe hook send`

兼容窗口：

```bash
vibe hook send ... -> vibe agent run ...
```

规则：

- 暂时继续接受旧命令。
- 返回 deprecation warning。
- 新 prompt guidance 和 docs 不再教学 `vibe hook`。

### `vibe task`

`vibe task` 管理保存下来的时间触发器。它本身不直接执行工作，而是创建或管理
`definition_type=scheduled` 的 `run_definitions` 记录。

创建 recurring task：

```bash
vibe task add \
  --cron "0 9 * * *" \
  --agent release-reviewer \
  --message "Prepare the daily release review."
```

创建 one-shot managed task：

```bash
vibe task add \
  --at "2026-06-01T09:00:00+08:00" \
  --create-session \
  --scope-id slack::channel::C123 \
  --message-file request.md
```

管理 task：

```bash
vibe task list
vibe task show <task-id>
vibe task update <task-id> --cron "*/30 * * * *"
vibe task run <task-id>
vibe task pause <task-id>
vibe task resume <task-id>
vibe task remove <task-id>
```

规则：

- 必须且只能指定一个 schedule source：`--cron` 或 `--at`。
- 必须且只能指定一个 message source：`--message` 或 `--message-file`。
- `--agent <name>` 选择并保存这个 task definition 使用的 Vibe Agent。
- 如果不传 `--agent`，创建 task 时从目标 Scope/session 解析 Agent。
- `task update <id> --agent <name>` 会影响这个 task 的未来 runs；历史
  `agent_runs` 保留创建时捕获的 run spec。
- 现有 `--prompt` / `--prompt-file` 只应被识别为 deprecated 参数，然后返回
  错误提示，要求改用 `--message` / `--message-file`。
- `--create-session-per-run` 适用于 recurring tasks；用于 `--at` one-shot task
  时应拒绝。
- `task run <id>` 根据保存的定义立即创建一条 `agent_runs` 记录；它不改变
  schedule。
- `pause` 禁用后续 schedule firing，但不删除历史。
- `remove` soft-delete 这个 definition。

### `vibe watch`

`vibe watch` 管理保存下来的条件触发器。watch 运行一个 waiter command，观察它的
终态，并在有内容需要报告时创建 Agent Run。

创建 one-shot watch：

```bash
vibe watch add \
  --agent release-reviewer \
  --message "The export finished. Summarize the result." \
  -- python3 scripts/wait_for_export.py
```

创建 long-running watch：

```bash
vibe watch add \
  --forever \
  --retry-exit-code 75 \
  --retry-delay 60 \
  --create-session-per-run \
  --scope-id slack::channel::C123 \
  --message "A CI event finished. Review the waiter output." \
  -- python3 scripts/wait_for_ci.py
```

管理 watch：

```bash
vibe watch list
vibe watch show <watch-id>
vibe watch pause <watch-id>
vibe watch resume <watch-id>
vibe watch remove <watch-id>
```

规则：

- Watch definitions 存在 `run_definitions`，`definition_type=watch`。
- waiter command 是 trigger configuration 的一部分。
- `--agent <name>` 选择并保存这个 watch definition 使用的 Vibe Agent。
- 如果不传 `--agent`，创建 watch 时从目标 Scope/session 解析 Agent。
- `--message` / `--message-file` 是 waiter 到达可报告状态后创建 Agent Run 的
  instruction template。
- 现有 `--prefix` 可以作为兼容 alias 保留，用于在 waiter stdout 前追加指令；
  目标 schema 应把它存成 `message.text` 加结构化 waiter output。
- `--forever` watch 会持续运行，直到 pause、remove、lifetime timeout，或遇到
  non-retryable terminal failure。
- Watch runtime process state 可以继续使用 `run_type=watch_runtime`，但用户可见
  follow-up execution 应使用 `run_type=watch`。

### One-Off Agent Run 和 One-Off Task 的区别

`vibe agent run` 和 `vibe task add --at ...` 都可以只执行一次，但它们
对应不同产品需求：

- Agent Run：现在执行；不保存 definition；通过 run history 管理。
- One-shot Task：稍后执行；保存 definition；可以 list、show、update、pause、
  resume、remove，也可以在计划时间前手动 run。

## RunSpec

所有 immediate runs、scheduled tasks、watches 和未来 webhook triggers 应共享
一个 run spec。

```text
RunSpec
  agent_target:
    mode: named_agent | session
    agent_name
    session_id
  session_target:
    mode: none | existing | create_once | create_per_run
    session_id
    scope_id
  delivery_target:
    mode: none | scope
    scope_id
  message:
    text
    payload_json
  execution:
    mode: sync | async
```

说明：

- `message.text` 是用户/任务消息，不是 system prompt。
- Agent system prompt 来自 Vibe Agent catalog。
- `payload_json` 为 webhook/API structured input 预留。
- `create_per_run` 属于 trigger definitions，不属于一次性 direct run。

## Session 和 Delivery Targeting

后台命令必须把三种身份分开：

- Agent Session：要继续或创建的 Vibe Session。
- Delivery Scope：Agent 输出最终投递到的 IM Scope。
- Agent Definition：提供 backend/model/effort/prompt 的 Vibe Agent。

### Scope ID

`--scope-id` 使用 `scopes.id`。它不是 session key。

格式示例：

```text
<platform>::<scope_type>::<native_id>
```

示例：

```text
slack::channel::C123
slack::user::U123
lark::channel::oc_...
```

不需要单独的 thread anchor 参数。如果未来 thread 成为一等 Scope，同样可以
通过 `--scope-id <scope-id>` 覆盖。

### Session Policies

继续已有 Session：

```bash
--session-id <agent-session-id>
```

创建一个可复用的新 Session：

```bash
--create-session --scope-id <scope-id>
```

每次 trigger 执行都创建一个新 Session：

```bash
--create-session-per-run --scope-id <scope-id>
```

规则：

- `--session-id`、`--create-session`、`--create-session-per-run` 互斥。
- `vibe agent run --create-session` 可以不带 scope placement，表示创建
  private/no-delivery Session，但必须显式传 `--agent`；managed task/watch
  definitions 中使用 `--create-session` 或 `--create-session-per-run` 时必须带
  `--same-scope` 或 `--scope-id`。
- `--create-session` 立即预留一个 Vibe Session ID。runtime 第一次执行时绑定
  backend-native 状态。
- `--create-session-per-run` 在 `run_definitions` 上保存策略；每次执行都创建
  一个新的 Vibe Session ID，并记录到对应 `agent_runs` 行里。
- one-shot `task add --at` 使用 `--create-session-per-run` 时应拒绝，因为它和
  `--create-session` 生命周期等价，但语义更不清楚。
- 立即执行的 `vibe agent run` 只需要 `--create-session`；per-run 对单次 direct
  run 没有独立含义。

### 参数互斥矩阵

| 参数组合 | `vibe agent run` | `vibe task add/update` | `vibe watch add` | 规则 |
| --- | --- | --- | --- | --- |
| `--message` + `--message-file` | 拒绝 | 拒绝 | 拒绝 | 每次定义或执行只能有一个 message source。 |
| `--prompt` / `--prompt-file` + 任意 message 参数 | 拒绝 | 拒绝 | 拒绝 | 旧参数只用于 deprecated 错误提示。 |
| `--session-id` + `--create-session` | 拒绝 | 拒绝 | 拒绝 | 一个 run 只能有一种 Session policy。 |
| `--session-id` + `--create-session-per-run` | 拒绝 | 拒绝 | 拒绝 | `existing` 和 `create_per_run` 生命周期冲突。 |
| `--create-session` + `--create-session-per-run` | 拒绝 | 拒绝 | 拒绝 | `create_once` 和 `create_per_run` 生命周期冲突。 |
| `--create-session` without scope placement | 仅允许同时传 `--agent` | 拒绝 | 拒绝 | Direct Agent Run 可创建 private/no-delivery Session，但 Agent 必须显式；managed definitions 创建 Session 需要 Scope。 |
| `--create-session-per-run` without scope placement | 不适用 | 拒绝 | 拒绝 | 每次创建新 Session 都需要 Scope。 |
| `--create-session-per-run` + `task add --at` | 不适用 | 拒绝 | 不适用 | one-shot task 只有一次执行，使用 `--create-session`。 |
| `--agent` + `--session-id` | backend 一致时允许 | backend 一致时允许 | backend 一致时允许 | `--agent` 只覆盖该次 run/definition，不修改 Session；若 Agent backend 和 Session backend 不同则拒绝。 |
| 默认 async + `--wait-timeout` | 拒绝 | 不适用 | 不适用 | `--wait-timeout` 只控制 `--sync` CLI 等待，不控制 async run 生命周期。 |
| `--agent` + `--scope-id` | 允许 | 允许 | 允许 | `--agent` 覆盖 Scope 默认 Agent；`--scope-id` 决定 placement。 |

### Delivery Policies

创建需要放进已有 Scope 的新 Session 时，使用 `--same-scope` 或
`--scope-id <scope-id>`，因为 CLI 在没有 injected caller context 时不能安全推断
IM 或 Workbench placement。Direct `vibe agent run --create-session` 不带 scope
placement 时创建 private/no-delivery Session，只返回 run/session 输出。

对于已有 Session，常规 delivery 来自 Session 保存的 Scope。新的 help 和 docs
不再教学一次性的 transport override flags。

### Runtime Target Resolution

后台命令不应接受 backend/model/effort override flags。runtime target 解析规则是：

1. 如果传了 `--agent <name>`，加载这个 Vibe Agent。
2. 否则，如果传了 `--session-id`，使用该 Session 当前的 Agent identity。
3. 否则，把 `--same-scope` 或 `--scope-id` 解析为 Scope，并读取 Scope 选择的
   Vibe Agent。
4. 如果 Scope 没有选择 Agent，则使用系统默认 Agent。
5. 如果同时传了 `--agent` 和 `--session-id`，校验 Agent backend 和 Session
   backend 一致；不一致时拒绝，因为跨 backend 无法保持上下文连续。
6. 使用解析出的 Agent backend/model/effort/system prompt。
7. 优先使用 Scope/session workdir；没有可用 workdir 时使用服务默认 workdir。

所以 `--agent` 是命令级的 Scope default Agent override，不是
backend/model/effort override；和 `--session-id` 组合时也只影响本次 run，不修改
Session 后续默认 Agent。

## 存储模型

由现有两张 background 表迁移并重命名：

- `background_tasks` -> `run_definitions`：reusable managed trigger 的定义表。
- `background_runs` -> `agent_runs`：每一次实际执行的运行表。

### `run_definitions`：定义

`run_definitions` 保存未来可能创建 runs 的定义，是 task、watch 和未来 webhook
的统一 definition 表。

建议的 `definition_type`：

- `scheduled`：cron 或 one-shot scheduled task；
- `watch`：managed waiter，在终态条件达成后产生 follow-up run；
- `webhook`：未来的外部触发定义。

立即执行的 `vibe agent run` 不创建 `run_definitions` 记录，它直接创建
`agent_runs` 记录。

核心 definition 语义应作为一等列保存。字段明细：

| Field | Status | Definition | Design intent |
| --- | --- | --- | --- |
| `id` | 现有 | Definition ID。 | 作为 task/watch/webhook definition 的稳定管理句柄。 |
| `definition_type` | 现有字段改名，扩展取值 | Definition 类型：`scheduled`、`watch`、未来 `webhook`。 | 由 `task_type` 改名，避免把 watch/webhook 都称为 task。 |
| `name` | 现有 | 用户可见名称。 | 让 CLI/Web UI 可以展示可读名称，而不是只展示 ID。 |
| `agent_name` | 新增 | 这个 definition 创建 runs 时使用的 Vibe Agent 名称。 | 让 task/watch 明确选择 Agent；不传 `--agent` 时写入从 Scope/session 解析出的默认 Agent。 |
| `session_policy` | 新增 | `existing`、`create_once`、`create_per_run`。 | 把“继续已有 Session / 复用新 Session / 每次新 Session”的生命周期语义落成可查询字段。 |
| `session_id` | 现有 | `existing` 或 `create_once` policy 下的 Vibe Session ID。 | 保存已有或预留的 Session ID，后续执行能继续同一个 Vibe Session。 |
| `legacy_session_key` | 现有，兼容 | 旧 `session_key` target。 | 只服务旧记录和旧命令迁移；新写入优先使用 `session_id` / `scope_id`。 |
| `scope_id` | 目标字段 | Session placement Scope ID。 | 让 placement 和 Session 身份分离，并显式表达 Scope 选择。 |
| legacy delivery fields | 现有，兼容 | 旧 delivery override 列。 | 只保留旧记录和隐藏兼容输入；不要暴露在 help、docs、prompt 或新示例里。 |
| `prompt` | 现有，兼容 | 旧消息模板字段。 | 迁移期读写兼容；目标 schema 用 `message` 表达同一语义。 |
| `message` | 新增 | 保存的 Agent message template。 | 配合 CLI `--message`，避免和 Agent `system_prompt` 混淆。 |
| `message_payload_json` | 新增 | 可选结构化消息 payload。 | 为 webhook/API 触发保留结构化输入，不把文本 message 和 payload 混在一起。 |
| `schedule_type` | 现有 | `cron` 或 `at`。 | 区分 recurring task 和 one-shot task。 |
| `cron` | 现有 | Cron 表达式。 | 存储 recurring schedule。 |
| `run_at` | 现有 | one-shot scheduled time。 | 存储单次计划执行时间。 |
| `timezone` | 现有 | schedule timezone。 | 保证 cron/at 的解释可复现，不依赖 runtime 当前时区。 |
| `command_json` | 现有 | watch waiter argv。 | 存储 structured command，避免 shell quoting 歧义。 |
| `shell_command` | 现有 | watch waiter shell command。 | 保留 shell 模式，服务复杂脚本和兼容旧命令。 |
| `prefix` | 现有，兼容 | 旧 watch follow-up instruction prefix。 | 迁移期兼容 `--prefix`；目标语义应合并到 `message`。 |
| `cwd` | 现有 | watch/runtime working directory。 | 让后台执行不依赖服务进程启动目录。 |
| `mode` | 现有 | watch mode，例如 `once` 或 `forever`。 | 表达 watch 生命周期。 |
| `timeout_seconds` | 现有 | per-run 或 per-cycle timeout。 | 防止单次执行无限挂起。 |
| `lifetime_timeout_seconds` | 现有 | watch overall lifetime timeout。 | 给 long-running watch 设置总生命周期边界。 |
| `retry_exit_codes_json` | 现有 | 可重试的 waiter exit codes。 | 让 watch 能区分“继续等待”和“终态失败”。 |
| `retry_delay_seconds` | 现有 | retryable waiter 的重试延迟。 | 控制 forever watch 的重试节奏。 |
| `enabled` | 现有 | Definition 是否允许创建未来 runs。 | 支持 pause/resume，不删除历史。 |
| `deleted_at` | 现有 | soft-delete timestamp。 | 支持 remove 后隐藏定义，同时保留历史 runs。 |
| `created_at` | 现有 | 创建时间。 | 审计和排序。 |
| `updated_at` | 现有 | 最近更新时间。 | 管理 UI、同步和变更排查。 |
| `last_started_at` | 现有 | 最近一次执行开始时间。 | 列表页快速展示执行状态。 |
| `last_finished_at` | 现有 | 最近一次执行结束时间。 | 列表页快速判断是否完成。 |
| `last_event_at` | 现有 | watch 最近检测到事件的时间。 | watch 列表摘要。 |
| `last_run_at` | 现有 | scheduled task 最近触发时间。 | task 列表摘要。 |
| `last_error` | 现有 | 最近错误摘要。 | 列表页直接暴露可处理问题。 |
| `last_exit_code` | 现有 | 最近 waiter/process exit code。 | watch/debug 摘要。 |
| `last_run_id` | 新增 | 最近创建的用户可见 run ID。 | 让 definition 和 run history 快速互跳。 |
| `metadata_json` | 现有，扩展 | 非核心扩展信息。 | 只放暂不需要查询/索引的 backend-specific、UI hint、实验字段。 |

`metadata_json` 只保留给非核心扩展信息：

- backend-specific import metadata；
- 不影响执行的 UI display hints；
- 暂时没有查询需求的 webhook auth/source metadata；
- 实验字段，后续稳定后再提升为列。

实现可以渐进迁移：当前 `prompt` 列先作为 `message` 的兼容 alias 保留，之后通过
schema migration 改名或复制成 `message` 列。

Definition 表的迁移优先级：

1. 先增加 `agent_name`、`session_policy`、`message`、`message_payload_json`、
   `last_run_id`。
2. 写入新 definition 时同时填充新列和必要兼容列，例如 `prompt`。
3. 读取旧 definition 时，如果 `message` 为空，则从 `prompt` 派生。
4. 兼容窗口结束后，再考虑是否删除或停止展示 `prompt`、`prefix` 等旧语义字段。

### `agent_runs`：执行

`agent_runs` 保存每一次实际执行：

- 立即执行的 `vibe agent run`；
- 异步的 `vibe agent run`；
- scheduled task 触发；
- watch 进入终态后的 follow-up；
- 未来 webhook invocation。

建议的 `run_type`：

- `agent_run`：直接 agent invocation，通常 `definition_id = null`；
- `scheduled`：由 scheduled `run_definitions` 记录创建；
- `watch`：由 watch `run_definitions` 记录创建；
- `webhook`：由 webhook `run_definitions` 记录创建；
- `watch_runtime`：如果仍然需要，可以保留给历史/runtime waiter bookkeeping。

目标 domain status：

- `queued`；
- `running`；
- `succeeded`；
- `failed`；
- `canceled`。

存储层初期可以把这些映射到现有的 `pending`、`processing`、`completed` 等值；
公开输出 schema 应该使用上面的 domain status。

字段明细：

| Field | Status | Definition | Design intent |
| --- | --- | --- | --- |
| `id` | 现有 | Run ID。 | 每次实际执行的稳定句柄，用于 show/list/cancel/history。 |
| `definition_id` | 现有字段改名 | 可选来源 `run_definitions.id`。 | 由 `task_id` 改名，表达来源是通用 definition，不只是 task。 |
| `run_type` | 现有，扩展取值 | `agent_run`、`scheduled`、`watch`、`webhook` 或 runtime bookkeeping type。 | 区分 direct run、task run、watch follow-up、webhook invocation。 |
| `status` | 现有，规范化取值 | `queued`、`running`、`succeeded`、`failed`、`canceled`。 | 给 CLI/Web UI/agent harness 一个统一状态机。 |
| `source_kind` | 新增 | `cli`、`api`、`scheduler`、`watch` 或 `webhook`。 | 记录谁创建了这次 run，方便审计和筛选。 |
| `source_actor` | 新增 | 可选 actor/user/system identifier。 | 区分人、agent、scheduler、外部系统触发。 |
| `parent_run_id` | 新增 | 父 run ID。 | 支持 agent harness/sub-agent 调用链和递归保护。 |
| `agent_name` | 新增 | 本次 run 捕获的 Vibe Agent 名称。 | 历史 run 可审计；Agent 定义变化后仍知道当时选择的是谁。 |
| `agent_id` | 新增 | 可选 Agent ID snapshot。 | 避免未来 Agent rename/显示名变化影响历史关联；如果 name 永久不可变，可为空。 |
| `agent_backend` | 新增 | backend snapshot。 | Debug 和历史审计，不需要 join 当前 Agent 才知道当时跑在哪个 backend。 |
| `model` | 新增 | model snapshot。 | 记录执行时模型，避免 Agent 后续修改影响历史解释。 |
| `reasoning_effort` | 新增 | effort snapshot。 | 记录执行时推理强度。 |
| `session_policy` | 新增 | 本次 run 使用的 session resolution policy。 | 解释 `session_id` 是复用、预留还是 per-run 创建出来的。 |
| `session_id` | 现有 | 本次 run 实际使用的 Vibe Session ID。 | 支持继续对话、按 session 查历史。 |
| `legacy_session_key` | 现有，兼容 | 旧导入 runs 的兼容 target。 | 只用于迁移和兼容展示。 |
| `scope_id` | 目标字段 | Scope placement snapshot。 | 记录 placement，支持按 Scope 查 runs。 |
| legacy delivery fields | 现有，兼容 | 旧 delivery override snapshot。 | 只保留旧 run history；新的 user-facing contract 应优先使用 Scope placement 和 Session callback 字段。 |
| `prompt` | 现有，兼容 | 旧消息字段。 | 迁移期兼容旧 run；目标 schema 用 `message`。 |
| `message` | 新增 | 发送给 Agent 的实际消息。 | 与 `--message` 对齐，并和 Agent system prompt 分离。 |
| `message_payload_json` | 新增 | 可选结构化 payload。 | 支持 webhook/API 传结构化输入。 |
| `result_text` | 新增 | 最终用户可见结果。 | 支持 sync `agent run` 回显、run show、Web UI 摘要。 |
| `result_payload_json` | 新增 | 可选结构化结果。 | 为 API/webhook/harness 返回机器可读结果。 |
| `message_ids_json` | 新增 | 本次 run 发送出的 IM message IDs。 | 支持投递审计、后续 thread 关联和 UI 跳转。 |
| `cancel_requested` | 新增 | 用户是否请求过取消。 | 支持 best-effort cancel 后保留真实执行终态。 |
| `cancel_requested_at` | 新增 | 取消请求时间。 | 审计取消请求和 worker 响应延迟。 |
| `pid` | 现有 | runtime/watch process id。 | 支持 watch runtime 管理和诊断。 |
| `exit_code` | 现有 | process exit code。 | 诊断 watch/waiter 或 backend process 失败。 |
| `error` | 现有 | 错误摘要。 | CLI/Web UI 快速展示失败原因。 |
| `stdout` | 现有 | bounded stdout。 | 保留 waiter/backend 输出摘要。 |
| `stderr` | 现有 | bounded stderr。 | 保留诊断输出。 |
| `created_at` | 现有 | run 创建时间。 | 队列和历史排序。 |
| `started_at` | 现有 | run 开始执行时间。 | 计算排队等待和执行耗时。 |
| `completed_at` | 现有 | run 完成时间。 | 计算执行耗时和判断终态。 |
| `updated_at` | 现有 | 最近更新时间。 | worker polling、stale running 检测。 |
| `metadata_json` | 现有，扩展 | 非核心扩展信息。 | 只放非查询、backend-specific 或实验数据。 |

scheduled/watch/webhook 创建 run 时，这些列应从 definition 复制快照。这样即使
后续 task、watch 或 Agent 定义变化，历史 run 仍然可审计。

`metadata_json` 只作为非查询、backend-specific 或实验数据的扩展字段。核心 run
语义不能放在 `metadata_json` 里。

Run 表的迁移优先级：

1. 先增加 `source_kind`、`source_actor`、`parent_run_id`、Agent snapshot、
   `session_policy`、`message`、`message_payload_json`、result 和 message IDs
   相关列。
2. 新创建 run 时，从 definition 或 direct command 参数复制一等列快照。
3. 读取旧 run 时，如果 `message` 为空，则从 `prompt` 派生；如果 `result_text`
   为空，可以继续从旧 `stdout` / `error` 组合展示摘要。
4. `metadata_json` 中已经存在的历史扩展数据只做兼容读取，不再作为新 run 的
   core semantics 写入目标。

### 表职责

- `run_definitions` 负责定义生命周期和 trigger 配置。
- `agent_runs` 负责执行生命周期、历史、输出和错误。
- 一次性 agent run 是“没有保存定义的执行”。
- Webhook 可以作为新的 definition type 和 run type 加进来。
- harness/sub-agent 场景通过 run 上的 `parent_run_id` 表示父子关系。

### 索引建议

现有索引已经覆盖常见 task/watch 查询。run 表还应该支持：

- worker polling：`(status, updated_at)` 或 `(status, created_at)`；
- 按类型查历史：`(run_type, status, created_at)`；
- 按 session 查历史：`(session_id, created_at)`；
- 按定义查历史：`(definition_id, created_at)`。

迁移时应把旧 `agent_runs.task_id` 改名为 `definition_id`，或先新增
`definition_id` 并从旧列回填；数据库列统一使用 `definition_id`。

## 和 Task / Watch 的关系

### `vibe task`

`vibe task` 管理 scheduled trigger definitions。

```bash
vibe task add --cron "0 9 * * *" --agent release-reviewer --message "Daily review"
vibe task run <task-id>
vibe task pause <task-id>
```

schedule 触发时，从 task definition 的一等列复制快照，创建一条
`agent_runs` 记录。

### `vibe watch`

`vibe watch` 管理 condition trigger definitions。

```bash
vibe watch add --agent release-reviewer --message "CI finished" -- python wait.py
```

waiter 成功或进入 terminal failure 时，创建一条 `agent_runs` 记录。

### `vibe agent run`

`vibe agent run` 立即创建一条 `agent_runs` 记录。除非未来有显式 save
命令，否则它不创建 `run_definitions` definition。

## 输出契约

JSON 输出应作为 agents 和 scripts 的稳定契约。

通用规则：

- 顶层必须有 `schema_version`、`ok`、`kind`。
- `kind` 表达返回对象类型，例如 `agent_run`、`run_definition`、`agent_runs`。
- 人类可读输出可以更短，但 `--json` 输出必须稳定。

同步 Agent Run 成功：

```json
{
  "schema_version": 1,
  "ok": true,
  "kind": "agent_run",
  "run": {
    "id": "run123",
    "status": "succeeded",
    "session_id": "sesk8m4q2p7x",
    "result_text": "..."
  }
}
```

异步 Agent Run accepted，或同步执行超过 30 分钟后转 async：

```json
{
  "schema_version": 1,
  "ok": true,
  "accepted": true,
  "kind": "agent_run",
  "run": {
    "id": "run123",
    "status": "queued",
    "session_id": "sesnew12345"
  }
}
```

Task/Watch definition 创建成功：

```json
{
  "schema_version": 1,
  "ok": true,
  "kind": "run_definition",
  "definition": {
    "id": "def123",
    "definition_type": "scheduled",
    "enabled": true,
    "agent_name": "release-reviewer",
    "session_policy": "create_once",
    "session_id": "sesnew12345",
    "scope_id": "slack::channel::C123",
    "next_run_at": "2026-06-01T09:00:00+08:00"
  },
  "warnings": []
}
```

`vibe task run <id>` 成功：

```json
{
  "schema_version": 1,
  "ok": true,
  "kind": "agent_run",
  "definition": {
    "id": "def123",
    "definition_type": "scheduled"
  },
  "run": {
    "id": "run123",
    "status": "queued",
    "definition_id": "def123",
    "agent_name": "release-reviewer",
    "session_id": "sesnew12345"
  }
}
```

`vibe runs show <id>`：

```json
{
  "schema_version": 1,
  "ok": true,
  "kind": "agent_run",
  "run": {
    "id": "run123",
    "run_type": "agent_run",
    "status": "running",
    "source_kind": "cli",
    "agent_name": "release-reviewer",
    "session_id": "sesnew12345",
    "definition_id": null,
    "created_at": "2026-05-21T17:00:00Z",
    "started_at": "2026-05-21T17:00:03Z",
    "completed_at": null,
    "result_text": null,
    "error": null
  }
}
```

`vibe runs list`：

```json
{
  "schema_version": 1,
  "ok": true,
  "kind": "agent_runs",
  "runs": [
    {
      "id": "run123",
      "run_type": "agent_run",
      "status": "running",
      "agent_name": "release-reviewer",
      "session_id": "sesnew12345",
      "definition_id": null,
      "created_at": "2026-05-21T17:00:00Z"
    }
  ]
}
```

`vibe runs cancel <id>`：

```json
{
  "schema_version": 1,
  "ok": true,
  "kind": "agent_run",
  "run": {
    "id": "run123",
    "status": "canceled"
  }
}
```

取消规则：

- 对尚未开始的 run，应直接标记为 `canceled`。
- 对已经进入 backend 执行的 run，第一版使用 best-effort cancel；能中断就中断，
  不能中断则记录 cancel requested，并由 worker/backend 最终回写终态。
- 如果 cancel requested 后 backend 已经正常完成，最终状态保留真实的
  `succeeded` 或 `failed`，同时保留 `cancel_requested=true`；不要把真实成功或
  失败统一覆盖成 `canceled`。

失败契约：

- CLI/runtime 基础设施失败时，命令应非零退出，并返回 `ok: false`。
- 如果目标 Agent 已经运行，但它产生的是 Agent-level failure，那么只要命令
  本身成功完成，可以返回 run 记录；失败信息由 `run.status` / `run.error`
  表达。
- `run.status=failed` 应足够让 harness caller 分支处理，不需要解析文本。

Run 查看和管理：

```bash
vibe runs show run123
vibe runs list
vibe runs cancel run123
```

## Runtime 和递归策略

`vibe agent run` 应始终先创建 `agent_runs` 记录，然后交给 Vibe runtime 执行。

同步 `vibe agent run --sync` 也应该先创建 run 记录，然后二选一：

- 通过本地 runtime service 执行并等待完成；
- 只有当 runtime service 不可用且 backend 路径适合在 CLI 进程里安全运行时，
  才由 CLI inline claim/execute。

推荐 runtime-backed execution，因为 session binding、delivery、logging、
cancellation 和 backend environment 都应该由同一条路径处理。

同步 run 默认没有固定等待上限。若执行超过 30 分钟，CLI 返回 async accepted
响应，保留 `agent_runs` 里的运行状态，后续通过 `vibe runs show/list`
管理。30 分钟是系统保护阈值，不是用户可见的默认 timeout。`--wait-timeout`
只改变 CLI 等待多久，不表示 run 执行超时或自动停止。

Agent 可以再次调用 `vibe agent run`，把它作为 harness/sub-agent 机制，但 run
metadata 应记录 `parent_run_id`。实现前应加一个简单递归保护：

- 最大嵌套深度；
- 根据 parent chain 做 cycle detection；
- guard 阻止运行时，写入清晰的 failure status。

## Webhook 方向

未来 webhook 支持也应该走同一条 run creation 路径：

```text
external webhook -> validate source -> build RunSpec/message payload -> create agent_run
```

具体 CLI 可以之后单独设计，但 schema 应该已经为 `source.kind=webhook` 和结构化
`payload_json` 留位置。

## 规范摘要

1. `vibe hook send` 应废弃，由 `vibe agent run` 替代。
2. Agent Run 消息参数应叫 `--message` 和 `--message-file`，不叫 `--prompt`。
3. Agent Run 必须支持已有 `--session-id`。
4. 共享存储模型是 `run_definitions` 定义表 + `agent_runs` 执行表。
5. Run 查看和管理放在 `vibe runs`。
6. 同步 `vibe agent run` 默认没有固定等待上限；超过 30 分钟自动转为 async
   管理，30 分钟是系统保护阈值，`--wait-timeout` 只控制 CLI 等待时长。
7. `vibe runs cancel` 是 best-effort；如果请求取消后 run 仍正常完成，保留真实
   `succeeded` / `failed` 终态，并记录 `cancel_requested=true`。
