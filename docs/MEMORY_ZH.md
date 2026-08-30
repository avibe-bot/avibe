# 记忆

本文是当前 Memory 系统唯一有效的产品与架构契约。已被取代的 Memory 设计方案只保留在
Git 历史中，不再代表当前行为。

Avibe 记忆会把符合条件的 Workbench 和私聊消息提炼为按范围隔离的用户与 Agent 画像、
事件和事实。在**设置 > 记忆**中可以查看状态、当前画像、搜索结果、设置和处理记录。

## 设计理念

重构后的 Memory 只有一个优先级：**先保证 Avibe 与 Agent 可用，再尽力保留 Memory
数据**。EverOS 作为隔离的子服务运行，Avibe 可以唤起、停止、替换，或在用户明确确认后
修复它。进程崩溃、重启、替换或过载时允许丢失尚未完成的 Memory 输入；系统追求大多数
正常场景不丢数据，而不承诺零丢失或 exactly-once 投递。

以下规则是架构不变量：

1. **Memory 不得拖垮主链路。** Memory 的延迟或故障不能让聊天、Agent、`/new`、归档、
   运行时替换或关机不可用；生命周期 offer 与 barrier 必须有界且不阻塞。
2. **接受不等于持久化。** 捕获被接受，只表示进入了有界的进程内工作，不代表 EverOS
   已经接收或持久化。Avibe 不维护持久 outbox、重放账本或逐调用投递工作流。
3. **资源与重试必须有界。** 准入、附件准备、提供方调用、待 flush 状态和重启尝试都有
   固定上限。容量耗尽时可以丢弃 Memory 工作，但主产品必须继续运行。
4. **EverOS 原生数据是唯一 Memory 内容事实源。** 搜索、画像、事件、事实和处理记录都
   投影已保留的原生数据。Avibe 不再维护平行的 Provider Call Log、关联账本或补造的
   历史记录。
5. **每项生命周期职责只有一个 owner。** `CaptureAdmission` 与 `MemoryModule` 负责
   准入；`MemoryRuntime` 负责产品策略；`BestEffortMemoryWriter` 负责易失投递；
   `EverOSSupervisor` 负责子进程归属和重启预算；`EverOSProcess` 只负责单次启动。
   调用方不能借用这些组件的内部状态。
6. **Wake 是唯一非破坏性可用路径。** 初始启动、手动重试、服务重启和崩溃恢复都复用
   有界 Wake。Wake 永不删除 Memory 数据，且在证明旧受管进程停止前不会启动替代进程。
7. **数据丢失必须得到明确授权。** 修复、删除数据和使身份失效的配置变更都要求精确的
   `confirm_loss: true`、停止证明和受限删除。遇到歧义时 fail closed，不扩大删除范围，
   也不静默回退。
8. **身份与诊断必须诚实。** 用户和 Agent owner 由服务端派生并互相隔离。原生证据缺失
   或不完整时明确报告 `partial` 或 `unavailable`；可能已经提交的结果绝不重放。

## 当前架构

平台适配器负责对原生事件分类，但不拥有 Memory 业务逻辑。它们把事件规范化为
`InboundTurnFacts`；`CaptureAdmission` 将这些事实视为不可信输入，在准入前重新校验身份、
平台、事件形状与设置。`MemoryModule` 负责已准入的产品操作及其范围化读取行为。

| 组件 | 唯一职责 |
| --- | --- |
| 平台适配器 | 分类原生事件并规范化 transport 事实，但不决定 Memory 业务准入。 |
| `CaptureAdmission` | 复核不可信的入站事实，并作出唯一的捕获准入决定。 |
| `MemoryModule` | 派生 owner/project 范围，提供已准入的捕获与读取语义，不暴露存储内部。 |
| `BestEffortMemoryWriter` | 负责有界、有序、易失的捕获投递和 flush 尝试。 |
| `MemoryRuntime` | 负责公开状态、配置策略、操作互斥和破坏性操作准入。 |
| `EverOSSupervisor` | 独占当前子进程、就绪状态、有界 Wake/重启恢复、停止证明和旧版本孤儿协调。 |
| `EverOSProcess` | 适配一次私有 EverOS 启动，包括进程身份、UDS 就绪、资源限制和终止。 |
| 原生读取器 | 在授权范围内投影 EverOS 画像、事件、事实、运行和索引状态，不创建第二事实源。 |

