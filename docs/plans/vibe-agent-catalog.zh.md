# Vibe Agent Catalog

## 背景

Vibe Remote 旧版本基本把 “agent” 当作 backend-specific routing 数据处理。
旧的 scope backend route 字段现在已经废弃并被忽略。

这在每个 backend 都有不同 subagent 模型时是可用的，但不是正确的产品抽象。
Scope 正在成为 Vibe Remote 里稳定的 project/workspace 单元。每个 Scope
应该选择一个 Vibe Remote 自己拥有的 Agent，由这个 Agent 定义工作方式。

这份计划把 Vibe Agent catalog 作为一等数据结构和 CLI 能力引入。

## 目标

- 增加 Vibe-owned Agent 数据结构。
- 让 Scope 通过名称选择一个 Agent。
- 将 backend、model、effort、description、system prompt 移入 Agent 定义。
- 增加 `vibe agent` 命令，用于增删改查和导入。
- 支持从 Claude Code、Codex、OpenCode 导入已有全局 Agent。
- 停止把 backend-native subagent 当作 Scope routing 字段。
- 保持 run definition/session 设计简单：新 Session 从 Scope 配置的 Vibe
  Agent 解析运行时目标。

## 范围

这份计划覆盖 Agent catalog、Scope-to-Agent resolution 和导入流程。
backend/model/effort 由 Agent 定义持有；task、watch 和 run 命令通过名称选择
Agent。

导入的 Agent 会成为 Vibe-owned Agent 定义。它们保留 source metadata 便于追踪，
但 V1 不和原 backend 文件保持实时链接。

## 核心数据模型

### Agent

最小持久化结构：

```text
agents
  id
  name
  description
  backend
  model
  reasoning_effort
  system_prompt
  source
  source_ref
  metadata_json
  created_at
  updated_at
```

字段语义：

| 字段 | 含义 |
| --- | --- |
| `id` | 稳定内部 ID。可以是 UUID 或短 ID；不是用户选择时使用的名字。 |
| `name` | 唯一 Vibe Agent 名称，Scope 和 CLI 通过它引用 Agent。 |
| `description` | 面向人的用途说明和选择提示。 |
| `backend` | `opencode`、`claude` 或 `codex`。 |
| `model` | 这个 Agent 的 backend model override；为空表示 backend 默认值。 |
| `reasoning_effort` | 这个 Agent 的 reasoning/effort override；为空表示 backend 默认值。 |
| `system_prompt` | Vibe-owned system prompt，追加或注入到 backend runtime。 |
| `source` | `manual`、`imported_claude`、`imported_codex`、`imported_opencode` 等。 |
| `source_ref` | 导入 Agent 的原始路径、名称或 ID。 |
| `metadata_json` | 扩展数据，例如 tools、导入细节或 backend-specific hints。 |
| `created_at` | Agent 创建时间。 |
| `updated_at` | Agent 最近更新时间。 |

名称规则：

- 名称查找大小写不敏感。
- 存储 normalized name 来保证唯一性。
- 展示名可以保留用户输入大小写，但 CLI selector 应稳定且适合 shell 使用。
- 名称不应该编码 backend；backend 是字段，不是身份的一部分。
- 名称全局唯一。Scope 私有 Agent 不由 Vibe 层实现；backend-specific 私有
  subagent 行为仍然归 backend 自己管理。

### Scope Routing

Scope settings 目标形态：

```text
scope_settings
  scope_id
  agent_name
  workdir
  enabled
  require_mention
  display settings...
```

Scope routing 现在由一个 `agent_name` 引用，加上可选 model、reasoning 和
subagent override 表达。

解析规则：

1. 如果命令显式传了 `--agent <name>`，加载这个 Vibe Agent。
2. 否则解析 inbound 或 delivery `scope_id`。
3. 读取 `scope_settings.agent_name`。
4. 加载对应 Vibe Agent。
5. 使用 Agent 的 backend/model/effort/system prompt 执行这次 turn。
6. 使用 Scope 的 workdir，除非未来 Agent 模型明确接管 cwd。

