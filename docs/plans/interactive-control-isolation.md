# Controller pauses: what they were, what fixed them, what is left

## 中文摘要

**卡顿的主因已经查清：** 每次完整的内存回收，Python 都要重新检查控制进程里约 85 万个对象。你的 Mac 上，没有任何模型请求在跑时也是如此。这些对象主要来自加载的程序代码（函数、类、模块），不是对话内容。其中近一半来自飞书 SDK：它一次加载约 1 万个模块，在单独测试里增加约 40 万个对象、约 2 秒启动时间和约 180 MiB 内存。

**试验结果（你的 Mac，rc4，同一个进程）：** 程序运行一段时间、空闲时，把这些不再变化的对象“冻结”，让回收不再反复检查它们。

| 同一进程，各 71 分钟 | 完整回收次数 | 超过 0.2 秒的次数 | 中位时长 | 最长 | 主循环卡住（≥2 秒） |
|---|---:|---:|---:|---:|---:|
| 冻结前 | 约 201 | 189 | 0.27 秒 | 0.91 秒 | 0 |
| 冻结后 | 约 298 | **0** | — | — | **0** |
| 解冻后（前 3 分钟） | 10 | 10 | 0.32 秒 | 1.05 秒 | — |

回收次数来自进程累计计数器，误差约 1 次。

冻结期间内存没有增长：中位 290 MiB，最高 567 MiB，冻结前两小时最高 793 MiB。解冻后，长停顿立刻恢复。这说明效果来自冻结本身。

**试验为什么在 71 分钟结束：** 我设的安全阈值遇到任何错误日志就自动解冻。触发它的是一次微信私信发送失败，代码记录了两条错误日志。从 11 月 14 日起，这种失败已出现 19 次，都没有冻结。冻结前的近 4 小时里一次也没有，因此和冻结无关。这次可能有一条微信通知没有送达；网页和其他渠道不受影响。阈值太宽，是我设计的问题。

**还剩什么：** 冻结前约 3.8 小时里，主循环卡住 17 次：9 次与内存回收同时发生，8 次与内存回收无关。这 8 次分别来自后台命令读取文件、网络响应解压、一次进程身份检查，以及 5 次无法标识来源的调用。这些问题需要单独修。此外还有两项已知但较小的工作：大请求的编码/解析，以及数据库读写，它们目前都在主循环上。原先的“控制进程 + 执行进程”重构仍能带来故障隔离：一个对话卡死或崩溃时不影响其他对话。但仅仅为了消除内存回收停顿，已经不再需要它。

**建议：** rc5 上再跑一次完整试验，至少一天，只让内存增长或与冻结有关的错误触发解冻。通过后，把冻结设为默认开启，并保留随时解冻的开关。飞书改为自有连接后（#4067：62.3 万 → 23.3 万个对象），仍能节省启动时间和内存，但不再是解决卡顿的必要条件。较大的重构是否还做，由你根据故障隔离的价值决定。

## Findings (English, one page)

**Cause.** A census of the owner's rc2 controller at idle, after two hours of use, counted 852,732 tracked objects with zero model streams. They were mostly functions (229,804), tuples, dicts, cells, classes and 13,087 module objects. That is the shape of imported code, not Session data. A standalone import of the same rc2 runtime reproduced 659,211 objects. Initializing the Feishu client (`lark_oapi`, 10,624 modules) added 395,690 tracked objects, plus 1.96–2.30 s and about 180 MiB RSS in warm isolated starts. A full collection rescans this static heap every time. That explains why its duration barely depended on how many streams were open.

**Change.** #4041 (rc4) adds an off-by-default, one-shot freeze. It is armed through the controller's private local IPC, takes effect only at a natural idle full collection after at least two hours of uptime, and requires zero open model streams and no live Turn. Durable Turn admission and transient stream/Turn publication are fenced. Unfreeze through the same channel is the rollback; no restart is needed.

**Field trial, owner's Mac, rc4, controller PID 87361.** Armed 17:15. Froze at 18:54:46 with 854,841 objects. An automatic guard unfroze it at 20:06:00, giving 71.2 min under freeze. The A-B-A comparison uses the same process and duration:

- Before: about 201 full collections, 189 over 200 ms. Median 273 ms, p90 354 ms, maximum 912 ms; 46 s/hour spent in slow collections.
- Frozen: about 298 full collections, none over 200 ms, and no loop stalls. The higher count suggests the workload was not lighter; stream counts during the frozen window are not logged because none crossed the logging threshold.
- After unfreeze: slow collections returned at once. All 10 full collections in the first 3.2 minutes exceeded 200 ms; maximum 1,047 ms. The first one scanned 882,495 candidates, so the whole heap was visible again.
- RSS while frozen: median 290 MiB, maximum 567 MiB. The baseline peak was 793 MiB. No retention problem was observed in this window.

**Why it ended.** The guard unfroze on any ERROR record. The two records were one failed outbound WeChat DM, logged by `send_message` and again by `send_dm`. The same pair has appeared 19 times since November 14, independent of any freeze. It did not occur in the 3.8 unfrozen hours earlier on rc4. User impact: possibly one undelivered WeChat notification. The guard was too broad.

**Remaining loop work (not addressed by freezing).** The 3.8 unfrozen rc4 hours had 17 loop stalls of at least 2 s: 9 with a collection in progress and 8 without. The 8 came from job metadata file reads on the loop (1), zstd response decompression on the loop (1), a runtime ownership check (1), and redacted frames (5). Two more stalls occurred during collections in the job-read and zstd paths. Earlier field work also found request-body encode and parse on the loop (1–52 ms per request on large bodies), about 66 of 69 SessionTurnManager SQLite transactions on the loop with a 5 s `busy_timeout`, and a 2.01 s startup stall. Estimates: body encode/parse off-loop 3–6 Agent-hours; bounded fixes for the named stalls 6–12 hours; a dedicated Turn-control database lane 16–30 hours.

**What a rebuild still buys.** A thin controller plus one supervised execution host, estimated at 55–90 Agent-hours, would contain crashes and wedged Turns. It would also remove Turn churn from the controller. It is no longer required to remove the GC pause class. The Feishu own-connection change (#4067: 622,780 → 232,614 objects in own mode) reduces startup cost, memory and the unfrozen scan. With the freeze in place, it is not needed for pause duration.

**Unknowns and next steps.** This is one 71-minute window on one machine. Next: a full-day trial on rc5 whose rollback guard responds only to RSS growth and errors attributable to the freeze. Then default-on behind the same warm-up gate, with unfreeze retained. Still untested: long-term retention of objects alive at freeze time, behavior after configuration reloads that import new code, and other operating systems. Earlier architecture analysis and the prototype benchmark remain in this PR's history (commit `8c2ce4056`) and `fixtures/interactive-control-isolation/`.
