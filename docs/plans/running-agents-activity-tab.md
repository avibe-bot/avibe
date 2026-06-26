# 运行中 Agent 视图（Agents → Running）— 需求设计稿 v3

> 状态：**已定稿（需求层面）**。两轮评审收敛（opus 4.8：feasible-with-changes → minor-fixes-then-done）；§8 全部分叉已拍板。可据此进入实现规划（接口签名/落点）。
> **已锁决策**：A4（Agents 页内 `Definitions | Running` 子 Tab，A2 顶层 Activity 作兜底）· B1 轮询 · C1 只读 · D2 含 idle · E1 覆盖 IM · F1 session 中心 · G1 含 OpenCode · H1 orphan 单独成行 · I1 不可达明确态 · **J1 默认进 Definitions** · **K2 Running badge 仅计 active**。
> 范围：**需求 / 产品层面**。不含实现代码；定义"要什么、长什么样、数据从哪来、有哪些待拍板分叉"。

## 1. 背景与问题

avibe 现在能看到 agent **定义**（Workbench → Agents）、定时与触发 **Harness**（Tasks/Watches/Webhooks/Runs 历史），但**看不到"此刻正在运行的 Agent"**：

- 我现在有哪些 Claude Code / Codex / OpenCode 进程或会话在跑？
- 每个挂在哪个 session 上？这个 session 是 slack / discord / web / 定时触发哪种来源？挂在哪个 channel/thread/项目、哪个 workdir？
- 想点进去看它、跳到对应会话、（可选）停它怎么办？

数据基本都已存在，但分散在三层且跨进程，从未被聚合成一个"运行时视图"。

## 2. 目标 / 非目标

**目标**
1. 一眼看清"当前正在运行/活跃"的所有 agent 实例，**覆盖全部三个活后端：claude / codex / opencode**（含未来后端，靠统一抽象自动纳入）。
2. 每行能看到关键上下文：来源平台、触发源、所属 session、scope、workdir、模型、运行时长、状态。
3. 每行提供可点击链接/操作：跳 Chat session、查看关联 Harness run、查看 scope、（可选）停止/驱逐。
4. 嵌入现有 Web IA，移动端可用，且在 controller 不可达时优雅降级（不报 500、不假报"0 在跑"）。

**非目标（本期不做）**：历史分析/图表（Runs 已有）；改 eviction/reaper 回收逻辑（只读快照）；token/费用统计。

## 3. 信息架构：放哪？（§8-A 待拍板，评审倾向已变）

现有结构：Workbench `CapabilityTabs`：`Agents | Skills | Harness | Vaults`；`HarnessPage` 内部 Tab：`Tasks | Watches | Webhooks | Runs`。

**已定（A4）**：在 **`AgentsPage` 内新增子 Tab `Definitions | Running`**。心智模型是"定义 vs 运行实例"（类比 镜像/容器、Deployment/Pod）：`Definitions` = 现有 agent 配置（claude/codex/opencode 后端、系统提示、模型），`Running` = 此刻活着的 session/进程，通过 `agent_sessions.agent_backend` 关联回所用后端。

实现注意：
- AgentsPage 现仅用顶层 `CapabilityTabs` + `WorkbenchPageHeader`，**没有内部子 Tab 机制**，需照 `HarnessPage` 的 `TabKey`/`TAB_ORDER`/counts/refresh/icon 模式新加一层子 Tab。
- **无需任何路由改动**：`/agents` 仍是单一 route，子 Tab 纯属组件内部 `useState`（与 `HarnessPage.tsx:67-90` 的 `TabKey`/`TAB_ORDER` 完全同构）；把现有 AgentsPage 主体抽成 `<DefinitionsTab>`，新增 `<RunningTab>`，外面套 HarnessPage 式 tab row 即可。
- `Running` 行是 **session 中心**（非"某定义的严格实例"）；可按 backend/agent 分组与筛选，但不强行把每行绑回某条定义。
- 评审曾倾向顶层 `Activity` Tab（理由：运行视图还含从不进 harness 的交互式会话，语义更宽）。**该理由不否决 A4**（一切在跑的都是"agent 在跑"，放 Agents 自洽），故 A4 成立；仅当深评审发现子 Tab 机制或定义↔实例映射出现 blocker 时回退 **A2 顶层 Activity Tab**。