如果 Scope 没有选择 Agent，且命令也没有显式传 `--agent`，则回退到系统默认
Agent。系统默认 Agent 应该是 config/state 里的显式对象，而不是长期从分散的
backend 默认值里推断出来。

### System Default Agent

Fresh install 和 migration 后应存在一个明确的系统默认 Agent：

```text
default_agent_name -> agents.name
```

建议第一版创建一个名为 `default` 的 Agent 行，并把 `default_agent_name` 指向
它。这个 Agent 的 backend/model/effort 可以从当前启用的全局 backend 默认配置
生成一次；之后运行时不再从旧 backend-specific scope 字段动态合成 Agent。

因为 Agent 的 `name` 和 `backend` 不可修改，如果用户之后想把系统默认 backend
从 Codex 换成 Claude/OpenCode，应创建另一个 Agent，然后把
`default_agent_name` 切到新的 Agent。这样默认行为仍然是一等 Agent 配置，而
不是隐藏 fallback 逻辑。

### Agent Sessions

`agent_sessions` 应保存已解析的 Vibe Agent 身份：

```text
agent_sessions
  agent_name
  agent_id
  agent_backend
  model
  reasoning_effort
  ...
```

即使 Agent 定义是源头，`agent_backend`、`model`、`reasoning_effort` 作为历史
和调试快照仍然有价值。

已有 Session 后续 turn 应读取 Agent 定义的最新版本。因为 `name` 和 `backend`
不可变，live update 可以改变 prompt、model、effort、description 和 metadata，
但不会改变一个已建立 Session 的 backend family。

## CLI 设计

### 命令族

```bash
vibe agent list
vibe agent show <name>
vibe agent create ...
vibe agent update <name> ...
vibe agent remove <name>
vibe agent import ...
```

这个命令族管理 Vibe Agent 定义。direct/manual execution 放在
`vibe agent run`，由 `agent-run-harness.md` 覆盖。

### `vibe agent list`

用途：列出可用于 Scope routing 和 Session 创建的 Vibe Agents。

建议参数：

```bash
vibe agent list
vibe agent list --backend codex
vibe agent list --json
```

输出字段：

- name；
- description preview；
- backend；
- model；
- reasoning effort；
- source；
- updated time。

### `vibe agent show`

用途：查看一个完整 Agent 定义。

```bash
vibe agent show release-reviewer
```

输出应包含完整 system prompt，除非未来需要 `--redact-prompt`。System prompt
是用户配置，不默认当作 secret。

### `vibe agent create`

用途：手动创建一个 Vibe Agent。

候选形态：

```bash
vibe agent create release-reviewer \
  --backend codex \
  --description "Reviews release diffs and deployment risk." \
  --model gpt-5.4 \
  --effort high \
  --system-prompt-file agents/release-reviewer.md
```

说明：

- `--backend` 必填。
- `--description` 推荐填写。
- `--model` 和 `--effort` 可选。
- System prompt 可以来自 `--system-prompt` 或 `--system-prompt-file`。
- 如果不提供 prompt，Agent 仍然有效，但只使用 backend 默认值和 Vibe Remote
  标准 prompt injection。

### `vibe agent update`

用途：修改 Agent 定义，不改变已选择它的 Scope。

候选形态：

```bash
vibe agent update release-reviewer --model gpt-5.5
vibe agent update release-reviewer --effort xhigh
vibe agent update release-reviewer --description "..."
vibe agent update release-reviewer --system-prompt-file agents/release-reviewer.md
```

更新规则：

- `name` 不允许修改。
- `backend` 不允许修改。
- `description`、`model`、`reasoning_effort`、`system_prompt` 和 metadata 允许修改。
- 已有 Session 会立即读取最新 Agent 定义，所以编辑会影响已创建 Session 的后续 turn。

