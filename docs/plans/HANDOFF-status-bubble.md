# HANDOFF — 状态条 P0 实现（断点续传用）

> 给"被清空上下文后的我 / 接力者"看。读完这份 + `agent-progress-status-bubble.md`(完整 spec) 就能无损继续。
> **恢复方法**：先 `git -C <worktree> diff` 看已改动，再看下方"进度"与"任务清单(TaskList)"，从第一个未完成任务继续。

## 环境
- Worktree：`/essd/qiqi/code/dev/avibe/.agents/worktrees/agent-progress-status-bubble`
- 分支：`feat/agent-progress-status-bubble`，基于 `alex/master`（remote `alex` = github.com/avibe-bot/avibe）。
- 完整设计：`docs/plans/agent-progress-status-bubble.md`（已定稿，含 §1-§12 + 评审折回）。
- 工作目录命令一律用绝对路径或 `git -C`，避免 cd 权限弹窗。

## 目标（两诉求）
1. 不污染消息列表：一轮跑完只剩一条结果，无过程残渣。
2. 推理状态可感知：长时间无新内容也能看出没挂。
仅 **Slack / Discord**（支持编辑）。其余平台回退现状、行为 byte-identical。

## 锁定决策（不要再推翻）
- concise = Slack/Discord **首版直接默认**。
- **Edit 优先**：状态条 edit 成 Result；Delete 仅"过程溢出成2+条"时删多余的。
- 过程状态条 = **单行替换**（不 append/不 split/不 continued-below）；正文 = 当前动作标签。
- 持久化不变：store 仍存完整 process 行 + result 行；可视清理 ≠ 删数据。
- 计时是 **P1** 心跳驱动；**P0 不带计时**（静态 `⏳ 思考中` / `🔧 <动作>`）。
- 可感知=分层：进程心跳(已运行计时) + 后端存活探针 + 无输出提示 + ⏳ 保留（P1）。

## 评审必守（codex 两轮）
- **B1** 后端探针按后端分流：Claude `controller.receiver_tasks[key].done()`；Codex `CodexTransport.is_alive`(transport.py:157-164)。心跳落 agent 内、对外暴露 `backend_alive(context)->bool`。(P1)
- **B2** concise 下抑制 processing_indicator 的 ack 消息/reaction，仅留 typing；注意 Slack `supports_message_indicator_delete=False`，ack_mode=message 时不抑制会永久残留。
- **B3** result 6 路径只有 inline 与 edit 干净；其余(split/summary+.md/fallback/失败通知)在另发后必须清理孤儿状态条。
- **S1** ✅已做：`base_formatter.to_status_label()` 安全清洗(去反引号/首行/截断)。
- **S2** 新增 `_consolidated_overflow_ids: dict[str,list[str]]`，并在 `_clear_consolidated_state` 清。
- **S3** 取消心跳(await)→再 edit result，**不能塞 finally**；stop/error 路径也取消。(P1)
- **S4** Slack `edit_message` 返回 **bool**！edit 成功后 `primary_message_id = 已知气泡 id`，不可取返回值。
- **S5** dispatcher 侧 per-message `asyncio.Lock` 字典，把 cancel-heartbeat→edit-result 整段原子化。
- N1 last_activity 按 turn-key 存；N3 P0 无计时；N4 心跳 edit 包 `try/except CancelledError`。

## 关键落点（worktree 实测行号）
- `core/message_dispatcher.py`
  - `ConsolidatedMessageDispatcher.__init__` 状态字典 @114-117
  - `emit_agent_message` @539；result 分支 @697-891；process 块(assistant/toolcall/system) @893-1027
  - `_clear_consolidated_state` @225-230；`_get_consolidated_message_key` 附近
  - `_supports_message_editing` @420；`_send_unconsolidated_log_message` @517
  - split/threshold helpers @359-377
- `modules/im/formatters/base_formatter.py`：`to_status_label`(已加，顶部)、`format_toolcall`@374
- `modules/im/slack.py` / `modules/im/discord.py`：`edit_message`（Slack 返回 bool）、带按钮 edit、typing
- `core/processing_indicator.py`：`start()` / `_candidate_modes()`（B2 抑制点）
- `modules/agents/claude_agent.py`：`handle_message`@79、`receiver_tasks` 填充@123-126、toolcall emit@~611

## 实现切片（= TaskList）
- [x] T1 落点核对
- [x] T2 `to_status_label` helper + 实测
- [ ] T3 过程路径→单状态条一行替换（concise，Slack/Discord）
- [ ] T4 edit_message 带按钮 + dispatcher per-message 锁（S4/S5）
- [ ] T5 result 情况1 edit-into-result + 孤儿清理(B3) + indicator 抑制(B2)
- [ ] T6 单测 + ruff
- 后续 P1：心跳/计时/后端探针/无输出提示；P1 长结果切分首段 edit；P2 溢出删多余条。

