# PM 设计评审：Memory #1990 「移除持续身份认证」设计

评审日期：2026-09-15。评审对象：`docs/plans/memory-macos-identity-recovery-1990.md`（下称「设计」），
配套证据 `docs/investigations/issue-1990/`。源码基线 `199cccf9`，与事故提交 `7f307086` 的
差异按 README 所述仅涉及 profile 启用。本评审只读仓库，不做实现、不发 PR、不代表合并批准。

## 结论：PASS WITH CHANGES（无阻断项）

设计方向正确、且是目前几个方案里最小的：它删掉的正是把「显示用创建时间变了 1 秒」放大成
「Memory 停摆」的那条链路，而不是把同一个时间戳比较搬到别处。需要在派工前补 6 处澄清
（见第三节），其中最重要的是 **psutil 依赖下限必须写成 `>=7.1.0`**，否则 Linux 上会把
今天用 starttime ticks 解决过的时钟跳变问题重新引进来。没有需要重新设计的地方。

## 一、逐项判断

### 1. 是否真正解决假故障（而不是把时间戳比较挪个位置）—— 是

事故链路在源码中是一条单一依赖：

- `_process_creation_stamp()`（process.py:2402）在 macOS 上返回 **公开的、经 boot-time
  修正的** `create_time()`；
- `_live_owned_processes()`（3273）用它做相等过滤；
- 三个消费者都把「不相等」当成「所有权变化」：`_host_identity_is_live()`（3238，0.2 s 一次，
  `_monitor_child` 785）、`_refresh_owned_process_tree()`（3247，1 s 扫描 + `_wait_for_ready`
  715 + 终止路径 2259/2273）、`_confirmed_owned_processes()`（3293，信号闸门 3364/3374）。

所以一次 +1 s 偏移同时造成两个缺陷：监控误判 → 触发终止；终止时信号闸门拒发信号 →
`_wait_for_owned_exit` 超时 → `RuntimeError` → `_down=True`、进入有界 Wake，Wake 里的
`supervisor.stop()` 再次走同一闸门失败 → 三次 `memory_wake_failed`，claims 保持暂停。
README 的 replay 记录与此一致（"no signals are issued while identity mismatches"）。

设计的替换物是 psutil 自己的身份：`Process._get_ident()` 在 macOS 上用
`create_time(monotonic=True)`，绕过 boot-time 修正；`is_running()`、`__eq__`、`send_signal()`
（后者经 `_raise_if_pid_reused()` 间接调用 `is_running()`）都基于它。我核对了 psutil
release-7.1.0 ~ 7.2.2 每个 tag 的 `__init__.py`：`LINUX or NETBSD or OSX` 分支从 7.1.0 起
一直存在；macOS 的 `INIT_BOOT_TIME` 修正也是 7.1.0 同版本引入。公开探针
（`public_process_probe.py` / `public-process-result.json`）在 +1 s 注入下 equal/is_running/
terminate 全部成立。**这不是「同一比较换个地方」：比较的值不同（原生 birth time vs 修正后
显示值），比较的执行者不同（库在动作边界，而不是 Avibe 每 0.2 s 一次）。**

保留的残余：psutil 仍是用户态 check-then-kill，不是 pidfd。设计已如实声明（"does not claim
an atomic macOS pidfd equivalent"），不夸大。

### 2. 职责划分是否连贯、保留 Process 引用是否整体更简单 —— 是，但「谁持有引用」要定

表格化的边界（Spawn / Normal runtime / Safety inspection / Stop / Replacement / Across restart）
与源码结构一一对应，且把「跨重启孤儿归类」明确隔离为例外路径，这是对的。相比被否决的
方案（Darwin ABI、新 schema、boot UUID、再确认循环、私有 getter），它只做一件事：把
`dict[int, float]` 里的 float 换成库对象，删掉围绕 float 相等的判定。

一处需要决策的抽象：设计写 "The concrete system host owns retained public psutil Process
references… Prefer a plain PID-to-Process collection owned by that host"。这会把当前
**无状态** 的 `_SystemProcessHost` 变成有状态对象，而它目前在 6 处被独立构造：
`EverOSProcess.__init__`（345）、`_RecordedSidecarReaper`（1047）、`SidecarOwnership`（1241）、
`_ReleasedSyncReaper`（1746）、`ReleasedEverOSOrphanReconciler`（1929），以及
`_wait_for_owned_exit` 里每次调用都新建一个（3400）。引用若挂在 host 上，第 3400 行的
新建 host 就会丢掉引用，而且 `SidecarOwnership` 与 `EverOSProcess` 共享同一 host 实例
（345 → 1241）会让两条路径的引用混在一个容器里。

