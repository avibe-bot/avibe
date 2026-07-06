# 过程消息精简方案 —— 「会变成结果的状态条」（Slack / Discord）

> 状态：**已定稿（精简版）**。取代此前两份较重的方案（已删）。经 codex-expert 两轮评审（sound-with-changes），3 Blocker / 5 Should-fix / 5 Nit 已折回。
> 范围：Slack / Discord（支持原地编辑 + typing）。其余平台回退现状、不受影响。
> 只为解决两个核心诉求：
> 1. **不污染消息列表**：一轮跑完只留一条结果，不留过程残渣。
> 2. **推理状态可感知**：长时间没新内容也能看出"还活着、没挂"。

## 1. 一句话方案

整轮只维护**一条状态条**（复用现有合并气泡的 message_id），过程中显示一行极简状态 + 走动的计时；**结束时把这条状态条原地 `edit` 成最终结果**。常态下既不删消息、也不新发——一轮只剩一条结果。

## 2. 设计原则（按用户拍板）

- **优先 Edit，不用 Delete**：反正终态必须有一条 Result，就让状态条 edit 成 Result。
- **Delete 仅用于收尾清理**：唯一场景是"过程中一条装不下、发了 2 条"——这时删掉第 2 条、把第 1 条 edit 成 Result。属边缘兜底。
- **状态条一行替换、不累加**：永远短 → 几乎不会触发上面的多条/删除场景。
- **过程不丢**：完整过程照常持久化到 store；我们只精简 **IM 呈现**，Web/transcript 不变。

## 3. 状态条生命周期

单条消息，原地编辑：

| 阶段 | 正文（极简，单行替换） | 计时（P1 才有，见下） |
|---|---|---|
| 开始 | `⏳ 思考中` | `0:01` |
| 进行 | `🔧 <最新动作>` | `0:08` |
| 完成 | `<最终结果>` | footer `✅ 18s` |
| 停止/失败 | `⚠️ 已停止` / `❌ <一句话>` | `0:31` |

- 正文只显示"最新一个动作"，**不拼接历史**（复用已 emit 的过程文本取最新一条，**无需新增结构化事件通道**）。
- **计时是 P1 心跳驱动的**（§5）；P0 先不带计时，状态条是静态的 `⏳ 思考中` / `🔧 <动作>`（Nit N3，避免 P0 验收时问"计时呢"）。
- **"当前动作"标签的提取（Should-fix S1）**：不能对 `format_toolcall` 输出**裸截 50 字**——它形如 `🔧 \`execute_command\` \`{"command":"pytest…"}\``，裸截会切坏反引号/JSON。需新增 `format_toolcall_status_label()`：只取工具名 + 单独截参数 + 去反引号。assistant 文本是多行（`\n\n` join），取**首行截断**；真有思考文本时显示首行，**不要硬显示"思考中"**——仅在确无文本时才回落 `⏳ 思考中`。
- Discord 计时用 `-#` 小字；Slack 用 context block 小字。

## 4. 结果落地（三种情况）

> **评审 B3 提醒**：result 分支现有 6 条投递路径，只有 inline 与 edit 干净组合；其余需逐条处理（见下表与 §8 孤儿清理）。

| 路径 | edit-into-bubble 处理 |
|---|---|
| inline (`_send_result_inline`) | 拦截 `send_message`→`edit_message(bubble_id, …, keyboard=)`；干净 ✅ |
| split (`_send_split_result_messages`) | 首段改 edit、其余发新消息——需加 `first_message_id_to_edit` 参数 fork 现循环 |
| summary + `.md` upload | 摘要 edit 进气泡、附件另发；**注意 S4**（见下） |
| fallback upload / 失败通知 / 附件通知 | edit 失败时**状态条会变孤儿**，必须清理（§8） |

三种情况：
1. **常态（状态条 1 条 + 结果能放下）**：状态条 `edit` 成结果（带 quick-reply 按钮走 edit-with-buttons 通道）。**不删、不新发。**
2. **结果很长，一条装不下**：状态条 edit 成结果**第 1 段**，其余段作为新消息追加，按钮挂最后一段。沿用 `_split_result_text_by_bytes`，仅首段由 edit 现有气泡产出（split 函数加参数）。
3. **过程曾溢出成 2+ 条**（边缘）：**删掉第 2..N 条**，把第 1 条按 1/2 规则 edit 成结果。