## 4. 实体模型：一行 = 一个"运行中单元"

行的粒度本身是一个**待拍板分叉（§8-F）**：session 中心 vs 进程/transport 中心。下表按"session 中心、pid 作为属性"的推荐口径给出字段：

| 字段 | 含义 | 来源与注意 |
| --- | --- | --- |
| backend | claude / codex / opencode | 内存 registry。opencode 无自有子进程（HTTP server + poll loop），**无 pid** |
| state | `active`(在途轮次) / `idle`(已连接空转) / `orphan`(脱钩进程) | 每后端 liveness 源不同，须各自明确定义（§7） |
| busy_for / idle_for | 当前轮次时长 / 空转时长 | **必须在 controller 内算成"已过秒数"再传**；`monotonic()` 不可跨进程 |
| **platform** | slack / discord / telegram / feishu / wechat / avibe(web) | `scopes.platform`；**"cron"不是 platform** |
| **trigger source** | human / scheduled / watch / webhook / callback | harness 的 `source_kind`（与 platform 正交，单列）。**兜底**：无可靠关联 Harness run 时默认 `human`；scheduled/watch/webhook/callback 仅在能可靠关联到 run 时显示 |
| scope | channel / thread / dm / project + 显示名 | `scopes.scope_type` + `display_name`；cron/web 可能 `scope_id=NULL`，则平台回退用 anchor 前缀 |
| session | base_session_id + 标题 | composite_key 拆分（注意 subagent key 为 `{platform}_{thread}:{agent}:{cwd}` 三段，须按最后一段绝对路径切分）+ `agent_sessions.title` |
| workdir | 工作目录 | composite_key / `agent_sessions.workdir`（展示可缩略，见安全 §7） |
| model | 当前模型（**可选/best-effort**） | claude `client._vibe_current_model` 可能为 None；不保证有值 |
| pid | OS 进程号 + 墙钟运行时长（**可选**） | claude：reaper `ClaudeOwnedProcess`，**仅 `owner=="session"`**（排除 auth 进程）；codex：**一个 pid 对应一个 cwd，多 session 共享**；opencode：无 pid |
| links | 见 §5 | 组合 |

**"活"的口径（关键）**：以**内存 registry + 实际进程**为准（此刻真在跑）。`agent_sessions.agent_status` **只对 workbench/web session 写入**，IM session 不写，故 DB 不能作可靠兜底——IM 在跑的真相只能来自 controller 内存快照。

## 5. 关联上下文 & 链接（每行可点）

- **→ 打开 Chat**：跳 `/chat/:sessionId`。**链接可用性规则（v4 修订）**：只要有持久化 `session_id` 就提供 Open Chat——**IM（slack/discord/…）session 同样可用**，与 Inbox/侧栏既有的 `/chat/<session_id>` 导航一致。无 `session_id`（如 orphan）时**直接不渲染链接，不显示 "unavailable" 占位**。
- **→ 查看 Scope**：IM → channel/thread；web → 项目。
- **→ 关联 Harness**：若来自某 task/watch（`agent_runs.definition_id` / `source_kind`），链到定义与该次 run。
- **→ 进程详情**：pid、墙钟启动时间、native_session_id、是否 orphan。
- **操作（可选，§8-C，C1 默认不做）**：Stop 轮次（复用 `POST /internal/cancel/{session_id}` 即 `internal_client.cancel_dispatch`）/ Evict 空闲 session / Reveal workdir。

## 6. 交互与状态