### `vibe agent remove`

用途：删除一个 Vibe Agent 定义。

规则：

- 如果还有 Scope 引用该 Agent，则拒绝删除，除非传 `--force`。
- 使用 `--force` 时，受影响 Scope 应该迁移到默认 Agent，或置空并给出警告。
  更好的第一版实现是拒绝删除，并要求显式重分配。

### `vibe agent import`

用途：把 backend-native 全局 Agent 导入 Vibe Agent catalog。

候选命令：

```bash
vibe agent import --from claude
vibe agent import --from codex
vibe agent import --from opencode
vibe agent import --from claude --name reviewer
vibe agent import --from codex --all
vibe agent import --file reviewer.md --backend codex
```

导入规则：

- `--from` 从 backend 的正常全局 Agent 位置读取定义。
- V1 只导入 global agents，不导入 project-local agents。
- `--file <path>` 导入一个显式文件，格式是通用 markdown-with-header。
  因为仅从文件路径无法判断 backend，`--file` 必须同时传 `--backend`。
- 导入后的 Vibe Agent 名称必须唯一。冲突时跳过并清晰报告被跳过的名称。
- 导入结果是 Vibe-owned copy。
- source metadata 记录 backend、原始名称、原始路径或 ID。
- Backend-specific tool permissions 只保存到 `metadata_json`，V1 不执行这些权限。

Backend 映射：

| 来源 | 预期字段 |
| --- | --- |
| Claude Code global agent | name、description、system prompt、可选 model/tools metadata |
| Codex global agent | name、description、developer instructions/system prompt、可选 model/effort |
| OpenCode global agent | name、description、prompt/instructions、可选 model metadata |

具体解析应尽量复用现有 discovery/parsing helper，但持久化结果应该是
backend-neutral 的。

## UI 和 Scope Settings

UI 和 IM settings flow 应该停止把多个 backend-native subagent dropdown 作为
主要 routing 模型。

新形态：

- Agent selector：一个 Vibe Agent 下拉框。
- Agent detail preview：description、backend、model、effort。
- Workdir 仍然是 Scope 设置。
- Backend credential/config availability 仍然是全局 backend 配置。

这让 Scope 配置更容易解释：

```text
Scope = project/workspace.
Agent = how this scope should think and act.
Session = one ongoing conversation/run under that scope and Agent.
```

## Runtime 行为

### Human Messages

一次 human turn：

1. 解析 Scope。
2. 解析该 Scope 配置的 Vibe Agent。
3. 构造 `AgentRequest`，包含：
   - `agent_name` 或 `vibe_agent_name`；
   - backend；
   - model；
   - reasoning effort；
   - system prompt。
4. 路由到 Agent 选择的 backend。

如果未来需要 prefix-triggered 快速切换，应按 Vibe Agent 名称切换。Routing
基于 Vibe Agent resolution，而不是 backend-native subagent prefix parsing。

### Background New Sessions

对于 `--create-session --deliver-key <scope-id>`：

1. 解析 `scope_id`。
2. 解析 Scope 的 Vibe Agent。
3. 预留或创建 `agent_sessions.id`。
4. 把 resolved Agent identity 存到 Session。
5. 用这个 Agent 执行第一轮 turn。
6. 在命令输出里返回新的 `session_id`。

对 `--create-session-per-run`，每次 run 都重复这个流程。

### Existing Sessions

对于 `--session-id <id>`：

1. 加载已有 Session。
2. 使用 Session 里存储的 Agent identity 加载当前 Agent 定义。
3. 后续 turn 应用最新的 Agent prompt/model/effort。

这样用户心智更简单：编辑一个 Vibe Agent，会改变所有选择该 Agent 的地方，
包括已有 Session 的后续行为。

### System Prompt 组合

Agent system prompt 在 Vibe routing 层替代 backend-native subagent prompt 选择。
Vibe Remote 应该在现有 prompt injection 点注入：

