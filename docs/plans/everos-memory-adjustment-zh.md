# EverOS Memory 集成调整方案（讨论基线）

> 状态：讨论基线，尚未实施
>
> 日期：2026-08-08
>
> 适用分支：Avibe `dev`
>
> 关联仓库：`/Users/rk/work/chainbot/avibe-bot/EverOS`

本文沉淀 Avibe 当前 EverOS Memory 集成的研究结论，作为后续设计、实现和评审的共同基线。本文只定义方向、约束和迁移顺序，不代表代码已经完成，也不把 EverOS 当前尚未提供的能力描述为现状。

约束：本次调整不修改 EverOS 源码。需要 EverOS 新增 caller identity、receipt lookup、replay、rotation operation 等能力的事项，只能记录为未来上游能力或 Avibe 侧的 fail-closed 降级，不能列为本次 Avibe 实现阶段的交付前提。

提交形态：本文以 Avibe `dev` 分支为目标基线，落地时随实现一起走 PR 评审与合入，不直接合入 `master`。本文是本轮 Memory 调整的最新设计基线；`docs/plans/memory-processing-log-page.md`、`docs/plans/memory-architecture-deepening.md` 和 `docs/plans/everos-1.2.1-upgrade.md` 继续作为已实现历史与约束来源。

由本文 supersede 的旧叙述：

- `memory-processing-log-page.md`：把日志页当作 Memory 状态控制台、独立状态模型、全量 provider call payload 记录的来源；
- `memory-architecture-deepening.md`：把独立 Memory 状态页、`ready/syncing/degraded` 综合状态推导、`status_payload.data_exists` 与 drain/embedding safety gate 混在用户可见的 status payload 里；
- `everos-1.2.1-upgrade.md`：逐条 delivery flush 是当时 chat mode 必需；本方案删除该行为并改为 idle/close/max-age 触发器。

保留的旧叙述：

- `data_exists`、`/status`、CLI machine-readable 字段、drain/embedding safety gate、provider-root sentinel 与 owned-clear 流程、`memory_capture_queue` 的 durable enqueue 与 recovery、EverOS real-wheel contract test 体系；
- `everos-1.2.1-upgrade.md` 中 `project_id` 是 Agent Session cwd 的 HMAC 摘要的派生路径与权限隔离模型；
- 现有处理记录页的 memcell/capture/OME/provider call 精确关联实现。

## 1. 结论摘要

### 1.1 接入方式

继续使用 **Avibe 管理的 EverOS 子进程 + Unix Domain Socket（UDS）HTTP**，不改为 Avibe 主进程内直接 import EverOS。

这里的 HTTP 不是公网服务，也不是本机 TCP 服务，而是进程间的私有调用方式：

```text
Avibe 主进程
    │ httpx + Unix Domain Socket
    ▼
Avibe 管理的 EverOS 子进程
    ├── Markdown
    ├── SQLite
    ├── LanceDB
    ├── Cascade
    └── OME
```

当前 UDS sidecar、子进程生命周期、凭据隔离和 TCP listener 检查已经形成有效基础，应继续保留。需要调整的是上层接口和内部职责，而不是替换传输层。

### 1.2 flush 时机

删除“每条消息成功 add 后立即 flush”的行为。

目标策略：

1. 普通消息只调用 EverOS `/add`，让 EverOS 自动完成边界检测；
2. 会话空闲超时后执行一次 flush；
3. `/new`、会话归档、明确关闭会话或用户明确要求时执行 final flush；
4. 增加最长未 flush 时长，防止持续会话永远不落边界；
5. Avibe 关闭时不阻塞等待可能持续数分钟的 LLM flush，而是保留 durable 状态，下一次启动继续处理；
6. final flush 不能只依赖一个 close hook：必须先等待目标 session 的 outbox drain，或在 session fence 下禁止新的 provider add，并持久化本次 generation 的 watermark；否则 close 后才完成 add 的消息可能没有后续 flush 触发器。

初始空闲时长建议先取 5 分钟，具体值需要结合实际 LLM 成本和交互体验确认。

### 1.3 处理记录页和最小内部可靠性机制

不再维护一个试图表达完整 Memory 状态的独立状态页。将状态摘要合并到现有处理日志页，页面只展示当前能够可靠读取的数据：

- EverOS 最近一次 `/health` 摘要；
- EverOS 版本和 capabilities；
- Cascade health、pending 和失败计数（如果 `/health` 提供）；
- recorder 状态；
- 已有的 capture → add/flush → memcell → OME → indexing 处理流水；
- 已确认的异常、恢复记录和数据不可用提示。

不修改 EverOS，也不新增 EverOS 业务 metrics 协议。当前 EverOS `/metrics` 只有 HTTP 请求计数和耗时，不作为 Memory 页面核心数据源。页面必须标注数据的观察时间，并区分当前摘要、历史记录、过期数据和不可用数据。

outbox 和 sidecar lifecycle 仍然保留，但只保留可靠性所需的最小闭环：

- outbox：本地持久化、claim、调用 `/add`、有限重试、成功确认后清理 payload、启动恢复；
- lifecycle：启动/停止受管 EverOS 子进程、UDS 可用性、崩溃后恢复、clear/restart 时的调用 fence；
- 不为页面维护 outbox/lifecycle 的复杂指标树；
- 不根据多个来源推导 `ready/syncing/degraded` 等综合状态；
- 不在普通 log 页面请求中新增 processing endpoint probe 或 provider root 深度扫描；现有 `/status` 内为 drain/embedding safety gate 和兼容契约所需的检查先保留，待调用方迁移后再单独简化。

因此，页面简化不等于删除可靠性机制：可靠性状态只在内部用于投递和恢复，用户界面只显示能直接确认的结果。

### 1.4 数据保护和重建

EverOS 的 Markdown 是已经抽取完成的业务记忆真相源，LanceDB 是可重建的派生索引。但完整的安全边界不能只写成“Markdown + unprocessed buffer”，还必须包含：

```text
Avibe capture outbox
+ EverOS unprocessed_buffer
+ EverOS memcell
+ Markdown
```

在 `/add` 尚未确认前，Avibe outbox 是恢复来源；消息进入 EverOS 后，尚未完成边界切分的内容在 `unprocessed_buffer` 中；边界切分后的原始对话归档在 `memcell`；抽取完成后的业务记忆落在 Markdown。

禁止：

- 直接删除整个 EverOS `.index`；
- 直接删除 `.index/lancedb` 后期待自动恢复；
- 由 Avibe 自己递归删除 provider root。

索引恢复必须调用 EverOS 受控的 rebuild 操作，并遵守维护锁和队列重置顺序。

### 1.5 embedding key 和模型变更

- **只更换 API key**：不改变向量语义，不需要重建；重启 EverOS 子进程并复用原 provider root 即可。
- **模型、有效维度、归一化、截断、预处理或实际模型语义变化**：视为新的 embedding space，需要完整重建 embedding-dependent 派生数据。