数据与生命周期路径保持分离且简短：

`符合条件的输入 -> CaptureAdmission -> MemoryModule -> 有界 writer -> 私有 EverOS UDS`

`MemoryRuntime -> EverOSSupervisor -> 一次 EverOSProcess 启动`

读取使用同一个私有 EverOS 服务及其当前原生数据根。`memory.sqlite` 保留稳定的 Avibe
身份与项目目录事实，以及一行有界的时间戳、捕获结果和处理故障诊断元数据；它不包含
Memory 消息载荷，也不承担投递队列或恢复状态机。后续章节定义这些边界上的具体产品行为。

## 用户与 Agent 记忆归属

自动捕获的用户消息仍以该用户自己的 Memory owner 为目标。`vibe memory remember`
接受请求后，会向 Avibe 为同一调用者派生的独立 Agent owner 发起尽力而为的进程内投递；
接受请求不保证提供方投递或持久化。该 ID 以 `-agent` 结尾，调用者不能自行指定，也不能
使用其他用户的 owner。用户捕获与 Agent 捕获使用互不相同的提供方 session，因此两者的
事件、事实和画像不会进入同一个 Memory cell。

搜索会同时读取两个 owner，并以**用户**、**Agent**或**两者**标注结果；两个 owner 中完全
相同的文本只返回一次，并标为**两者**。画像会显示为两个独立的带标签区块，不会合并。
设置页的已处理事件浏览器可明确切换**用户记忆**和 **Agent 记忆**；
`vibe memory list` 仍只展示用户 owner。

现有记忆不会迁移。归属拆分前由 Agent 记录的事实仍保留在用户 owner 下，并继续可搜索。
新版本保留稳定的 provider session 身份，但捕获投递只存在于进程内。升级、重启或回滚时
仍待处理的易失工作可能丢失；不会通过旧版队列重放，也不提供零丢失保证。

## IM 附件捕获

记忆可以从 Slack、Discord、Telegram、飞书/Lark 和微信已绑定的一对一会话中提取受支持
的附件。仅当记忆已启用，并且在**设置 > 记忆**中完整配置多模态 LLM 端点后，此能力才
可用；它不会改变发送给 Agent 的文件。

只有人类直接分享的普通文件才符合条件。机器人、系统、转发、编辑、引用、富内容和无法
识别的原生消息形状都会被排除。随后，Avibe 使用同一套格式与内容策略检查每个符合条件
的文件。支持纯文本、Markdown、CSV/TSV、VTT、PDF、位图图片、音频、HTML、EML，以及在已安装
LibreOffice 时的 Office / iWork / ODF / RTF 文档。原生 Windows 当前会跳过这些 Office
格式；Linux 与 macOS 可通过 Memory 子进程能够访问的 LibreOffice 安装来启用。SVG 和视频仍不支持。
不支持或格式异常的文件会被单独跳过，符合条件的文本和其它有效附件仍可进入捕获。

每轮最多捕获 8 个附件，单个附件最大 25 MiB，总计最大 100 MiB。通过准入的文件会复制
到 Avibe 私有存储，并保留到本次进程内投递完成。提取后的内容可能发送给已配置的多模态
提供方，因此请按数据处理要求配置该端点。清理记忆数据会删除本机仍保留的附件包，但
无法删除提供方已经接收的副本。

## 已处理事件列表

`vibe memory list` 只读取当前范围内用户和项目的有效、活跃、已处理事件，不包含画像、
Agent 记忆、原子事实、未处理消息或已被取代的事件。单项目读取严格使用 EverOS 从 1
开始的页码，并固定按时间倒序排列。经过验证的设置页可通过 Avibe 生成的有界、带版本
cursor 聚合同一用户的多个项目；Agent CLI 不能请求 `all`。JSON 会保留提供方的不透明
entry id，作为未来检查入口。列表读取不会把 Search/Get 负载写入提供方诊断，也不会调用
LLM、Embedding 或重排序服务。