- **实时刷新**：轮询 3–5s（B1）。计数 badge **只数 active（在途轮次，K2）**，idle 不计；**socket 不可达时 badge 显示 "—" 而非 "0"**。
- **排序/筛选（v4 修订：稳定排序）**：先按 state（active→idle→orphan），**band 内按稳定标识排序（非 elapsed）**，避免时长每轮跳动导致行乱跳；可按 backend / platform / source / state 筛选。
- **状态机**：加载态 / 空态（"当前没有 Agent 在运行"）/ **controller 不可达态（"运行时不可达"，区别于空态）** / orphan 高亮（只提示不自动回收）。
- **移动端**：行卡片化，`backend·platform·state·busy_for` 优先，其余折叠。

## 7. 数据来源 & 跨进程取数（佐证可行性 + 实现骨架）

**架构前提（第 1 轮评审硬伤修正）**：UI 服务（`vibe/ui_server.py`，FastAPI）与 controller 是**两个进程**；活 registry 都在 controller 内存，UI 的 `/api/...` 读不到。唯一跨进程通道是 controller 的本地 Unix socket（`core/internal_server.py`，已有 `/internal/turn-state`、`/internal/cancel` 等，`0o600` 仅本机）。

**取数链路（实现骨架）**：
1. controller 侧新增 `GET /internal/running-agents`（与 controller 同 asyncio loop，直接读内存；在此处完成 DB join 与时长计算后再返回）：
   - claude：扫 `session_handler.claude_sessions.values()`，读 `_vibe_runtime_base_session_id` / `_vibe_native_session_id` / `get_claude_client_pid(client)`；**注意 `active_sessions`、`session_last_activity` 以 `composite_key` 为键（非 anchor）**，须先经 client 的 `_vibe_runtime_session_key`/`_vibe_runtime_base_session_id` 把 composite_key 解析到 base_session_id 再做 DB join（`session_anchor=base_session_id`）；与 reaper `claude_processes.json` 对账标 orphan（仅 `owner=="session"`，排除 auth 进程）。
   - codex：`_session_mgr.all_base_sessions()` 遍历 → `get_cwd(bid)` → `get_thread_id(bid)`、`_turn_registry.get_active_turn(bid)`；pid 经 `_transports[cwd]` 取。**`CodexTransport` 目前只有 `is_alive`/`is_initialized` 公开，`_process` 私有——需新增公开 pid 访问器**；pid 是 cwd 级、多对一（多 session 共享）。
   - opencode：状态来自 `OpenCodeAgent.runtime_turn_keys()` 与 `_active_requests` 任务（HTTP server + poll，**无子进程 pid**）。
   - 跨后端在途轮次：`core/session_turns.py` 的 `in_flight`（键为 workbench `session_id` UUID）。
   - 显示元数据 join：SQLite `agent_sessions`（backend / workdir / title / scope_id，按 `session_anchor=base_session_id` 关联）+ `scopes`（platform / scope_type / display_name）。
2. `vibe/internal_client.py` 新增 `list_running_agents()`，捕获 `InternalServerUnavailable`。
3. `vibe/ui_server.py` 新增 `GET /api/running-agents` 薄代理（沿用现有 AuthGuard 鉴权；controller 不可达时返回明确的 unreachable 而非 500）。
4. UI 侧（已定 A4）：在 `AgentsPage.tsx` 内照 `HarnessPage.tsx:67` 的 `TabKey`/`TAB_ORDER`/`useState` 模式新增 `Definitions | Running` 子 Tab——把现有主体抽成 `<DefinitionsTab>`、新增 `<RunningTab>`，无路由改动。
5. CLI 侧可加 `vibe ps` 复用同一 `/internal/running-agents`。

**registry 路径修正**：`{config.paths.get_runtime_dir()}/claude_processes.json`（解析到 `~/.avibe/` 下），**非** `~/.vibe-remote/`。