完整重建不能只重建 LanceDB。EverOS SQLite 中的 cluster centroid 也是向量派生状态，还需要重新计算或明确失效；profile、agent skill、reflection 等依赖 cluster/embedding 的派生结果也需要纳入重建方案。

在完整 rotation 操作尚未具备之前，保持当前 fail-closed 行为，拒绝在已有数据上静默混用不同向量空间。

### 1.6 四类搜索

Avibe 应通过自己的搜索策略接口利用 EverOS 的四种搜索方式：

- `keyword`：精确术语、ID、错误码、命令名；
- `vector`：语义相似、同义改写；
- `hybrid`：默认通用搜索；
- `agentic`：需要多步推理的复杂搜索，显式开启且受能力、成本和超时限制。

不要把 EverOS DTO 直接泄露给 Avibe 上层。Avibe 只暴露 provider-neutral 的搜索策略，并在 adapter 内映射到 EverOS。

Profile 的目标 adapter 是否切换到 EverOS `/get` 取决于 profile scope 决策：在拍板完成前保持当前通过搜索字面量 `"profile"` 模拟的兼容行为。EverOS `/get` 当前按 owner 读取单行 profile，底层没有可靠的 project filter；因此在不修改 EverOS 的前提下，不能继续承诺 project-scoped profile isolation。需要在实现前拍板：profile 视为 user-global，还是由 Avibe 仅在当前项目范围内做兼容性隐藏/降级。详见 §9.3。

当前 session 的未处理消息可以作为近期上下文补充，但只能绑定 Avibe 可信的 canonical current session；不得把任意用户提供的 `filters.session_id` 转发为 overlay 查询。overlay 必须明确来源是 EverOS `unprocessed_buffer`，不代表已抽取 Markdown，也不覆盖尚在 Avibe outbox 或已写 Markdown 但尚未 Cascade 投影的数据。

## 2. 研究范围和版本基线

本方案基于以下源码状态：

- Avibe `dev`：`fbd406eab933fc84e88b10a4a6087c793ab6fc11`；已与 `origin/dev` 核对一致；
- EverOS 本地工作树：`560fb80`；
- EverOS `origin/main`：`48fc908`，相对于本地仅新增 `v1.2.3` 发布说明，没有改变本方案依赖的核心源码；
- EverOS 本地工作树包含用户准备的 `CONTEXT.md`、`EVEROS_INTEGRATION_zh.md` 及其他未跟踪知识库文件，研究期间未修改。

当前 Avibe 运行时仍固定 EverOS 1.2.1：

- `core/memory/artifact.py:35-42`
- `scripts/memory_runtime/pyproject.toml`

EverOS 源码元数据已经是 1.2.3，并要求 Python 3.12：

- `EverOS/pyproject.toml:1-8`

因此，1.2.3 升级属于独立的 runtime artifact、兼容性和发布工作，不能通过把 Avibe 指向 EverOS 工作树来完成。

## 3. 当前实现事实

### 3.1 Avibe 当前调用路径

当前 Memory 主要由以下模块组成：

| 模块 | 当前职责 |
|---|---|
| `core/memory/runtime.py` | controller-owned 生命周期、artifact 激活、sidecar 监管、worker 启停、配置 reconcile |
| `core/memory/module.py` | provider-independent capture/search/profile/status/clear |
| `core/memory/everos.py` | EverOS HTTP 请求、响应映射和 provider 错误分类 |
| `core/memory/sidecar.py` | 子进程内的 route/shape/attachment 校验和 EverOS app 启动包装 |
| `core/memory/process.py` | Python runtime、子进程、UDS、owner/reaping 和启动健康检查 |
| `core/memory/store.py` | Avibe 本地 capture queue、delivery、flush observation 和恢复 |
| `core/memory/worker.py` | queue claim、EverOS add、每条消息后的 flush、breaker 和启动恢复 |
| `core/memory/everos_insight/` | provider call recorder、处理日志和版本耦合的读取适配 |

现有最有价值的 seam 是 `MemoryProviderPort`，但当前 worker 仍然直接承担很多 EverOS 生命周期语义。后续应在不扩大上层接口的前提下，把 session flush 和状态采集拆成内部模块。

### 3.2 当前 flush 缺陷

`MemoryWorker.drain()` 在每个成功 delivery 后调用 `_flush_session()`：

- `core/memory/worker.py:168-182`
- `core/memory/worker.py:287-315`

这会破坏 EverOS 通过连续消息识别自然边界的设计，增加 LLM 调用次数，并把一个连续会话切割成过细的 memory cell。

### 3.3 当前搜索限制

Avibe 当前 `EverOSPort` 固定发送：

```json
{
  "method": "hybrid",
  "include_profile": true,
  "enable_llm_rerank": false
}
```

相关位置：

- `core/memory/everos.py:386-413`
- `core/memory/sidecar.py:296-311`

sidecar guard 当前还限制：

- 只能 user owner；
- 只能 `hybrid`；
- 不能传 filters/radius/min_score；
- profile 使用搜索查询 `"profile"`。

### 3.4 当前状态模型

Avibe 当前状态由以下内容拼装：

- 本地 queue stats；
- provider `/health`；
- processing endpoint probe；
- 磁盘空间；
- runtime error；
- provider root/data existence；
- recorder health；
- flush observations。

主要位置：

- `core/memory/module.py:348-420`
- `core/memory/module.py:592-635`
- `core/memory/runtime.py:542-580`
- `core/memory/runtime.py:1407-1429`

这些信息并非都应该删除，但不应该在每次 status 请求中重新执行所有探测。

### 3.5 当前 EverOS metrics 和 health

当前 EverOS `/metrics` 仅由 Prometheus HTTP middleware 产生：

- `EverOS/src/everos/core/middleware/prometheus.py:25-41`
- `EverOS/src/everos/entrypoints/api/routes/metrics.py:14-20`

目前只有：

- HTTP request counter；
- HTTP request duration histogram。

EverOS `/health` 包含 capabilities 和 cascade health，但其 `status="ok"` 明确是 liveness，不是完整业务 readiness：

- `EverOS/src/everos/entrypoints/api/routes/health.py:78-90`
- `EverOS/src/everos/entrypoints/api/routes/health.py:109-150`

因此，本次页面简化不依赖 `/metrics`，也不等待 EverOS 增加业务指标。可以删除 Avibe 面向页面的复杂状态采集和综合推导，但必须保留 outbox 可靠投递与 sidecar lifecycle 所需的最小内部状态。

## 4. 目标架构：一个深模块，多个内部实现

### 4.1 Avibe 对外接口

建议将业务调用收敛到以下三类操作：

```text
capture(CaptureRequest) -> CaptureReceipt
recall(RecallRequest) -> RecallResult
operate(MemoryOperation) -> OperationReceipt
```

初期 `operate` 只覆盖 owner/admin-gated 的 restart 和 clear；projection rebuild、embedding rotation 等需要更强 journal/fence/reconciliation 的操作保持内部运维能力，不提前作为普通公共 API 暴露。