在**设置 > 记忆 > 搜索**中留空查询，即可按最新优先浏览这些事件。可以选择单个项目或
**我的全部项目**，并明确选择**用户记忆**或 **Agent 记忆**；通过事件摘要下方的页码
切换页面，再选择一行打开完整详情。点击**条目 ID** 芯片会复制提供方的不透明标识符。
输入任意非空查询后，同一标签页会切回跨两个 owner、按相关性排序的搜索结果。

## 可选重排序端点

**设置 > 记忆**中的第三个处理端点是可选项。选择一个 EverOS 重排序提供方
（`deepinfra`、`vllm` 或 `dashscope`），并同时配置该提供方的 Base URL、模型与 API Key。
添加或更改端点会在保存前执行提供方对应的有界预检。旧配置若没有 `provider`，默认沿用
DeepInfra 协议；但省略 `provider` 且主机为 Bailian workspace（`*.maas.aliyuncs.com`）时，
会推断为 DashScope。所有字段留空时继续使用标准记忆搜索层级。移除端点会同时清除提供方
和端点字段，但不会重建 Embedding 索引。DashScope 当前只支持 `gte-rerank-v2`，Base URL
必须是 `https://dashscope.aliyuncs.com` 或 Bailian workspace 主机。

## 处理记录

处理记录是按调用者隔离的有界 EverOS 原生数据视图，只覆盖所选项目以及调用者的用户和
Agent owner。详情可以展示已授权的原始载荷、保留的原生运行、关联的情节与原子事实，以及
当前画像和索引状态。当前状态明确标记为无法归因，并不是该次运行的历史状态还原。

各来源独立读取；缺失、忙碌、格式异常或已被保留策略移除的数据会显示为不可用，其他部分
仍可显示。这是一项尽力而为的诊断：Avibe 不保留持久化逐调用观察器、重放队列或 Provider
Call Log，记录可以不完整或丢失。包含 `memory.diagnostics.log_provider_calls` 的已发布配置仍可
加载，但该字段会被忽略，新的配置和 API 序列化也不会再输出它。

处理故障只写入 Avibe 主服务日志；捕获丢失不会通过 IM 发送管理员消息。

## 尽力而为的捕获

捕获是易失且尽力而为的能力。进程内 writer 在附件固定、排队、提供方调用和终结清理
之间共享固定的 256 个许可，并维护 256 项 source-message LRU。单个有序 worker 使用
有界的 PendingFlush 跟踪器（最多 256 个 session、每个 100 个消息 ID）；空闲、年龄和
数量阈值固定为 5 分钟、30 分钟和 100 次确认。

持久化仅包含元数据。`memory.sqlite` 保存安装 scope key、epoch、provider-root id、提供方
时间戳水位线、项目目录、捕获摘要（`missed_count`、`last_success_at`、`last_error` 及其
时间戳）和有界的处理故障诊断。这些字段只概括本地状态，不包含消息载荷或逐调用投递
工作流。v4 之后只有 `memory_meta` 与 `memory_projects` 两张应用表。v0-v3 迁移会保留身份
与项目事实，并从旧捕获数据推导项目行；队列、租约、结算、附件引用和恢复表会在没有
提供方 I/O 的情况下丢弃。

生命周期 offer 和 barrier 都不会阻塞。`/new`、归档、运行时替换和关机不会等待捕获
投递；运行时 authority 被撤销后仍在准备的捕获可能丢失或失效。替换和关机
会主动丢弃进程内易失工作。

add/flush 最多尝试三次，只有明确证明在提供方执行前失败时才重试。可能已经提交的
结果绝不重放；系统复用现有 sidecar stop/reap，无法证明终止时 Memory 保持 fail closed。
附件 pin/cleanup 也受同一许可限制；隔离的清理失败会关闭本次运行时的附件准入，但
非空 caption 仍可退化为纯文本。只有提供方明确证明附件没有写入时，才允许同样的退化。

## 运行状态与恢复