## 如何判断 concise 生效（实现内部用）
平台是否 slack/discord + `_supports_message_editing` 为真 + progress_style!=off。建议加一个 `_use_concise_status(context)` 私有判定，集中口径。

## 测试 & lint
- 单测：`python3 -m pytest tests/<file> -q`（worktree 内）。新测放 `tests/`。
- lint：`ruff check <changed.py>`（push 前必跑）。
- 不重启本地 vibe；跨平台人工验证用 Incus（用户触发"回归测试"时）。

## 提交规范
- commit：`type(scope): summary`。先不 push，除非用户要求。已在 feature 分支。

## 进度日志（每步追加）
- 2026-06-24: 建 worktree(alex/master) + 带入 spec + 落点核对 + `to_status_label` 落地实测通过。下一步 T3。
- 2026-06-24: **P0 核心完成**（未提交，feature 分支工作区）。
  - `base_formatter.to_status_label()`：安全单行标签（去反引号/首行/截断，保留下划线）。
  - `message_dispatcher`：新增 `_progress_style`/`_concise_progress_style`/`_render_concise_status`/`_concise_status_bubble_id`/`_edit_bubble_into_result`/`_tidy_orphan_status_bubble`；`__init__` 加 `_consolidated_overflow_ids`；`_clear_consolidated_state` 清 overflow。
  - process 块：Slack/Discord 走单状态条**一行替换**（concise 默认；off=无气泡；verbose/其他平台=旧路径不变）。
  - result 分支：**case1 edit-into-result**（S4 用已知气泡 id），非 inline/edit 失败时**孤儿气泡收成 ✅**(B3)。**踩坑修复**：曾把原 `if/elif/else` 投递链拆成两个 `if` 导致 consumed=True 时落到 summary 误发第二条——已改成 `if status_bubble_consumed: pass / elif ...` 重新接好链。
  - 测试：`tests/test_message_dispatcher_status_bubble.py` 10 passed；既有 dispatcher 套件 61 passed（无回归）；ruff 全过。
  - 验证用 python：`/essd/qiqi/code/dev/avibe/.venv/bin/python -m pytest <file> -q`（worktree 内 `uv run` 会因缺 ui/dist 构建失败，别用）。
- 2026-06-24: **P0 已 commit** `e72cb385`。
- 2026-06-24: **P1 完成**（未提交→见下条）。
  - `message_dispatcher`：注入时钟 `self._now`(可测)；per-turn `_status_started_at`/`_status_last_activity_at`/`_status_heartbeat_tasks`。
  - footer：`_format_elapsed`/`_status_footer_text`(运行 `⏳ m:ss`、完成 `✅`、后端死 `⚠️`、久无输出提示)/`_compose_with_footer`(Discord `-#`、Slack 斜体小字)/`_compose_status_message`。
  - 心跳：`_start_status_heartbeat`/`_status_heartbeat_loop`(~30s, turn-token+bubble 守卫, `CancelledError` 安全)/`_status_heartbeat_render_once`(走 consolidated lock 串行 render↔heartbeat = S5 等效)/`_stop_status_heartbeat`。
  - **S3**：result 分支在任何 edit 前 `await _stop_status_heartbeat(key)`；`_clear_consolidated_state` 也先停心跳(在取锁前，避免与 heartbeat render 抢同一 lock 死锁)再清 timing。
  - **完成 footer**：edit-into-result 用 `_compose_status_message(done=True, result_body=display_text)` → `答案\n\n_✅ m:ss_`；孤儿 tidy → `✅ m:ss`。持久化/SSE 仍用干净 display_text（footer 仅在 IM edit）。
  - **B1 探针 hook**：`_backend_dead`→`controller.backend_alive(context)`（None/缺失=不判死，安全）。**注意：controller.backend_alive 尚未实现** → ⚠️ 暂不会真正触发；需后续在 controller 落地按后端分流(Claude `receiver_tasks[key].done()` / Codex `transport.is_alive`)。
  - i18n：`status.backendUnresponsive`/`status.noOutputFor` 加到 en.json/zh.json。
  - 测试：status_bubble 套件 17 passed（含 footer/心跳 render/S3 停心跳）；既有 dispatcher 61 passed；ruff 全过。
- 2026-06-24: **Loop 续作（commit 链）**：
  - `f8cdd29d` Step A — backend_alive 真正生效（base/claude/service/controller 分层探针；Codex 暂 None）。+8 tests。
  - `2954e6fb` Step C — 长结果(Discord)首段 edit 进气泡(case2)；`_send_split_result_messages` 加 `edit_first_message_id`。
  - `71165875` **评审修复**（codex 全量 diff 评审 issues-found → 已修）：
    - **Blocker**：heartbeat render 吞了 `CancelledError` → `_stop` 永久 hang → 改为 `raise`；心跳循环改为每 tick 读当前 bubble id（一 turn 一 task，re-send 不丢计时）。
    - split 后续 chunk 失败不再外抛（否则首段已发却被判未消费→孤儿 ✅ 覆盖已发内容）。
    - `suppress_delivery` 结果路径补孤儿气泡 tidy。
    - `clear_consolidated_message_id` 补停心跳+清 timing。
    - +2 回归测试（cancel-mid-edit 能终止；split 后续失败保留首段）。
  - 全量：status_bubble 套件 + backend_alive + result_fallback/platform_limits/scheduled = **74 passed**；ruff 全过。