> **Should-fix S4（关键 bug 点）**：Slack `edit_message` **返回 `bool` 不是 message_id**（`slack.py:1469-1497`）。edit 成功后必须 `primary_message_id = <已知气泡 id>`，**不可** `= await edit_message(...)`（那会得到 `True`），否则 `finalize_scheduled_delivery`(`:830`) 与 `_stream_chunk` 全断。
> 结果走 `.md` 附件等兜底：逻辑不变，只把"首条结果消息"改为 edit 气泡产出，并按 S4 显式设 id。

## 5. 状态可感知（防"假死"）—— 分层判断

"卡死"分三种，单一信号不够，分层侦测：

| 情况 | 侦测手段 |
|---|---|
| ① avibe 进程崩了/被杀 | **进程心跳**停 → footer 的"已运行"计时不再增长 |
| ② 后端(claude/codex)挂了/无响应 | **后端存活探针**（按后端分流，见下）→ 翻 `⚠️ 后端无响应` |
| ③ 长任务跑得久但正常 | 当前动作标签 + "已运行"在涨 = 正常 |

**后端存活探针必须按后端分流（Blocker B1）**：`controller.receiver_tasks`（`controller.py:177`）**只有 Claude 会填**（`claude_agent.py:123-126`）；Codex 用 per-dir `CodexTransport`、不在其中 → 裸查会 `KeyError`，或 Claude 切 Codex 后残留 truthy 误判。故：
- Claude：`receiver_tasks[runtime_session_key].done()`；
- Codex：查该 session 活跃 transport 的 `CodexTransport.is_alive`（`transport.py:157-164`）。

→ 因此**心跳应落在各 agent 内部**（Claude agent 能拿 `receiver_tasks`，Codex agent 能拿自己的 transport），而非 dispatcher（拿不到 agent 句柄）。心跳通过 agent 暴露的统一接口（如 `backend_alive(context)->bool`）回报存活，dispatcher/渲染层只消费布尔值。

**进程心跳（必要，且非装饰）**：一个挂在 turn 上的心跳任务，每 ~30s 做两件实事：
1. 刷新 footer 的 **"已运行"计时**（mm:ss，由心跳驱动 → 长任务期间也在涨 = avibe 活着）；
2. **按后端分流探存活**，没了就把 footer 翻成 `⚠️ 后端无响应`。
心跳 turn 结束即停、受 turn-token 守卫，stale turn 不刷；真实事件到来时即时重渲染（重置动作标签与"无输出"计时器）。`last_activity_at` 须**按 turn-key 存**（`dict[consolidated_key,float]`），避免跨 session 串扰（Nit N1）。心跳循环对自身 `edit` 调用要 `try/except CancelledError: return`，防取消时留半截状态（Nit N4）。

**残留盲区兜底**：后端活着但某工具内部死循环永不返回 —— 事件层无法与"超长正常任务"区分（avibe 故意不设 turn 超时，正经任务可能跑数小时）。用**温和提示**而非死亡判决：>N 分钟（默认 3min）无任何新活动时，footer 追加 `· 已 N 分钟无输出` 作为线索。

**typing 指示**：全程保活（Slack RTM / Discord `sendTyping`），与心跳双保险。

### footer 样式（保留沙漏 ⏳，辨识度）
```
运行中      -# ⏳ 运行 pytest · 4:30
久无输出    -# ⏳ 运行 pytest · 6:00 · 已 3 分钟无输出
后端无响应  -# ⚠️ 后端无响应 · 4:30
完成        -# ✅ 18s
```
- "已运行计时" = turn 开始至今，心跳驱动增长。
- Discord 用 `-#` 小字；Slack 用 context block 小字。

## 6. 相比"大方案"砍掉了什么

- ❌ 结构化进度事件旁路（`AgentProgressEvent`）——复用已 emit 的文本取最新一行即可。
- ❌ 平台中立渲染器抽象层——只做两平台，直接在适配器里拼 footer。
- ❌ `tidy_overflow` 能力位、批量删除。
- ❌ verbose/concise 双 pipeline 大改——过程路径从"累加+切分+continued below"改为"一行替换"，多半是**净减代码**。