API、CLI 与设置界面共用一个运行状态和一个简短原因。状态只能是 `disabled`、
`starting`、`running`、`degraded` 或 `needs_repair`。Memory 不可用不会让 Avibe、
Agent 或聊天不可用；相互冲突的生命周期操作由同一个进程级锁拒绝。

内部的 **Wake** 是普通的非破坏性可用路径。它会校验已准入的 `memory-runtime` 工件，
必要时重新安装，证明旧进程已经停止，再使用同一个 EverOS 数据根启动。服务启动和子进程
意外退出也使用相同路径；就绪检查采用有界退避和 EverOS 原生 health 路径。Wake 永远不会
删除或重建 Memory 数据。

设置界面按用户意图命名这条路径：`degraded` 时显示 **重试启动**；`running` 时在
**更多操作** 中提供 **重启记忆服务**。两者都复用同一条非破坏性 Wake 路径。

提供方、凭据、磁盘或权限故障只会进入 `degraded` 并显示脱敏原因。修正外部条件后选择
**重试启动**；这些故障不会开启或自动转入破坏性的修复。

**修复**只在 `needs_repair` 时提供，即本机原生数据根确实不可用或不兼容。界面、公开
API、内部 API、客户端与 Controller 每一层都要求精确的 `confirm_loss: true` 字段。随后：

1. 获取 Memory 操作锁，并证明旧的受管进程树已停止；
2. 保留 Memory 设置、凭据、稳定 scope 身份和项目目录，只轮换 provider 数据 generation；
3. 仅删除受限的 `<effective_home>/memory` 根及名称固定的废弃恢复残留；
4. 复用非破坏性启动路径，并且只有 EverOS 原生就绪检查成功后才报告成功。

若无法证明进程归属或终止，修复不会删除任何数据。若受限删除部分失败或遇到不安全内容，
响应会如实报告剩余表面并保持 `needs_repair`。修复失败或中断后不会保存待续跑的阶段；下次
启动会重新评估数据根，用户可以重新明确确认并从头执行修复。

**删除数据**是独立的用户意图，有独立响应和同样的丢失确认。它复用修复的先停进程与受限
删除原语，但不要求事先进入 `needs_repair`。这不是安全擦除，也无法删除原始 Avibe 聊天或
远程提供方已接收的副本。

Embedding 身份变更会使原生数据根失效，因此保存时使用同一个明确接受丢失的边界与统一重置。
系统不再保留候选配置、重建标记、重试阶段，也不会静默回退到旧设置。

当一个正常工作的 custom 安装首次获得组织托管的 Memory 能力时，Avibe 会持久化
`cloud.transition_notice_pending` 作为确认围栏，并在用户明确确认身份变更重置前继续选择
当前 custom 来源。该字段只记录待处理的用户决定，不表示捕获投递或可续跑的恢复进度，
也不承诺在等待确认期间保留捕获。

已发布的 `recovery_intent`、`embedding_change_pending`、`transition_rebuild_owned` 与 Clear
状态仅作兼容输入，其已退役执行阶段不会再次序列化或续跑。不安全的兼容证据会折叠为
内部 `repair_required` 围栏；普通保存会保留该围栏，直到一次成功的破坏性修复将其清除。

## 召回策略

召回使用一套封闭策略，模式只能是 `auto`、`keyword`、`vector`、`hybrid` 或
`agentic`。只有最近一次可信的 EverOS health 明确报告 Embedding 能力时，`auto` 才选择
hybrid，否则使用 keyword。显式请求 vector 或 hybrid 但能力缺失时 fail closed。一次
请求最多调用一次提供方搜索，不会换模式重试。

Agentic 召回只通过 CLI 提供，并且要求 health 明确报告 Embedding、LLM 与 rerank 能力，
且没有关闭 `agentic_search`。它只发起一次请求，由 sidecar 执行最长 30 秒的墙钟超时；
能力不可用时 fail closed。EverOS 1.2.3 尚不强制模型调用次数和 token 上限，因此这些策略
字段目前只是声明式边界，而不是 Avibe 独立执行的提供方预算。

当前 session overlay 只能使用运行时提供的可信调用者 session；调用者不能传入任意提供方
filter 或 session ID。