**安全**：pid / 绝对 workdir / native_session_id 经浏览器 API 暴露需明确沿用 `/api` 现有鉴权；workdir 是否全量展示见 §8。

## 8. 偏好分叉 — 已决策（2026-06-24）

> 全部已拍板：**A4**（兜底 A2）· **B1** · **C1** · **D2** · **E1** · **F1** · **G1** · **H1** · **I1** · **J1** · **K2**。下列保留选项与理由备查。

### 8-A. 视图落点（IA）
- A1：`Harness → Live` 子 Tab（改动最小）。
- **A2（评审倾向）**：新增顶层 Capability Tab `Activity`（语义最贴切：覆盖交互式 + 自动化全部来源）。
- A3：放进 `Admin → Dashboard` 运维小部件。
- A4：`Agents` 页内 `Definitions | Running` 两子 Tab。

### 8-B. 刷新机制
- B1：轮询 3–5s（简单够用）。
- B2：SSE 实时推送（更实时、成本更高）。

### 8-C. 操作能力（v6 修订：统一 End）
- ~~C1：纯只读~~ → ~~C2：仅 Stop~~ → **C3（已采纳）：每行统一 "End"**，按 state 分派后端拆除：
  - **active** → 中断在途轮次 + 断开 client（claude `client.interrupt()`+`cleanup_session`；codex `turn/interrupt`+清 thread/turn；opencode abort+cancel task）。文案 "Stop"。
  - **idle** → 断开释放 client/transport。文案 "Disconnect"（无损可重建，免确认）。
  - **orphan** → SIGTERM→SIGKILL 泄漏进程（**先复用 reaper 校验：owner==session + 存活 + 启动时间匹配**，绝不杀非 avibe-owned/复用 pid）。文案 "Kill process"。
- 链路：`POST /api/running-agents/end` → `/internal/running-agents/end`（**在事件循环上跑**，因要 await 后端中断 + 改 loop 拥有的 registry，不能 to_thread）→ `end_running_agent(controller, target)` 分派。
- **破坏性二次确认**：active/orphan 首点 arm（按钮变 "确认"，3s 自动解除），再点执行；idle 立即。
- **无自杀护栏**（产品决策）：可终止当前会话 / avibe 自身；误杀手动重启。

### 8-D. "运行中"纳入范围
- D1：只显示真正持有活进程/在途轮次的实例。
- D2：也显示"已连接但空闲（idle）"的 session，便于了解资源占用。

### 8-E. IM session 可见性（评审新增）
IM（slack/discord…）的 `agent_status` 不写 DB，要显示其"在跑"必须完全依赖 controller 内存快照（实现更重）。
- E1：完整覆盖 IM 在跑 session（实现更重，但满足 Goal 1）。
- E2：暂不覆盖 IM（更轻，但与 Goal 1 矛盾）。

### 8-F. 行粒度（评审新增，核心建模分叉）
Codex 一个 transport（pid）对应一个 cwd、服务多 session。
- F1（推荐）：**session 中心**——一行一个活 session，pid 作属性；多 session 共享同一 codex pid 时各自成行并注明。
- F2：**进程/transport 中心**——一行一个 OS 进程/transport，pid 维度聚合。

### 8-G. OpenCode 是否纳入 v1（评审新增）
OpenCode 已是一等活后端（进程模型不同、无 pid）。
- G1（推荐）：v1 纳入，pid 列对 opencode 留空。
- G2：v1 显式延后，仅 claude/codex（须在 Goal 1 注明缺口）。

### 8-H. orphan 进程展示策略（已定 H1 + v4 存活校验修订）
- H1（已定）：脱钩进程单独成行标 orphan。
- **v4 关键修订**：orphan **必须做存活+身份校验**——只展示 pid **当前确实存活**且启动时间与注册表 `started_at` 匹配（±1s，防 pid 复用）的进程。原实现只读 `claude_processes.json`（累积大量历史死 pid），会把早已退出的死 pid 当 orphan 刷屏；现复用 reaper 的 `_process_start_time` 过滤死/复用 pid。修订后多数时候 orphan 为 0，仅真有存活泄漏时出现。