## 7. 实现落点 / 新增

- `core/message_dispatcher.py`
  - 过程合并块（~893-1027）：Slack/Discord 走"单状态条、一行替换"，去掉 append/split/`continued below`。
  - result 分支（~697-891）：首条结果消息改为 **edit 现有状态条**（无气泡则回退现发送逻辑）；按 §4 表逐路径处理；**edit 成功后按 S4 显式设 `primary_message_id`**；持久化（process 行 + result 行）不变。
  - 复用 `_consolidated_message_ids` 记状态条 id；**显式新增 `_consolidated_overflow_ids: dict[str,list[str]]`**（仅情况 3 用），并在 `_clear_consolidated_state`(`:225-230`) 一并清（Should-fix S2）。
  - **新增 per-message 编辑锁字典** `dict[message_id, asyncio.Lock]`，把"**取消心跳 → edit 结果**"整段**原子化**在锁内（Should-fix S5；光靠 Discord `_run_on_client_loop` 不够，会被心跳 edit 抢后写）。
  - `last_activity_at` 按 turn-key 存（N1）。
- 心跳：**落在各 agent 内部**（Claude/Codex 各自能拿存活句柄，B1），per-turn asyncio task，挂 turn 生命周期、受 turn-token 守卫；agent 暴露统一 `backend_alive(context)->bool` 给渲染层。
- `modules/im/slack.py` / `discord.py`
  - `edit_message` 支持带按钮（edit-into-result 用，避免 quick-reply 丢失）。**注意 Slack `edit_message` 返回 bool（S4）**。
  - footer 渲染：Discord `-#`、Slack context block。
  - 情况 3 需要 `delete_message`（删自己发的消息；两平台 API 简单，仅此一处用到）。
- `core/processing_indicator.py`（Blocker B2）：concise 下**抑制 ack 消息/reaction、仅留 typing**。需落地一个抑制 hook——`start(..., suppress_message=True)` 或服务内读 `progress_style`。**额外**：Slack `supports_message_indicator_delete=False`，若部署配了 `ack_mode=message` 又不抑制，`finish()` 删不掉 ACK → **永久残留**；抑制 hook 必须覆盖这条路径。
- `modules/im/formatters/base_formatter.py`：新增 `format_toolcall_status_label()`（S1，干净提取工具名/截参数/去反引号）。
- `vibe/i18n/`：`status.thinking/working/done/stopped/stillRunning/backendUnresponsive` 等。

## 8. 边界处理

- **edit 失败 + 孤儿气泡清理（Blocker B3）**：edit 失败/走附件兜底/失败通知等路径会让状态条变孤儿（永远卡 `⏳`）。每条非 inline 路径在另发结果后，**必须把孤儿状态条 edit 成"✅/见上"或删除**，不可放任。回退新发本身不阻塞。
- **过程被频道隐藏**：没有状态条 → 只有 typing + 结果，更干净（结果走正常发送）。
- **Discord 长度**：状态条与"结果能否放下"判断要**预留 footer 字节**，否则撞 2000 上限。
- **顺序（防旧状态盖结果，Should-fix S3）**：**取消心跳(await 其结束) → 再 edit 成结果**，且这步**不能塞 `finally`**（`finally` 在 :889-891 与 indicator 清理并列，会与 edit 并发）。**stop / `is_error` / 异常**终态路径同样要取消心跳（不只正常 result）；与 `release_runtime_turn` / `finish_terminal_turn` 钩子串起来。
- **持久化不退化**：edit/删除均不改 store 中 process/result 行（失败结果仍按 `error` 入库）。

## 9. 设置

频道级 `progress_style`：`concise`（**Slack/Discord 首版直接默认**，见 §11 分叉2）/ `off`（仅 typing + 结果）。
可调：`heartbeat_interval_ms`(默认 30000)、`no_output_hint_after_ms`(默认 180000，即 3min 触发"已 N 分钟无输出"提示)、`show_step_count`(默认关，先不显示步数保持最简)。状态条正文固定显示"当前动作"（已拍，见 §11）；footer 保留 ⏳ 沙漏。