状态页不再依赖一个新的、强语义的 `snapshot()` 接口。处理记录页使用已有日志读取能力，并在页面顶部轻量读取 EverOS `/health` 摘要。

#### `capture`

- 返回意味着 Avibe 已经将消息写入本地 durable outbox；
- 不等待 EverOS、LLM、embedding、抽取或 Cascade；
- 以 source-message identity 做幂等；
- 不承诺消息返回后立即可搜索。

#### `recall`

- 使用 Avibe-owned `RecallRequest`；
- 允许指定搜索策略和 freshness policy；
- 返回 Avibe-owned result，不返回 EverOS DTO；
- 可以携带 `unprocessed`、`eventual` 等 freshness 元数据。

#### `operate`

- 统一 restart、clear、projection rebuild、embedding rotation 等维护操作；
- 长操作返回 operation identity；
- lifecycle 互斥和数据安全由 Memory 内部持有；
- 不要求为每个操作建立新的全局状态机，页面只显示已确认的操作结果和处理记录。

### 4.2 内部模块

```text
Avibe Memory interface
    │
    ├── Durable capture store
    ├── SessionFlushCoordinator
    ├── ProcessingRecordView
    ├── MaintenanceCoordinator
    │
    ▼
MemoryEngine seam
    │
    ▼
EverOS UDS HTTP adapter
    │
    ▼
Pinned EverOS child runtime
```

上层不应该知道 `/add`、`/flush`、`include_profile`、LanceDB、Cascade、OME 或 EverOS error envelope。它们都属于 adapter 或内部协调模块。

## 5. flush 目标设计

### 5.1 目标时序

```text
消息
  │
  ▼
Avibe durable outbox
  │
  ▼
EverOS /add
  │
  ├── status=accumulated：继续留在 EverOS unprocessed_buffer
  └── status=extracted：EverOS 已完成一次边界抽取
  │
  ▼
自然会话边界 / idle / explicit close
  │
  ▼
EverOS /flush
  │
  ▼
Markdown 已落盘
  │
  ▼
Cascade 异步投影到 LanceDB
```

`/add` 成功不等于可搜索；`/flush` 成功也不等于 LanceDB 已经完成投影。UI 和调用者必须保留 eventual consistency 语义。

### 5.2 Flush 触发条件

| 触发条件 | 是否触发 | 说明 |
|---|---:|---|
| 每条消息成功 add | 否 | 避免切碎连续对话 |
| EverOS `/add` 自动边界 | 由 EverOS 决定 | Avibe 不追加无意义 flush |
| session idle timeout | 是 | 初始建议 5 分钟 |
| `/new` 或 session close | 是 | final flush |
| 会话归档 | 是 | 需要接入统一 lifecycle hook |
| 用户明确要求立即落 memory | 是 | 内部接口保留 bounded wait 语义；第一版 UI 不暴露，由 Phase 3 之后的产品决策再开放 |
| 最大未 flush 年龄/消息数 | 是 | 防止长会话无限积累 |
| Avibe shutdown | 不同步等待 | 持久化 due 状态，下次启动恢复 |

### 5.3 Flush generation 不变量

每个 `(app, project, session)` 维护独立的 flush generation。canonical key 必须在 capture、add、flush、日志和恢复路径中统一，不能在不同模块间混用 `(session, project)`、`(app, project, session)` 或带 principal/epoch 的变体。

- flush 开始前停止目标 session 的新 provider add，或等待该 session 的 outbox drain 完成；
- 持久化 generation、已确认 add 的 watermark 和 fence epoch；
- flush in-flight 期间新消息只能进入下一个 generation；
- settlement 只能更新被 fence 的 generation，不能按 session/project 更新所有历史 delivery rows；
- 同一 session 同时最多一个 flush；
- `unknown` 结果不能自动无限重放；
- 新 worker 启动时先恢复 `in_flight`，再处理 `due`/`not_attempted`，并做分批退避，避免重启 flush storm；
- 不同 session 允许并发，但受全局 provider 并发上限约束。

### 5.4 上游恢复缺口

EverOS 当前 boundary 逻辑先写 memcell、再替换 unprocessed buffer、随后才运行下游 pipeline：

- `EverOS/src/everos/service/_boundary.py:154-198`
- `EverOS/src/everos/service/memorize.py:236-279`

进程在这些步骤之间崩溃时，可能出现 memcell 已有、buffer 状态已变化、Markdown 或 OME pipeline 尚未完成的情况。当前不能据此宣称完整 exactly-once extraction。

在 EverOS 当前没有 caller message identity、flush operation/generation identity 或 receipt lookup 的前提下，Avibe 不能把 unknown 宣称为可安全重放。目标恢复契约为：

- unknown 表示 provider 可能已经提交；
- Avibe 持久化 unknown、generation 和 fence，不自动重放；
- 只有明确证明 `not_committed`，或存在有效 receipt/reconciliation，才允许自动继续；
- 不能确认时保持 `manual_required`，而不是永久伪装成成功或无限 at-least-once replay；
- extraction receipt/ledger、caller stable identity 和 memcell-to-Markdown replay 属于未来 EverOS 能力，不纳入“本次不修改 EverOS”的实现前提。

## 6. 处理记录页设计

### 6.1 页面形态

删除独立的 Memory 状态页，将状态摘要合并到处理记录页：

```text
Memory
├── 处理记录
│   ├── EverOS 运行摘要
│   ├── 最近处理流水
│   ├── 异常与恢复
│   └── 诊断详情
├── Profile
├── Search
└── Settings
```

页面顶部只展示最近一次能够成功读取的 EverOS `/health` 摘要：

- EverOS version；
- LLM、embedding、reranker、parser、agentic 等 capabilities；
- Cascade `healthy`、`pending`、retryable/permanent failure counts；
- Cascade reasons；
- Avibe recorder state。

每次摘要必须携带 `observed_at` 或等价的观察时间。读取失败、缺失或过期的数据应显示为 `unknown/unavailable`，不能推导成 `ready` 或“所有记忆已完成”。

### 6.2 处理流水

继续使用已有的处理日志，不新增 EverOS 业务 metrics 或私有 SQLite 状态接口：

```text
capture
  → add / flush
  → memcell
  → episode
  → OME strategy
  → profile / skill
  → indexing
```

只展示已经能够通过现有 provenance 关联确认的记录。对于数据库缺失、锁定、格式错误、关联标记过期等情况，显示对应步骤 unavailable，并保留其他步骤。

### 6.3 复杂状态推导的删除范围

以下内容不再作为用户可见状态页的主模型，也不应在普通 log 页面读请求中重新计算：

- `ready/syncing/degraded` 综合状态树；
- Avibe outbox 的完整 pending/succeeded/dead/missed 指标树；
- provider root 是否存在和深度扫描；
- processing endpoint 主动 probe；
- 多个接口结果之间的复杂 precedence；
- 用一次 `/health` 成功推导整个 Memory pipeline ready。

