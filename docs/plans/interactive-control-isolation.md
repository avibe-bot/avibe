# Controller pauses: cause, what shipped, what is left

Status: closed record (3.2.8). The fixes below have shipped. The process
rebuild that this plan first proposed is deferred. Nothing in this document is
pending implementation.

## 中文摘要

**原因：** 每次完整内存回收，Python 都要重新检查控制进程里约 85 万个对象，即使没有模型请求在跑也是如此。这些对象主要来自加载的程序代码，不是对话内容；其中近一半来自飞书 SDK。

**已上线：**
- 预热后冻结（#4041、#4074，3.12 修正 #4176）：服务运行满两小时后，在第一次自然的完整回收时把这些不变的对象冻结，后续回收不再检查它们。默认开启，可在同一进程内通过本地私有接口解冻，无需重启。
- 主循环上的同步工作移走或加索引：请求历史在每次模型调用后释放（#3937）；Turn 结束时的替换查找加索引、任务启动移出主循环（#3960）；任务文件轮询（#4079）、请求编码/解析（#4081）、Watch 接管（#4117）移出主循环；Turn 结束的队列与收件箱读取改为按索引定位（#4165）；Claude SDK 与 MCP 客户端改为首次使用时加载（#4127）。
- Agents → 运行中 图：Active 视图 12.4 秒 → 0.28 秒（#4186）；含历史视图 9.06 → 5.91 → 4.69 → 2.30 秒（#4186、#4194、#4202）。

**现场结果（主人的 Mac）：**
- rc8 完整 24 小时冻结试验：超过 0.2 秒的完整回收从每小时 178 次降到 52 次，每小时损失时间从 82 秒降到 18 秒；内存峰值 643 MiB，对照 937 MiB。
- rc13 起每个进程都在约两小时后自动冻结（CPython 3.15）。冻结前后慢回收：rc13 116→2.6 次/小时，rc16 155→2.2 次/小时；负载重的进程冻结后仍有约 33 次/小时。3.12 只在测试中验证过，主人的机器不跑 3.12。
- rc13 的 7.45 小时内，Turn 结束时没有出现 ≥2 秒的队列或收件箱读取卡顿（rc8 为 20 小时 18 次）。

**暂缓及原因：**
- 回调边的部分索引：可把含历史视图里最大的一条查询（1.43 秒）变窄，但基于 JSON 条件的索引在新旧 SQLite 之间会算出不同结果，可能漏画应有的边；最旧受支持 SQLite 未确认，冷启动建索引时间也无法给出上限。产品与 lead 决定停在 2.30 秒。另一种方案是新增一列由写入者维护，但旧版本写入者会改动回调模式而不清这一列，回滚后再升级会漏边，因此也不做。
- “控制进程 + 执行进程”重构：仍能隔离单个对话的崩溃或卡死，但已不再是消除回收停顿的前提。主人决定暂不重构。

**仍存在：** ≥2 秒的卡顿未清零，分散在 Watch 所有权检查、SQLite 写提交、会话绑定、响应解压等处，没有单一主因。zstd 解压要先确认是哪个客户端，才决定是否改动。

## Findings (English)

**Cause.** At idle, two hours after start, the owner's rc2 controller tracked
852,732 objects with no model streams open. Most were functions, tuples, dicts,
classes and 13,087 modules: imported code, not Session data. Initializing the
Feishu client (`lark_oapi`, 10,624 modules) alone added 395,690 objects, about
2 s and about 180 MiB in isolated starts. Every full collection rescanned this
static heap, so its duration barely depended on load.

**Shipped.**

| Area | PRs | Effect |
|---|---|---|
| Warm-heap freeze | #4041, #4092, #4126, #4074, #4176 | Default on. Freezes at the first full collection after two hours of uptime; same-process unfreeze over private local IPC is the rollback. #4176 recognizes CPython 3.12's nonempty startup baseline. |
| Request retention | #3937 | Request histories are released after each model attempt. |
| Loop work | #3960, #4079, #4081, #4117, #4073 | Indexed Turn-end replacement lookup; job spawn and polling, request encode/parse and Watch hand-over moved off the loop; loop-stall frames name Avibe's own code. |
| Turn-end reads | #4165 | Live FIFO heads and common inbox updates use index seeks instead of history scans. |
| Startup imports | #4127 | Claude Agent SDK and MCP client load on first use. |
| Agents graph | #4186, #4194, #4202 | Active stops reading unrelated history. History reads timestamps from a covering index (migration 0099), fetches only contributing headers, and logs per-statement rows and timings. |

**Field results, owner's Mac.**

- rc8, full 24 h frozen versus the unfrozen baseline: full collections over
  200 ms fell from 178.3 to 51.8 per hour, and time lost to them from 82.2 to
  17.7 s per hour. Peak RSS was 643 MiB, against a 937 MiB baseline.
- From rc13, every controller froze on its own about two hours after start
  (CPython 3.15). Slow full collections per hour before and after the freeze:
  rc13 116.4 → 2.6, rc16 154.8 → 2.2. Two busier processes still ran about
  33 per hour after freezing. The 3.12 path is verified in child-process tests
  only; the owner does not run 3.12.
- #4165: in 7.45 h on rc13, no Turn-end delivery-queue or inbox stall of 2 s or
  more, against 18 in 20.25 h on rc8. One inbox read stalled at Turn start. The
  window is too short to measure the size of the improvement.
- Agents graph, one request per release: Active 12.4 s → 0.283 s. History
  9.06 s → 5.91 s (rc13) → 4.690 s (rc15) → 2.295 s (rc16). Web-process disk
  reads fell from 655 MiB to 401 MiB, then to 220 MiB.

**Deferred, and why.**

- *Callback-edge index (History).* On rc16, `callback_modes` cost 1.433 s of
  2.295 s. The cost comes from fetching wide rows to read `callback_mode`
  from `metadata_json`. A partial index over proven failure-only rows
  halved History reads on a fixture. It was not shipped:
  - the plain JSON predicate gives different results on SQLite 3.34 and 3.50,
    and can drop an edge that should be drawn;
  - the restricted form still needs the oldest supported SQLite established;
  - it needs an exception to the additive-migration rule;
  - its cold build time on the owner's database has no bound.

  A writer-maintained column was rejected too. Released writers remove the mode
  without clearing that column, so a stale value after a rollback could hide an
  edge. Without the callback cost, about 0.86 s would remain, 78% of it other
  SQL. Product and the lead chose to stop at 2.3 s; reopen only on new field
  evidence.
- *Process rebuild.* A thin controller with one supervised execution host
  (estimated 55–90 Agent-hours) would contain crashed or wedged Turns. It is no
  longer needed to remove the GC pause class. The owner deferred it. The
  prototype and VM benchmarks remain in `fixtures/interactive-control-isolation/`.

**Remaining.** Stalls of 2 s or more still occur, spread across Watch
ownership checks, SQLite write commits, session binding and response
decompression, with no dominant source. zstd decompression stays on the loop
until a labelled sample identifies the client. Long-term retention of objects
alive at freeze time and behavior on other operating systems are untested in
the field.