```text
agent_system_prompt + vibe_remote_instructions
```

Backend-native 默认 prompt 仍然存在于 backend 底层。Vibe Remote 使用选中的
Vibe Agent system prompt 作为自己拥有的 prompt 层。

## 输出契约

`vibe agent` 命令应该使用和后台命令计划一致的 JSON 风格。

Create 输出：

```json
{
  "ok": true,
  "agent": {
    "name": "release-reviewer",
    "description": "Reviews release diffs and deployment risk.",
    "backend": "codex",
    "model": "gpt-5.4",
    "reasoning_effort": "high",
    "source": "manual",
    "updated_at": "2026-05-19T15:00:00+00:00"
  },
  "warnings": []
}
```

Import 输出：

```json
{
  "ok": true,
  "imported": [
    {
      "name": "reviewer",
      "backend": "codex",
      "source": "imported_codex",
      "source_ref": "~/.codex/agents/reviewer.md"
    }
  ],
  "skipped": [],
  "warnings": []
}
```

List 输出：

```json
{
  "agents": [
    {
      "name": "release-reviewer",
      "description": "Reviews release diffs and deployment risk.",
      "backend": "codex",
      "model": "gpt-5.4",
      "reasoning_effort": "high",
      "source": "manual",
      "updated_at": "2026-05-19T15:00:00+00:00"
    }
  ]
}
```

## 存储方案

### 推荐方案

增加 SQLite 表和字段：

```text
agents
scope_settings.agent_name
agent_sessions.agent_id / agent_name snapshot fields
```

这和 scopes、sessions 已经进入 SQLite 的方向一致。

### 兼容窗口

现有 JSON settings 形态可以在过渡期保留旧字段，但新代码应把 `agent_name`
作为 source of truth。

根据产品决策，旧 backend-native agent 字段不需要迁移。已有用户可能需要重新
选择、导入或创建 Vibe Agents。

## 和已有计划的关系

- `agent-run-harness.md`：后台/direct `--create-session` 通过 Scope 的 Vibe
  Agent 解析 runtime target，然后把实际执行记录到 `agent_runs`。
- `agent-run-harness.md`：`vibe agent run` 是 direct/manual execution 入口。
  它消费本 catalog 里的 Vibe Agent 定义，而不是接受 backend/model/effort
  override 参数。
- Delivery targeting 由 `agent-run-harness.md` 负责：`--deliver-key` 变成
  `scopes.id`，不是 legacy session key。

## 实施切片

1. 增加 Agent storage model 和 CRUD service。
2. 增加 `vibe agent list/show/create/update/remove`。
3. 先为一个 backend 增加 import discovery，再扩展 Claude/Codex/OpenCode。
4. 增加 `scope_settings.agent_name` 和 Scope routing 解析。
5. 更新 runtime message routing，让它解析 Vibe Agents。
6. 更新 UI/IM settings，让用户选择 Vibe Agents。
7. 更新 background session creation，让它使用 Scope Agent resolution。
8. 从主要 UI/docs 移除旧 backend-native agent routing。

## 规范摘要

1. Agent name 全局唯一。
2. Agent 编辑立即影响已有 Session。
3. Agent 的 `name` 和 `backend` 不可修改；其他字段允许修改。
4. Agent system prompt 在现有 prompt injection 点拼到 Vibe Remote instructions
   前面。
5. 导入的 backend-specific tool permissions 只保存到 `metadata_json`，V1 不执行。
6. `vibe agent import --from ...` V1 只导入 global agents。
7. 导入时名称冲突则 skip，并报告出来。
8. `vibe agent import --file <path> --backend <backend>` 导入一个显式
   markdown-with-header 文件。
9. Fresh install/migration 应创建显式系统默认 Agent，并用
   `default_agent_name` 指针引用；运行时不动态合成旧式 backend 默认 Agent。