`/status` 路由作为内部契约仍保留：CLI、UI 设置、保存校验、drain/embedding safety gate 都依赖它，其中的探测和 `data_exists` 推导不在本轮范围内简化；处理记录页的新读取路径不再触发它们。

页面可以显示已经直接确认的单项事实，例如最近一次 `/health` 失败、某条 add/flush 失败、某个 recorder degraded，但不把它们组合成超出数据来源语义的总状态。

### 6.4 `/metrics` 的处理

当前 EverOS `/metrics` 只有 HTTP request counter 和 duration histogram：

- `EverOS/src/everos/core/middleware/prometheus.py:25-41`
- `EverOS/src/everos/entrypoints/api/routes/metrics.py:14-20`

在“不修改 EverOS”的前提下，不把 `/metrics` 作为 Memory 页面核心依赖，也不通过 Avibe 维护一套自定义 EverOS business metrics 协议。后续如 EverOS 自己提供稳定的业务 metrics，可以再作为可选数据源接入，但不作为本次调整的前置条件。

### 6.5 内部 outbox 和 lifecycle 的最小职责

#### Outbox 最小闭环

outbox 只负责：

1. 将 capture payload 持久化；
2. 以稳定 identity 去重；
3. claim 一条待投递记录；
4. 调用 EverOS `/add`；
5. 对明确可重试错误做有限重试；
6. 只有结构完整、状态值受支持的明确 provider ack 才允许 success settlement 和敏感 payload 清理；malformed 2xx 按 unknown/manual_required 处理并保留可恢复数据；
7. Avibe 重启后恢复未完成 claim。

为支持 idle/max-age flush，Avibe-owned durable state 至少要增加 `generation`、`first_unflushed_at`、`last_add_ack_at`、`due_at`、`next_attempt_at`、`flush_state`、`watermark` 和 `fence_epoch`。idle 定时器由 Avibe controller/runtime 的长期任务托管；重启从数据库恢复 due session，并按全局并发上限和退避窗口分批处理，避免 restart storm。持续新消息只能更新 idle due，不能重置 `first_unflushed_at`，从而保证 max-age 不会 starvation。

outbox 不负责：

- 计算全局 Memory ready 状态；
- 解释 EverOS Cascade/OME 内部状态；
- 为 UI 提供完整的业务指标仪表盘；
- 代替 EverOS 的 Markdown、memcell 或 unprocessed buffer。

#### Lifecycle 最小闭环

lifecycle 只负责：

1. 准备并启动受管 EverOS 子进程；
2. 确认 UDS 可用；
3. 进程退出或失联时回收并按策略重启；
4. restart/clear/配置切换时停止旧 child，避免共享 root 并发使用；
5. shutdown 时停止 worker、recorder 和 child。

lifecycle 不负责：

- 通过多层探测推导复杂 Memory 状态；
- 对 EverOS 内部队列做私有扫描；
- 维护第二套 provider 状态数据库；
- 为页面提供实时的“所有阶段已收敛”保证。

这两个模块仍然是内部可靠性机制，但它们的状态不再扩大为用户需要理解的产品状态模型。

## 7. 数据持久化、备份和重建

### 7.1 数据层级

| 层 | 作用 | 是否可由 Markdown 完整重建 | 保护要求 |
|---|---|---:|---|
| Avibe capture outbox | `/add` 前的本地 durable intake | 否 | 必须保护，直到 provider receipt/明确恢复 |
| EverOS `unprocessed_buffer` | 已接收但尚未 boundary 的原始消息 | 否 | 必须保护 |
| EverOS `memcell` | boundary 后的原始对话单元 | 当前不应假定可从 Markdown 完整恢复 | 必须保护，直到 extraction recovery 契约成立 |
| Markdown | 已抽取业务记忆真相源 | 它本身是真相源 | 必须保护 |
| `md_change_state` | Cascade 队列和 LSN | 可重建，但受控 reset 更安全 | rebuild 时按顺序操作 |
| OME SQLite | 异步策略运行状态 | 部分可重建 | 维护操作中评估是否需要重放 |
| LanceDB | 向量/BM25/标量派生索引 | 可由 Markdown 重建 | 不直接 rm，使用 EverOS rebuild |
| cluster centroid | embedding 派生的 SQLite 向量 | 不是普通索引 | rotation 时必须重算或失效 |

### 7.2 备份范围

最安全的默认备份是整个 provider root 的一致性快照，加上 Avibe 自有状态和附件目录：

```text
~/.avibe/state/memory/memory.sqlite
~/.avibe/memory/everos-root/
~/.avibe/memory/call-log/call-log.db
~/.avibe/attachments/avibe/
```

当前 capture queue 只保存 attachment URI/metadata，`core/memory/attachments.py:12-59` 不复制附件字节；因此如果备份策略不包含附件原文件，accepted capture 可能无法重放。进入实现前必须明确附件策略：Workbench 上传本来就位于 Avibe-owned private attachment store；默认建议在 capture acceptance 时为 pending queue row 建立 durable attachment reference/pin，并把该目录纳入一致性快照、大小/保留期和恢复验证。若产品不提供附件复制或备份保证，则 capture 只能声明 metadata 已保存，不能承诺附件内容可恢复。

至少必须覆盖：

- Markdown；
- `system.db` 中的 unprocessed buffer 和 memcell；
- Avibe capture queue；
- attachment bytes（如果 capture 允许作为可恢复输入）；
- OME 状态（如果需要保留异步任务的精确恢复）；
- call-log（如果需要保留处理诊断）。

不能在 SQLite 正在写入时直接复制 live database 文件；应暂停相关进程或使用 SQLite backup/snapshot 语义，并让附件快照与 queue snapshot 具备明确的一致性 fence。

### 7.3 Projection rebuild

适用于 LanceDB 损坏、schema drift 或索引漂移。当前 EverOS 提供的是 CLI 运维操作，不是 Avibe 可直接调用的业务 HTTP operation；Avibe 不应把“调用受控 rebuild”写成已有 API 能力。

1. 创建并持久化 `operation_id`、operation phase、provider-root fingerprint、fence epoch 和 owner；
2. 获取 exclusive maintenance lease；
3. fence Avibe 新 delivery 和旧 epoch provider calls；
4. 停止 EverOS server；
5. 先 reset Cascade queue；
6. 仅删除并重建 LanceDB business tables；
7. 扫描全部 Markdown；
8. 等待并验证 Cascade drain；
9. 更新 operation journal，记录每个 phase 的结果；
10. 启动 sidecar，恢复 delivery；
11. 启动时根据 journal resume、rollback 或 fail closed，不能把中途 crash 当成成功。

所有 destructive phase 必须有故障注入测试。迟到的旧 epoch 写入必须被拒绝或丢弃，不能写入新 generation/root。

EverOS 当前受控实现已明确“先 reset queue，再 drop tables”：

- `EverOS/src/everos/entrypoints/cli/commands/cascade.py:494-529`

Avibe 不应自行复制这一套底层逻辑，而应调用受控的 EverOS operation。

### 7.4 不允许的恢复方式

```text
rm -rf .index
rm -rf .index/lancedb
```