更小的做法：**引用留在 `EverOSProcess._owned_processes` 现有位置，只把值类型从 `float` 换成
host 产出的不透明活句柄**（生产环境就是 `psutil.Process`，测试假 host 用自己的对象）。
`_ProcessHost` 各方法的形状（`Mapping[int, Handle]`）不变，host 保持无状态，测试里
`_SidecarHost` / `_ReleasedSyncHost`（tests/test_memory_process.py:24、79）只需改值类型。
两种做法都能实现设计意图；请设计二选一并写明，不要留给实现者。我的建议是后者。

设计中不属于本问题的范围：「running projection 必须同时看 `_starting/_down/stop` 标志」
（Normal-running changes 末段）。`running` 属性（371）被 supervisor `status`（supervisor.py
152）和 runtime 多处（1106、1337、2855）消费，改它是独立的投影语义修正，与假故障无关。
建议移出本设计或降为「仅当本次测试需要时」。

### 3. 监控/子孙发现、进程组终止、PID 复用与 asyncio 回收竞争、未知子孙、released 记录恢复
——大体一致，有 5 处要点名

一致的部分：`_watch_child` 继续是唯一的直接子进程退出路径；扫描中观察到退出 = 普通竞争、
不进失败分支；组信号只在「当前成员全部与保留引用相等」时发出、pgid 数字本身无权威；
直接子进程 `wait()` 结果在仍有已知子孙时不足以退休记录；退出/复用与延迟 asyncio 回调的
测试要求（证据项 3）恰好对准 CPython `Popen.send_signal` 的已知竞争和 asyncio 线程 watcher
先 `waitpid` 再回调的时序。

需要澄清的：

(a) **孤儿路径仍在用时间戳当活性**。分类器（`_classify_recorded_child` 2874）本身已经能容忍
wall-clock 不一致（`_legacy_create_time_mismatch_verdict` 2831 回落到 cmdline/uid/EVEROS_ROOT），
但通过分类之后，`reap()`（1313）把 `identity.stamp` 交给 `_terminate_orphan_tree`（1412），后者
和 `_terminate_claimed_processes`（1681）、`_reap_unidentified_child`（1590）都用
`_host_identity_is_live` / `host.live` / `_wait_for_identities_exit`（3184）——全是修正后时间戳
相等。若 boot-time 读数在「分类」和「等待退出」之间翻转，`_live_owned_processes` 会把活着的
孤儿判成不活 → `wait_for_exit` 立即返回 True → 记录被删 → 新子进程与孤儿并存于同一
provider root。设计一句 "retain a fresh public Process reference and bracket identity-bearing
reads with public identity checks before acting" 意图正确，但请**点名这三个函数**必须改用
分类时捕获的引用，并说明 `_snapshot_process_group` 对新成员的捕获同样返回引用。

(b) **「未知成员」的表示**。现在用 `-1.0` 哨兵表示 AccessDenied 的组成员（3221、3052），
`_refresh_owned_processes`（917）专门把它们保留下来以让 killpg 失败关闭。换成引用后，
macOS 上任意 pid 的 `create_time` 都可读，`psutil.Process(pid)` 基本不会因权限失败，
「未知」的含义会漂移成「cmdline/environ 不可读」。设计写了 "Unknown current group members
remain unknown, not owned"，但没定义未知 = 什么。请写成一句可实现的规则，例如：
组成员若无法构造/校验公开引用（NoSuchProcess 之外的 psutil.Error）→ 记为未知，阻止组信号，
且使清理保持「未证明」，不得静默丢弃。

(c) **psutil 引用的粘性语义**。`is_running()` 一旦置 `_gone` 或 `_pid_reused` 就永久返回
False（`__init__.py` 632-641）。这正是设计要的（"do not refresh a PID entry into a different
process generation"），但要写成不变量：一个引用一旦报告 gone/reused，就视为该 pid 的清理
已完成或该 pid 已属他人，永不按 pid 重新捕获、永不再对它发信号。