- **目标达成（P0/P1）**：两诉求（不污染列表 + 状态可感知）在 Slack/Discord 已端到端实现、评审、修复、测试。

- 2026-06-24: **全设计目标 loop（每步过 opus+codex 双验收）**：
  - `a8c3bb5a` Step B — progress_style + 心跳/无输出阈值接 V2 config + controller getters + config_to_payload(防 partial-save 丢失) + 上下界 cap。
  - `0c6b6dbe` Step E — concise 下 indicator 行为：**保留 👀 received-ack reaction + typing keepalive**，仅抑制 ack **消息**(它会重复状态条且 Slack 删不掉)；`Controller.uses_concise_status_bubble` 单一真源。逻辑在 `start()` 的 concise 分支(reaction+typing best-effort)，非 first-wins。
  - `ad0af1c0` Step D — Codex backend_alive(transport.is_alive)；删不可达 overflow 脚手架。
  - `d82aa567` Step F — footer 下沉适配器：Slack context block / Discord `-#`；core 改传 (body, footer)；base 抽象加 `subtext`，Multi 仅 set 时转发(不破坏其它适配器)。
  - 每步 opus(general-purpose)+codex-expert 双评审，blocker/should-fix 当轮修复并加回归测试。
- `b030d757` **Web UI 开关** — Messaging 设置页 CompactSelect(精简/详细/关闭) for `agent_progress_style`，i18n en/zh，`npm run build` 通过。opus+codex ACCEPT(修复了编辑碰撞误删的 `appShell.moreSettings`)。
- **设计目标状态：spec 全部目标（后端 + 前端）已完成并逐轮 opus+codex 验收，共 12 commits。**
  - 注意：Web UI 子代理误改了**主 checkout**(master 工作区)；已把 3 个 UI 文件搬回 worktree 并 `git checkout` 还原 master，worktree ui 用软链 node_modules 重新 build 验证。
- **运行测试**：worktree 内 `/essd/qiqi/code/dev/avibe/.venv/bin/python -m pytest ... -p no:cacheprovider`；UI build `cd ui && npm run build`(node_modules 为指向主 checkout 的软链，gitignore)。
- **未推送/未发 PR**（按用户明确约束，等其指示）。

- **历史/已被取代的"剩余"清单（多已完成，仅留档）**：
  - **B 设置 plumbing**：`progress_style`(concise/off/verbose)+心跳/无输出阈值 接 V2 config + Web UI（现 dispatcher 已用安全默认 concise；getter 缺失即降级）。需动 config schema + React UI = 独立 PR 量级。
  - **E B2 indicator 抑制**：仅 `ack_mode=message`(非默认)才双信号；依赖 B 的 progress_style 解析。
  - **D case3 溢出删多余条**：状态条天生单行短、几乎不溢出 → 极低价值；`_consolidated_overflow_ids` 已预埋。
  - **F Slack context-block footer**：现用斜体小字近似，可升级为 `[section,context]`。
  - **Codex backend_alive**：transport 解析后接 `is_alive`（现 None 安全）。
  - **(原始遗留)** controller.backend_alive —— 已在 Step A 实现。
  - **Slack context block footer**：当前用斜体小字近似；可升级为 `[section, context]`（评审 B5）。
  - **Discord footer 字节预算**：edit-into-result 给 display_text 追加 footer ~15B，理论上 +footer 可能逼近 2000；当前 within_limit 用 1900 字符判定，余量足够，但严格起见可在判定时预留 footer 字节。
  - **B2 indicator 抑制**：concise 下抑制 ack 消息/reaction。默认 Slack ack=typing 无双信号，故 P0 不阻塞；待 `progress_style` 设置 plumbing 落地后随 P1 一起做（注意 Slack `supports_message_indicator_delete=False` 的 ack 残留）。
  - **P1**：心跳(已运行计时)+后端分流探针(B1)+typing 保活+"无输出"提示+取消心跳→edit 顺序(S3)+per-message 锁(S5，心跳并发才需要)+计时 footer(Discord `-#`/Slack context block)。
  - **P1**：result 情况2（长结果切分首段 edit）。**P2**：情况3（溢出删第2+条，用 `_consolidated_overflow_ids`）。
  - **设置 plumbing**：`get_progress_style_for_context` 目前 controller 未实现 → `_progress_style` 默认 concise；待接 V2 config/Web UI（concise/verbose/off + heartbeat/no_output 阈值）。
  - 未 commit（按规范等用户指示）。建议 commit msg：`feat(im): concise status bubble for slack/discord (P0)`。