原因：

- 删除整个 `.index` 会删除尚未抽取的 `unprocessed_buffer`；
- 只删 LanceDB 会留下 done queue，导致扫描器认为文件已处理，索引可能恢复为空。

EverOS `docs/storage_layout.md` 和 `docs/how-memory-works.md` 中仍有“整个 `.index` 可删除”的旧表述，应在上游统一为当前更严格的恢复契约。

## 8. embedding rotation

### 8.1 Semantic fingerprint

建议定义：

```text
embedding_semantic_fingerprint = hash(
    provider semantic identity,
    effective model identity/revision,
    output dimension,
    dimensions parameter,
    normalization,
    truncation,
    preprocessing/tokenizer revision,
    vector/index schema revision,
)
```

API key 不进入 fingerprint。

fingerprint 必须持久化在 Avibe-owned Memory metadata 中，并在每次启动/reconcile 时与候选配置比较；不能只存在于进程内。未知模型身份、缺失 fingerprint 或无法证明语义等价时 fail closed。provider-root sentinel 可以记录创建 runtime 的 artifact fingerprint，但不能代替 semantic embedding fingerprint。

只把真正会改变向量空间的因素纳入 fingerprint；例如，等价的代理 base URL 变化不应仅凭 URL 字符串强制重建，但无法证明等价时应 fail closed。

### 8.2 Key-only rotation

```text
验证新 credential
→ fence provider calls
→ 停止旧 sidecar
→ 用新 key 启动新 sidecar
→ 复用原 provider root
→ 验证 capability
→ 恢复 claims
```

不删除 Markdown、SQLite、LanceDB，也不触发 full rebuild。

### 8.3 Full semantic rotation

```text
获取维护锁
→ fence capture delivery / provider calls
→ 保存并验证当前 root
→ 停止 EverOS
→ 保留 Markdown、unprocessed_buffer、memcell、Avibe outbox
→ 重建 LanceDB 向量和 FTS/标量索引
→ 重算 cluster centroid 和 cluster membership
→ 重建或失效 embedding-dependent OME 状态
→ 重新处理 profile/skill/reflection 派生结果
→ 写入新 fingerprint
→ 启动新 sidecar
→ 验证 projection convergence
```

第一版允许离线停机重建。后续如有明确 SLA，再考虑 shadow index/blue-green generation。

在 EverOS 尚未提供完整 rotation operation 之前，Avibe 保持已有数据上的 embedding change guard，不把 LanceDB-only rebuild 错报为完整迁移。

## 9. 四类搜索设计

### 9.1 Avibe-owned 搜索策略

建议定义：

```text
RecallPolicy
  mode: auto | keyword | vector | hybrid | agentic
  limit: 1..N
  freshness: eventual | bounded | session_overlay
  include_profile: bool
  filters: provider-neutral filter tree
```

- `mode=auto` 只在 `keyword/vector/hybrid` 中选择，不由 Avibe 隐式升级到 `agentic`；
- `mode=keyword/vector/hybrid/agentic` 是显式选择，由调用方负责声明预算；
- `freshness=eventual` 不承诺 deadline；`bounded` 是 caller deadline 前的 best effort，超时显式返回 timeout/unknown；`session_overlay` 仅绑定可信 current session。

EverOS DTO、filters DSL 和 response arrays 只存在于 adapter 内部。

### 9.2 模式选择

| 模式 | 使用场景 | 能力要求 | 默认性 |
|---|---|---|---|
| `keyword` | 精确名称、ID、错误码、命令、术语 | BM25/index | embedding 不可用时的降级 |
| `vector` | 同义改写、语义相似、多语言 | embedding | 显式/策略选择 |
| `hybrid` | 通用召回，兼顾精确和语义 | embedding + BM25 | 默认 |
| `agentic` | 多跳、复杂推理、多次检索 | LLM + embedding + reranker | 显式开启 |

本次调整同时纳入 reranker 配置和显式 agentic search。接受其额外 LLM/reranker 延迟和调用成本作为产品语义成本。

#### 9.2.1 agentic 默认与触发策略

- `auto` 不隐式选择 agentic，只能在显式策略中选择 `keyword/vector/hybrid`；
- 普通 UI search 不调用 agentic；
- 普通聊天/系统 recall 不调用 agentic；
- agentic 只能由调用方显式声明，且必须经过 Avibe-owned policy 验证；
- 一次调用可以重复声明同一 mode，但每条声明独立计时、独立计预算。

#### 9.2.2 agentic 必填预算

Avibe-owned `RecallPolicy` 在允许 `agentic` 时必须携带：

- `timeout_seconds`：默认 30s；
- `max_model_calls`：默认 4（包含 reranker 步骤与 reflect/merge 等内部步骤）；
- `max_results`：默认 20；
- `cost_budget_tokens`：可选；超过则立即 `capability_unavailable`；
- `allow_fallback_to_hybrid`：默认 false；超时和 capability 失败返回明确结果，不伪装为 hybrid 成功。

不携带完整预算的 agentic 请求在 adapter 层 fail closed。

#### 9.2.3 Reranker 配置

新增 Avibe 配置：

```text
memory:
  processing:
    reranker:
      enabled: true
      base_url: https://...
      model: ...
      api_key: ...
      timeout_seconds: 20
      max_concurrent: 4
```

行为：

- API key 只进入 managed EverOS child 环境，与 LLM/embedding 一致；
- API key change 仅触发 sidecar restart，不进入 semantic embedding fingerprint；
- model change 仅记录 `reranker_config_fingerprint`，不重建 LanceDB；
- 缺失/未启用时，`agentic` capability 不可用；
- UI/API 响应中只暴露 enabled、configured、available 三个状态，不暴露 key 或 secret；
- `/health` 摘要的 capabilities 增加 `reranker`，便于处理记录页和检查脚本定位；
- 处理记录页只显示 capability 状态，不展示供应商配置。

#### 9.2.4 capability gating

收到 `mode=agentic` 的请求时：

- LLM、embedding、reranker 全部 available 才放行；
- 任意一个不可用时返回 `capability_unavailable`，并说明哪个 provider 缺失；
- 不得 fallback 到 hybrid；
- 不得通过 agentic 配置来隐式触发；
- 请求体只接受 allowlist 中允许的 method，超出则由 sidecar route guard 拒绝（与 capability gating 是两道独立闸门）。

#### 9.2.5 隐私和日志

- agentic search 仍按现有 provider call recorder 策略：不记录完整 payload，只记录 kind、mode、duration、provider status 和脱敏错误；
- Agent chat memory capture 不在本次范围；不开启 agent case/agent skill；
- user-memory agentic 仅对调用方返回可见记录；不进入其他用户 profile。

#### 9.2.6 范围限制（明确不进入）

- 不开启 assistant/tool/agent capture；
- 不切换 Memory capture provenance；
- 不改变 owner scope；
- 不修改 EverOS 源码；
- 不在 EverOS 上游未提供前承诺 `agentic` capability always available。

### 9.3 Profile 和近期消息