(d) **`processing_healthy()` 探针（442）共用同一组终止/快照助手**（`snapshot_tree` 464、
`_terminate_owned_tree` 473/490/508）。它不在设计的边界表里；请把它列为同一批助手的
消费者，并在证据项 4 或新项里加一条「探针超时清理在偏移期间仍能发信号并证明退出」。

(e) **依赖下限**。设计说 "set a justified dependency floor… if necessary"。结论：必要，
且应为 `psutil>=7.1.0`。理由：

| psutil | macOS 身份 | macOS 公开 create_time | Linux 身份 |
| --- | --- | --- | --- |
| 5.9.0 – 7.0.x | 未修正的内核 birth time（稳定） | 无修正（稳定） | wall-clock（随时钟跳变而变） |
| ≥ 7.1.0 | `monotonic=True`（稳定） | 有 boot-time 修正（本事故来源） | monotonic（稳定） |

macOS 在两段都安全；但 Linux 上 <7.1.0 的身份会随时钟跳变改变，配合 (c) 的粘性标志，
一次 NTP 跳变就会把保留引用永久毒化、让 stop 无法再发信号——这正是仓库当初引入
starttime ticks 要避免的。pyproject 当前 `psutil>=5.9.0`（pyproject.toml:69），锁文件 7.2.2。

### 4. 保留监听器监控、去掉持续身份认证，是否自相矛盾 —— 否

TCP 监听器策略回答的是「我们的进程在做什么」，身份只用来回答「哪些是我们的进程」。
设计把身份降级为归属簿记（psutil 引用 + `children()`），策略本身不变，这在逻辑上是干净的。
两处措辞要落地：`_assert_no_tcp_listener`（939）里 `pid not in live_processes` 现在抛
"ownership changed"，设计已要求改成竞争 no-op（等 `_watch_child`）；`has_tcp_listener`
（3654）应直接对保留引用调用 `net_connections`，对已 not-running 的引用跳过，对
AccessDenied 继续按现策略失败关闭。注意 psutil `children()` 每次都会做一次库内身份检查
（`_raise_if_pid_reused`），这不违背设计——设计承诺去掉的是 Avibe 自己的时间戳认证。

### 5. 恢复范围是否符合 owner 意图 —— 符合，且产品决策已写明

「Recovery scope」一节明确：去掉假触发，不新增后台再确认或自动重试承诺；自动预算耗尽后
只有显式 Wake。这与 owner「标准子进程生命周期 + 终止/孤儿边界最小保障」的选择一致。
需要 owner 知情的一点：replay 复现的第二个缺陷（预算耗尽后 claims 一直暂停）在**其它**
瞬时原因下仍可能出现（例如 helper 挂住 10 s 后自行退出），这是被接受的产品行为，不是本
设计的缺口。建议只做两件轻量事：troubleshooting 文档写清「degraded + memory_wake_failed →
手动 Wake」；验收项 6 追加「预算耗尽后状态可见、手动 Wake 成功」。不建议扩大需求。

### 6. 最小完整建议

**删除（从设计或实现范围中）**
- `_monitor_child` 的 0.2 s `_host_identity_is_live` 分支及其 `_SAFETY_MONITOR_INTERVAL_SECONDS`
  语义（保留 1 s 树/监听器扫描）。
- `_wait_for_ready` 的 "ownership changed before readiness" 判定。
- `_live_owned_processes` / `_confirmed_owned_processes` / `_group_contains_only_confirmed_owned_processes`
  / `_signal_owned_processes` 中的时间戳相等逻辑（改为引用相等 / `is_running`）。
- `_wait_for_owned_exit` 里 `_SystemProcessHost()` 的现场构造（3400）。
- 设计里的「running projection 看 `_down`/`_starting`」段落（另立议题）。

**保留（不动）**
- `_watch_child` 作为唯一直接子进程退出路径；TERM → 有界等待 → KILL → 最终等待。
- TCP 监听器策略、组信号「全员确认」前提、pgid 无权威、leader 退出后的组清扫、
  `retire_if_group_is_clear` 的失败关闭。
- `SidecarOwnership` / `ReleasedEverOSOrphanReconciler` 的分类器与 released 记录 schema；
  Linux `starttime_ticks` 仅服务记录路径。
- supervisor 的有界重试预算与 Wake 语义；`core/process_isolation.py` 不动。