> **后续更新（2026-07）**：
> - **飞书/Lark 也支持 concise 状态条**，见 [feishu-concise-progress.md](./feishu-concise-progress.md)。飞书卡片 schema 2.0 无 `note` 组件，footer 用 notation 字号 + 内联 `<font color='grey'>` 渲染。收尾**不走撤回**（飞书撤回会留「此消息已撤回」墓碑），而是把气泡 `edit` 塌缩成 `✅ done · 耗时 · tok` 小标记；结果仍单独新发以保留推送。当前支持 concise 的平台为 **Slack / Discord / Lark**。
> - **心跳间隔默认已改为 8s**（`agent_status_heartbeat_ms`，实现值此前为 15000，本节 30000 为更早的规划值）。全局生效，非按平台。

## 10. 实现顺序（最小切片）

> P0 **先不带计时**（计时随 P1 心跳落地）——状态条 P0 阶段是静态动作标签（Nit N3）。

1. **P0**｜Slack/Discord 过程改"单状态条一行替换"（去 append/split）+ `format_toolcall_status_label()`（S1）+ `edit_message` 带按钮 + dispatcher 侧 per-message 编辑锁（S5）。
2. **P0**｜result 情况 1：edit 状态条成结果（按 S4 设 id；含回退新发 + 孤儿清理 B3）+ indicator 抑制 hook（B2）。
3. **P1**｜心跳计时 + 后端分流存活探针（B1）+ typing 保活 + 取消心跳→edit 顺序（S3）。
4. **P1**｜result 情况 2（长结果切分，首段 edit）+ 无输出提示。
5. **P2**｜result 情况 3（溢出删第 2+ 条，需 `_consolidated_overflow_ids` S2）—— 实际极少触发。

## 11. 决策

**已拍（分叉1）**：状态条正文**显示当前动作**`🔧 <最新动作>`（截断 ~50 字、剥 workspace 前缀；无工具回合显示 `⏳ 思考中`）。来源是**复用已 emit 的过程文本取最新一条**，仍**不引入结构化事件通道**——只是把现在"累加"改成"取最新一行替换"。属 P0 范围。

**已拍（分叉2）**：concise **首版直接设为 Slack / Discord 默认**（不灰度）。其余平台仍回退现状。

**已拍（分叉3）**：状态可感知采用**「进程心跳 + 后端探针 + 已运行计时 + 无输出提示」分层方案**（见 §5），而非单纯计时器或单纯时间戳。
- 心跳 ~30s（`heartbeat_interval_ms` 默认 30000，可调），驱动"已运行"计时增长 + 探后端存活；真实事件到来时即时重渲染并重置"无输出"计时器。
- 选"已运行计时（心跳驱动）"而非"事件驱动绝对时间戳"的原因：绝对时间戳在**长任务**中途无子事件时会冻结、误报卡死；心跳驱动的已运行计时在长任务期间仍增长，配合后端探针与"无输出"提示，能区分①avibe 死 ②后端死 ③长任务正常。
- 保留 ⏳ 沙漏图标（辨识度）。

## 12. 测试

- 单测：状态条一行替换不切分；result 情况 1/2/3；心跳刷新计时；取消心跳→edit 顺序无"旧状态盖结果"（含 stop/error 路径，S3）；edit-into-result 不丢按钮；**edit 成功后 id 是气泡 id 不是 True（S4）**；Discord footer 字节预算不撞上限；**edit 失败/附件兜底后无孤儿状态条（B3）**；`_consolidated_overflow_ids` 在情况 3 后被清（S2）。
- **后端探针（B1）**：Claude 用 `receiver_tasks`、Codex 用 transport.is_alive；Claude→Codex 同 session 切换不误判；缺 key 不 `KeyError`。
- **标签提取（S1）**：`format_toolcall_status_label()` 不产生坏 markdown；assistant 多行取首行截断。
- **indicator（B2）**：concise 下 `ack_mode=typing` 与 `ack_mode=message` 均无残留 ACK，仅 typing。
- 持久化回归：concise 下 store 仍有完整 process + result 行。
- 人工：Incus 跑 Slack + Discord，确认①一轮一条结果②计时走动③无过程残渣④后端杀掉时翻 `⚠️ 后端无响应`。