- Profile 是否使用 EverOS `/get(memory_type="profile")` 取决于 profile scope 决策；当前 search workaround 在迁移完成前保持兼容；
- 相关性查询使用 `/search`；
- 当前 session 的未处理消息使用 EverOS `unprocessed_messages` overlay；
- 结果中区分 `source=unprocessed` 与 `source=extracted`；
- 不把未处理消息误标为已经抽取的长期记忆。
- `eventual` 不承诺 deadline；`bounded` 是调用方 deadline 前的 best effort，超时必须返回显式 timeout/unknown，不得声称已完成；`session_overlay` 只允许可信 current session，并明确数据来源和 partial 状态。
- overlay 不覆盖仍在 Avibe outbox 的消息，也不覆盖已写 Markdown 但尚未 Cascade 投影的记录；如需 read-your-write，必须另定义目标 watermark 和等待范围。

## 10. 错误、并发和安全

### 10.1 错误分类

adapter 必须优先按照 EverOS `error.code` 分类：

- 网络错误、超时、`EXTERNAL_SERVICE_UNAVAILABLE`：有限退避重试；
- `INVALID_INPUT`、`BAD_REQUEST`、`UNSUPPORTED_FORMAT`：不重试；
- `PROVIDER_NOT_CONFIGURED`：配置修复前不重试；
- `CAPABILITY_UNAVAILABLE`：永久能力缺失，不按 HTTP 503 盲目重试；
- 未知 add/flush outcome：记录为 `unknown`，按“可能已提交”处理；没有 receipt/reconciliation 或明确 `not_committed` 证据时不自动 replay，转为 `manual_required`，不声称 extraction 成功。

当前 `EverOSPort` 仍较多依赖 HTTP status，后续应集中修正。

### 10.2 并发规则

- 同一 `(app, project, session)` 的 add/flush 串行；
- 不同 session 可以并行；
- 一个 provider root 只允许一个 EverOS process；
- clear、rebuild、rotation、artifact cutover 互斥；
- 不持有 SQLite transaction 跨越 HTTP/LLM/model 调用；
- maintenance lease 必须先于 session lease 获取，并固定锁顺序。

### 10.3 安全边界

继续保留：

- owner-only UDS；
- exact route/shape validation；
- HMAC-derived principal/project，不保存原始平台 ID和工作路径作为 provider identity；
- attachment root containment 和 symlink 检查；
- child environment allowlist；
- response body、item 数量、嵌套深度和字符串长度限制；
- 不暴露正文、secret、用户 ID 或高基数 session label。

Provider call log 是处理记录页的诊断数据源，不是通用 metrics。它仍需保留，因为当前 UI Log 和 provenance 读取依赖它；但不与新的全局状态机耦合。

## 11. 现有处理记录页的改进方向

现有日志页的核心展示逻辑是合理的：以 memcell 为中心，展示捕获、add/flush、OME 处理、provider call 和索引关联。它应从“Memory 状态控制台”调整为“处理记录和诊断页面”，不需要推倒重做。

### 11.1 保留的核心逻辑

继续保留：

- memcell 列表和分页；
- memcell 预览、时间和消息数量；
- 详情页的精确 capture → memcell → OME run → provider call 关联；
- scope/principal/project 的访问控制；
- provider call 的 bounded、scrubbed request/response 展开；
- 数据缺失、过期、截断和不可用的 fail-closed 提示；
- 列表和详情的数量、字节大小及嵌套深度限制。

这些逻辑回答的是“这条记忆是如何被处理的”，与新的处理记录页定位一致。

### 11.2 明确区分四种数据语义

页面和后端类型需要区分：

| 数据语义 | 例子 | 展示规则 |
|---|---|---|
| 历史处理事实 | memcell、capture、精确关联的 run/call | 进入历史时间线 |
| 数据来源可用性 | EverOS DB、capture queue、call-log 是否可读 | 标记为 `Source availability`，不称为系统健康 |
| 当前快照 | 当前 profile、当前 indexing row、当前 error | 单独显示为 `Current snapshot`，不放入历史时间线 |
| 推导或不完整数据 | run 聚合、profile trigger 关系、过期 call-log | 标明 `inferred/expired/omitted`，不能当作完整事件 |

当前 `current_state` 应改名为 `Current snapshot`，并与历史 steps 视觉分离。

当前 `sections` 应改名为 `Source availability`，只表达相关数据源是否可读；`partial` 不应映射为全局 `Memory degraded`。

### 11.3 列表页的活动摘要

当前 `run_summary` 是从 OME `run_record` 聚合而来的派生数据，不是完整生命周期事实。建议：

- 列表页只保留紧凑的“处理活动”摘要；
- 不展示复杂的 run 状态组合；
- 详细 run 状态放入 memcell 详情页；
- `authorized_call_count` 改名为“Recorded calls”或“Linked calls”，避免暗示覆盖所有 provider calls。

### 11.4 Provider call 的展示边界

provider call 详情应保留，但文案要明确它是“已记录的提供方调用”，不是全部调用：

- recorder degraded 时提示部分调用可能未记录；
- call-log 过期时显示 expired；
- provider payload 仍只展示 scrubbed、bounded 字段；
- 继续禁止展示 raw sidecar stdout/stderr、附件字节、embedding vectors 和未经处理的秘密；
- copy 操作只复制已经投影和脱敏后的内容。

### 11.5 处理记录页目标结构

```text
处理记录

┌────────────────────────────────────┐
│ EverOS 运行摘要                    │
│ version · capabilities · cascade   │
│ recorder · observed_at             │
└────────────────────────────────────┘

┌────────────────────────────────────┐
│ Source availability                │
│ EverOS · capture · call log        │
└────────────────────────────────────┘

┌────────────────────────────────────┐
│ Memcell 列表                       │
│ 时间 · 预览 · 消息数 · 处理活动     │
└────────────────────────────────────┘

详情：
  Memcell 概览
  ├── 历史处理流水
  ├── 已记录的 provider calls
  ├── Current snapshot
  └── 缺失/过期/截断说明
```

顶部 EverOS 运行摘要只轻量读取现有 `/health`，不引入新的 EverOS 接口，也不展示 Avibe outbox/lifecycle 的完整内部状态树。

### 11.6 需要删除的误导性表达

不得从以下信息推导全局状态：

- 某个 memcell 的 run 成功；
- linked/recorded calls 数量；
- Cascade pending 为零；
- 最近一次 `/health` 成功；
- 当前 profile 文件存在。

它们最多只能证明各自的单项事实，不能证明“所有消息已处理”“所有 OME 已完成”或“所有索引已收敛”。

### 11.7 EverOS 私有 schema 依赖

现有 `core/memory/everos_insight/reader.py` 直接读取 pinned EverOS 的 `system.db`、`ome.db`、`md_change_state`、memcell 和 provider call provenance。这套日志适配可以保留，但必须：