### 8-I. controller 不可达时的 UX（评审新增）
- I1：显示"运行时不可达"明确态（推荐）。
- I2：显示上次已知 + 过期横幅。
- I3：显示空。

### 8-J. 子 Tab 默认落地（已定 J1）
点 `/agents` 默认进 **`Definitions`**（保留现有肌肉记忆）；`Running` 为并列项。

### 8-K. Running 子 Tab 计数 badge 口径（已定 K2）
Running 子 Tab 的 badge **仅计 `active`（在途轮次）**；idle 实例仍在列表中可见但不计入 badge。须与 §6 口径统一：列表含 idle（D2），但所有"在跑计数"badge 一律只数 active。

## 9. 验收标准（草案）
1. 有真实运行的 claude/codex/opencode 时，Tab 内列出对应行，platform/source/session/workdir/pid（如有）正确。
2. 点击链接正确跳 Chat / Harness run / scope。
3. 进程结束后该行在一个刷新周期内消失。**只读观察口径**：stuck-active 被既有 eviction 驱逐后，其行随之消失——视图只观察既有驱逐结果，不自行驱逐（C1）。
3b. 多个 codex session 共享同一 app-server pid 时（F1），各 session 各自成行、显示相同 pid 并标"共享进程"。
3c. controller 重启后"仅 reaper registry 有进程、内存 session 丢失"时，按 H1 显示为 **orphan 行**，其 session/scope/Chat 链接为空或 best-effort；badge 仍只统计 active。
4. controller 不可达时显示明确"运行时不可达"态、不报 500、badge 显示 "—"。
5. 移动端可读可用；空态/加载态/出错态完整。

## 附：评审日志
- **Round 1**（opus 4.8 + codex-expert\*）：双方均 feasible-with-changes。已折入：两进程取数链路、registry 路径、platform/source 拆分、monotonic→时长、codex pid 语义、agent_status 不可靠、OpenCode 纳入、安全与降级。新增分叉 8-E~8-I。
- **Round 2**（opus 4.8）：**minor-fixes-then-done**，A4 可行无需回退。已折入：composite_key 与 base_session_id 解析、CodexTransport 需公开 pid 访问器、opencode 状态源（runtime_turn_keys/_active_requests，无 pid）、子 Tab 无需路由改动、codex 共享 pid 验收项。新增微分叉 8-J/8-K（已定 J1/K2）。
- **Round 3a**（opus 4.8）：**minor-fixes-then-done，无新分叉**。修：§7 step4 去除 A1/A2 死代码改为 A4 目标、`/internal/cancel/{session_id}` 路径精确化。
- **Round 3b**（codex 后端，小上下文）：**minor-fixes-then-done，NEW_HUMAN_FORKS=none**。修：§5 链接可用性规则（失效链接置灰/隐藏）、§4 trigger source 兜底（无关联默认 human）、§9 stuck-active 改为只读观察口径、controller 重启残留进程按 orphan 显示。
- **✅ 收敛定稿**：codex + opus **双评审一致 minor-fixes-then-done / 无遗留 blocker / 无新人工分叉**，全部修正已折入。需求设计完成。
  - \* codex 评审取数过程：`codex-expert`(OpenAI Codex) 本区域不可用；用户配置的 codex 后端偶发 **403 直连 api.openai.com(HKG)**，且**读多文件的大型评审会触发 remote-compact，被中转上游以 `403 This account only allows Codex official clients` 拒绝**。最终改用「不读文件、doc 正文内联」的小上下文单回合评审成功跑通。**附带发现**：上述 403（偶发直连 OpenAI + compact 被禁）很可能也是用户 codex 后端反复失败/"授权失效"的根因，建议单列排查 `model_provider`/`base_url` 路由与 compact 行为。