**澄清（写进设计后再派工）**
- 引用持有位置：建议 `EverOSProcess._owned_processes` 值类型替换，host 保持无状态。
- 孤儿路径三个函数改用分类时捕获的引用（3(a)）。
- 「未知成员」定义与 `-1.0` 哨兵的替代（3(b)）。
- 引用粘性不变量（3(c)）。
- `processing_healthy()` 列为消费者（3(d)）。
- `psutil>=7.1.0` 下限及理由（3(e)）。

### 7. 测试是否成比例、有意义 —— 是，需分清证据层次

- **已有的探针证据**只证明：Darwin arm64、psutil 7.2.2、一台主机、`patch.object(osx, 'boot_time')`
  注入下，psutil 公开等价/终止行为正确。它不证明 Avibe 实现正确，也不是跨版本/Intel 认证。
  设计对此表述准确。
- **证据项 1**（偏移期间跑完多个扫描周期）只能通过在测试进程内 patch psutil 私有
  `_psosx.boot_time` 实现，这是故障注入而非产品代码依赖，可接受；但请标注它绑定 7.1.0+ 的
  内部结构（psutil 8.0.0 dev 已在改 `__eq__`/`__hash__` 对未知 ctime 的处理，#2895/#2899）。
- **证据项 3**（延迟 asyncio 回调 + 真正不同代的进程，而非只有 NoSuchProcess stub）是本设计
  最有价值的新测试，保留。
- **证据项 7**（Incus 全链路）验证的是共享运行时行为，Incus 是 Linux，不能替代 macOS 原生
  项 1/2；设计已区分，请在 PR 描述里按层列出。
- 建议补两条小的：探针超时清理在偏移期间的行为（3(d)）；`pyproject` 下限的解析级契约测试
  （避免只改锁文件）。
- 场景 ID：扩展 MEMORY-WAKE-001 / -202 合理；新 ID 需对最新 master 的 catalog 分配。

## 二、阻断项

无。

## 三、必须澄清项（PASS WITH CHANGES 的「changes」，非阻断，但应在派工前落到设计文档）

1. `psutil>=7.1.0` 依赖下限与理由表（3(e)）。
2. 点名 `_terminate_orphan_tree`、`_terminate_claimed_processes`、`_reap_unidentified_child`
   改用分类时捕获的引用；`_wait_for_identities_exit` 改为引用活性（3(a)）。
3. 定义「未知成员」及 `-1.0` 哨兵的替代（3(b)）。
4. 决定引用持有位置，并删除 `_wait_for_owned_exit` 的现场 host 构造（问题 2）。
5. 写明引用粘性不变量（3(c)）。
6. 把 `processing_healthy()` 探针列为同一助手的消费者并加一条证据（3(d)）。

## 四、可选简化

- 移除「running projection」段落，另立议题。
- 引用值替换而非 host 有状态化（若采纳则测试假 host 改动最小）。
- troubleshooting 文档一句话 + 验收项 6 追加「手动 Wake 可恢复」。

## 五、证据索引

- 假故障链路：process.py 2402、3273、3238、3247、3293、785、715、2259、3364、3374。
- 停止失败链路：3352-3370（引用闸门）、3386-3417（等待）、2250-2290（TERM/KILL）。
- 孤儿路径时间戳活性：1313、1412、1448、1590、1681、3184。
- 哨兵：3221、3052、917-937。
- host 构造点：345、1047、1241、1746、1929、3400。
- running 投影：process.py 371；supervisor.py 152；runtime.py 1106、1337、2855。
- psutil 7.2.2（.venv）：`_get_ident` 363-384、`__eq__` 430-449、`is_running` 625-645、
  `_raise_if_pid_reused` 459-470、`_send_signal` 1259-1282、`children` 951+。
- psutil 各 tag（release-7.0.0 ~ 7.2.2）`_get_ident` 与 `_psosx.INIT_BOOT_TIME` 对照：
  7.0.0 无 OSX monotonic 分支且无修正；7.1.0 起两者同时出现。
- 依赖：pyproject.toml:69 `psutil>=5.9.0`；uv.lock psutil 7.2.2。
- 探针：docs/investigations/issue-1990/public_process_probe.py、public-process-result.json。
- 场景：tests/scenarios/memory_repair/catalog.yaml MEMORY-WAKE-001（26）、-202（75）。