- 明确它是 pinned-runtime adapter，而不是稳定 EverOS 公共 API；
- EverOS 版本升级时运行 real-wheel contract tests；
- schema/字段不匹配时 fail closed 为 unavailable/partial，不返回伪造成功；
- 将 message-id derivation、run event 字段和 owner/scope 关系集中在 adapter 内；
- 不把私有读取逻辑扩展成全局 status 状态机。

### 11.8 处理记录页验收重点

- 列表和详情能够围绕 memcell 展示精确可验证的处理事实；
- 当前快照与历史时间线视觉、类型和文案分离；
- Source availability 不被解释为 Memory 全局健康；
- provider calls 明确标注为已记录、可能过期或可能缺失；
- run_summary 在列表中保持紧凑，详情中可展开；
- EverOS `/health` 摘要包含观察时间，并按 capabilities 列出 LLM、embedding、reranker、parser、agentic 等可用性；
- 私有 schema 读取失败时显示 unavailable/partial，不推导 ready/degraded；
- 现有权限、scrub、分页和响应边界继续有效。

## 12. 分阶段实施计划

### Phase 0：先闭合可靠性和兼容契约

本阶段不以 EverOS 1.2.3 升级为前置条件。优先定义并测试 Avibe 侧契约：

- unknown provider outcome 的 `manual_required`/不自动重放规则；
- final flush 的 outbox drain 或 session fence；
- canonical `(app, project, session)` key、generation、watermark、fence epoch；
- idle/max-age 的 durable due、启动恢复和分批退避；
- attachment acceptance、私有复制和备份范围；
- profile scope、session overlay 授权和 freshness 语义；
- 现有 status payload、`data_exists`、CLI machine-readable 字段保持兼容；
- 定义处理记录页的数据边界和 pinned-runtime adapter contract。

EverOS 1.2.3 runtime artifact、manifest/checksum 和兼容性验证是独立发布轨道，可并行推进，不阻塞停止 per-message flush 或处理记录页简化。

### Phase 1：合并状态页和处理记录页

- 删除独立的 Memory 状态页的用户可见叙事，保留现有 `/status`、`data_exists`、CLI 和内部 machine-readable contract；
- 在现有处理记录页顶部读取已有 EverOS `/health` 摘要；
- 页面展示 EverOS version、capabilities、Cascade 摘要、recorder 状态和观察时间；
- 保留现有 processing log、异常和恢复记录；
- 不修改 EverOS，不新增业务 metrics 协议；
- 删除用户可见的复杂综合状态推导、provider probe 和 provider root 深度扫描，不删除 drain/embedding safety gate 所需的内部检查；
- 保留 outbox/lifecycle 的内部投递和子进程监管能力，但不把其完整计数树暴露到页面；
- 旧 status 字段先保持兼容，新增 source/observed_at/unknown 信息采用 additive projection。

### Phase 2：session flush coordinator

- 增加 durable session flush state；
- 删除 add 后立即 flush；
- 增加 idle、max-age、explicit-close flush；
- 接入 `/new`、archive、session close；
- 增加启动恢复和 unknown fencing；
- 增加同 session generation/concurrency 测试。

### Phase 3：搜索策略扩展

- 新增 reranker 配置和 Avibe-owned 配置写入；
- sidecar allowlist 放开 keyword/vector/hybrid/agentic 四种 method 校验；
- adapter 映射 Avibe-owned RecallPolicy，含 timeout/max_model_calls/max_results/cost_budget_tokens；
- hybrid 默认；keyword 降级；agentic 必须显式；
- LLM/embedding/reranker 三个 capability 都 available 才允许 agentic；缺一返回 `capability_unavailable`，不 fallback；
- profile 是否改 `/get` 取决于 profile scope 决策，不能在 project isolation 未闭合时直接切换；
- 增加仅绑定可信 current session 的 unprocessed overlay；
- 增加 scope isolation、filters、overlay 越权、freshness、agentic 预算和 capability 边界测试；
- 处理记录页的 capabilities 增加 `reranker` 字段，agentic search 仍按当前 provider call recorder 策略只记元数据，不记录 payload。

### Phase 4：Avibe 侧恢复边界和故障注入

本阶段不修改 EverOS，也不承诺 EverOS 当前没有的 receipt/replay 能力：

- unknown add/flush 无 receipt 时保持 `manual_required`，不自动重放；
- 明确 payload scrub、附件保留和 durable-home 转移的安全前置条件；
- 为 Avibe enqueue、add timeout、flush timeout、sidecar crash、controller restart 注入故障；
- 对 EverOS boundary 后、Markdown 前的 crash window 只验证 fail-closed 可见性，不伪造 exactly-once 修复；
- 将 Avibe 可控制范围内的 durability invariant 作为 contract test；
- caller stable identity、receipt lookup、memcell-to-Markdown replay 记录为未来 EverOS 上游能力。

### Phase 5：embedding rotation

- 先实现 key-only restart；
- 再实现离线 full semantic rotation；
- 覆盖 LanceDB、cluster、OME 和 embedding-dependent Markdown；
- 使用维护锁、operation journal（operation_id/phase/fingerprint/fence_epoch/owner）和 rollback/fail-closed；
- 旧 epoch 的迟到写入必须拒绝；
- 后续再评估 blue-green rebuild。

### Phase 6：发布和验证

- focused unit/contract tests；
- EverOS real-wheel contract tests；
- 运行时 artifact checksum/architecture/version tests；
- UI build；
- 用户可见行为使用本地 Incus regression 环境验证，不重启本地 coding-agent `vibe` 服务。

## 13. 验收标准

### Flush

- 连续消息不会每条都触发 flush；
- 一个 session 的多条消息可合并为自然 memory cell；
- idle/close/max-age 触发可靠；
- final flush 先完成目标 session outbox drain，或在 session fence 下禁止新的 provider add；
- generation 使用统一 `(app, project, session)` key、watermark 和 fence epoch；
- 新消息在 in-flight flush 期间不会丢失或混入错误 generation；
- due/unknown session 的状态持久化并在重启后分批恢复；
- unknown 无 receipt 时进入 `manual_required`，不自动重放；
- shutdown 不因 LLM flush 无限阻塞。

### 处理记录页

- 独立状态页已移除，状态摘要合并到处理记录页；
- 页面只展示 EverOS `/health` 和现有处理日志能够直接确认的数据；
- 每个运行摘要携带观察时间，读取失败或过期时显示 unknown/unavailable；
- 不把 `/health.status="ok"` 推导成全部 Memory pipeline ready；
- 普通页面读取不触发 processing endpoint probe 或 provider root 深度扫描；
- outbox/lifecycle 的完整计数和内部状态机不暴露为用户可见仪表盘；
- 不暴露正文、密钥或无关的内部标识；
- recorder degraded/corrupt 仍可在处理记录页显示和恢复。

### 最小可靠性机制

- outbox 保留 durable enqueue、幂等、claim、有限重试、success settlement 和启动恢复；
- lifecycle 保留 child start/stop、UDS、崩溃恢复和维护操作 fence；
- 删除 outbox/lifecycle 对全局 ready/syncing/degraded 的复杂推导职责；
- 简化后仍能保证 provider 暂时不可用时已接受 capture 不会直接丢失；
- 同一个 provider root 不会被两个受管 EverOS 子进程同时使用。

### 数据和重建

- Avibe accepted capture 至少存在于一个可恢复 durable home；
- attachment capture 只有在附件字节位于 Avibe-owned durable store 且已被 pending capture pin 住，或产品明确接受不可重放语义时，才允许宣称可恢复；
- `.index` 误删不会被文档或代码描述为无损；
- projection rebuild 不删除 unprocessed_buffer/memcell；
- LanceDB rebuild 后 done queue 不会导致空索引；
- embedding key-only change 不触发 full rebuild；
- semantic change 不会混用两种 vector space；
- rotation 会处理 cluster centroid 和 embedding-dependent OME 状态；
- rebuild/rotation 的 operation journal 能在 crash 后 resume、rollback 或 fail closed；
- status payload、`data_exists`、CLI machine-readable 字段在页面简化后仍保持兼容。

### 搜索

- keyword/vector/hybrid/agentic 有真实 adapter contract test；
- agentic 缺失 reranker/LLM/embedding 时返回 capability-unavailable，且不 fallback；
- agentic 显式超时返回明确 timeout，不伪装为 hybrid；
- hybrid 是默认；
- embedding 不可用时 keyword 仍可用；
- agentic 在当前缺少 reranker/预算配置时只返回 capability-unavailable，不隐式调用；
- profile 的 `/get` 目标行为通过测试，但 project isolation 在 profile user-global/project-scoped 决策前不得作为验收承诺；
- session overlay 只能访问可信 current session，不能接受任意 session filter；
- user/project/session 隔离测试通过（profile scope 以拍板后的语义为准）；
- 未处理消息与已抽取记忆有明确 freshness、source、watermark/partial 标识。

## 14. 需要产品确认的事项

以下产品语义必须在进入对应实现前定案；技术安全默认已经写入前文，不再作为开放选项：

| 决策 | 选项 | 推荐默认 | 不定案的后果 |
|---|---|---|---|
| Profile scope | user-global；或 project-scoped | **user-global**，并在 UI 明确说明；不修改 EverOS | `/get` 当前按 owner 单行读取，无法证明 project isolation（详见 §9.3） |
| `bounded` 成功标准 | flush 已明确成功；或 LanceDB 已可见 | **flush 明确成功**，结果仍标注 indexing eventual；不承诺立即可搜 | wait/overlay 语义漂移 |
| unknown 无 receipt | `manual_required`；或 at-least-once replay | **manual_required**，不自动 replay | 重复记忆与永久卡死之间无法安全选择 |
| 附件恢复 SLA | pin-before-accept 并纳入备份；或只保证 metadata | **pin-before-accept**，附件目录纳入一致性快照 | accepted capture 可能无法重放 |
| final flush 时 outbox 未 drain | 等待目标 session drain；或 fence 后续 add | **先短时等待 drain，超时后持久化 due 并在 fence 下阻止旧 generation add** | close 后 add 的消息可能漏 flush |
| in-flight flush 期间新 add | provider generation；或 Avibe session fence | **Avibe session fence**，因为当前 EverOS 不接受 generation ID | watermark 无法强制，settlement 可能串 generation |
| agentic search 显式启用 | 默认开启；或显式启用 | **显式启用**，受 §9.2.2 预算约束，详见 §9.2.1/§9.2.4 | 普通调用路径被昂贵能力污染 |
| agentic capability gating | 三 capability 缺失时 fallback；或 capability-unavailable | **capability-unavailable**，不 fallback，详见 §9.2.4 | 静默退化为 hybrid，无法定位能力缺失 |
| agentic 默认 timeout / max_model_calls / max_results / cost_budget_tokens | 取决于产品 | **30s / 4 / 20 / null**（默认值，§9.2.2） | 在 Phase 3 实现前需产品确认具体数字 |
| 第一版是否提供 bounded wait 用户操作 | 是；或仅内部接口 | **仅内部接口**（§5.2） | 用户在 UI 看不到可控等待 |

下列是非阻塞的产品/排期选择，采用推荐默认即可开始前置实现：

1. idle flush 默认 5 分钟，max-age 默认 30 分钟；
2. `/new`、archive 和明确 session close 都触发 final flush；
3. 第一版不提供“立即可检索”的 bounded wait 用户操作，只保留内部接口语义；
4. 第一版继续只捕获用户消息，不开启 assistant/tool/agent memory；
5. 已采纳：为 agentic search 配置 reranker，agentic 显式启用并受预算约束；本版不开启 assistant/tool/agent capture，不修改 EverOS；
6. embedding semantic rotation 初期允许停机，且在 journal/fence 未实现前不开放；
7. provider payload diagnostics 继续沿用现有默认开启行为，后续另做隐私决策；
8. EverOS 1.2.3 artifact 走独立发布轨道；
9. Cascade permanent data-quality failure 只进入 operator diagnostics，不映射全局 degraded。

## 15. 关键源码参考

### Avibe

- `core/memory/everos.py`：EverOS UDS HTTP adapter、搜索和错误映射；
- `core/memory/sidecar.py`：sidecar route/shape guard；
- `core/memory/process.py`：managed child、UDS 和生命周期；
- `core/memory/worker.py`：当前 per-message flush 和 queue drain；
- `core/memory/store.py`：capture queue、flush observation、boot recovery；
- `core/memory/module.py`：当前 Memory interface 和 status precedence；
- `core/memory/runtime.py`：controller-owned lifecycle、embedding guard 和 status payload；
- `core/memory/everos_insight/`：provider call recorder 和处理日志；
- `docs/plans/everos-1.2.1-upgrade.md`：已完成的 EverOS 1.2.1/project_id 升级历史；
- `docs/plans/memory-architecture-deepening.md`：已完成的 Memory 深模块重构历史。

### EverOS

- `CONTEXT.md`：集成领域术语；
- `EVEROS_INTEGRATION_zh.md`：集成契约和运维建议；
- `src/everos/service/memorize.py`：add/flush 调度；
- `src/everos/service/_boundary.py`：buffer、memcell 和 boundary 顺序；
- `src/everos/service/_session_lock.py`：同 session 并发语义；
- `src/everos/entrypoints/api/routes/health.py`：health/liveness/readiness；
- `src/everos/entrypoints/api/routes/metrics.py`：Prometheus endpoint；
- `src/everos/core/middleware/prometheus.py`：当前 HTTP metrics；
- `src/everos/entrypoints/cli/commands/cascade.py`：受控 projection rebuild；
- `src/everos/infra/persistence/sqlite/tables/unprocessed_buffer.py`：未处理消息；
- `src/everos/infra/persistence/sqlite/tables/memcell.py`：boundary 后原始对话归档；
- `src/everos/infra/persistence/sqlite/tables/cluster.py`：embedding-derived cluster centroid；
- `src/everos/memory/search/dto.py`：四类搜索方法和请求语义。
