# Memory 设置页增加 sidecar 强制重启按钮（rev4）

> rev4 根据整体 review 收敛为一个公开入口和四个很小的内部修正：重启配置快照、
> lifecycle busy 快速失败、worker lease 轮换、clear marker 的锁内恢复。不新增
> lifecycle coordinator、状态机、provider/store port 或前端 DOM 测试框架。

## 背景与目标

当 Memory 状态为 `down` / `memory_sidecar_unavailable` 时，Web UI 没有恢复入口：当前重启按钮只出现在 `processing_fault_kind === 'engine'` 的横幅中。与此同时，`EverOSProcess` 只会在子进程退出或启动失败后自动拉起；“进程仍存活但 UDS/health 不可达”不会自动替换进程。

本需求只做两件事：

1. Memory 已启用时，在 setup-stage 分支之外的 Status 动作区始终提供“重启引擎”按钮；即使依赖 API 报告 runtime 未安装、status 尚未加载或读取失败，也不能隐藏入口。
2. 点击后由 controller 强制停止旧 sidecar 并等待新 sidecar `ready`，不重启 Avibe 主进程、不重装 runtime、不从磁盘重新选择 restart 目标配置，也不做 processing 预探测。若启动快照仍带 durable embedding-change marker，则必须先完成既有 root 兼容性检查，并允许 settlement 为核对/清除 marker 读取持久化配置。

非目标：自动健康重启、重做全部 Memory lifecycle、改变 provider/store 协议、重试历史 `unknown` flush、四平台全量回归。

## 为什么不能继续复用 reconcile

- Settings PATCH 与旧 restart 路径分别加载配置，存在 C0/C1 交错，可能把磁盘和 live 配置拆开。
- reconcile 会先等待最长 30 秒的 worker drain，再做 processing probe；长 flush 最长 300 秒，因此它既不满足“5 秒后强制”的产品语义，也可能因无关的 probe 故障拒绝重启。
- `self._config` 不是可靠的“最后成功配置”：enabled reconcile 会在 root 校验和新进程启动完成前写入候选；候选及 UI rollback 都失败后，它可能仍指向失败的 C1。

因此保留现有 UI 路径 `/api/memory/runtime/restart`，但后端改走专用的 `MemoryRuntime.restart()`。

```text
UI
  -> POST /api/memory/runtime/restart
  -> internal_client.memory_restart()
  -> POST /internal/memory/restart
  -> MemoryRuntime.restart()
```

## 最小后端设计

### 1. 可重放配置快照

在 `MemoryRuntime` 增加私有 `_restart_config`：

- 初始化为启动时 `MemoryConfig` 的深拷贝，使首次启动失败后仍可人工重试同一份配置。
- 只在 enabled 或 disabled reconcile **成功结束**时，在 `_reconcile_lock` 内更新为深拷贝。
- 失败候选不能更新它。之所以必须深拷贝，是因为 `MemoryConfig` 及其嵌套配置可变，`embedding_change_pending` 也会在运行时被修改。
- `restart()` 在 `_reconcile_lock` 内读取其深拷贝，并在 safety guard 通过后、替换进程前同步恢复 `self._config`；这样 worker 的 `enabled` callback 和新 child settings 都不会继续使用失败候选。
- 启动时的初始快照可能带 `embedding_change_pending=true`。restart 必须复用 reconcile 的 embedding/root safety guard，并只在 guard 成功且 marker settlement 成功后继续；有旧向量数据或 root 状态不可判定时返回 `memory_clear_failed`，不得强行启动。settlement 核对持久化配置仍与快照相同后，将磁盘、`_restart_config` 和随后恢复的 `self._config` 三处 marker 同步为 `false`，不改变其他配置；这里不运行 processing probe。

这个字段只回答“人工 restart 应重放哪份配置”，不重新定义现有 `_config`，也不引入配置版本模型。

### 2. clear marker 与进程替换原子化

现有 `_recover_interrupted_clear()` 在取得 module lifecycle lock 前检查 `_clear_active`。并发 clear 若在这个间隙失败并留下 durable marker，restart/reconcile 可能随后启动 child 而不再检查 marker。

最小修正：

- 从现有方法抽出 `_recover_interrupted_clear_locked()`；调用方必须已持有 `module._lifecycle_lock`，方法内部再按现有顺序取得 root lifecycle lock 并重新读取 durable marker。
- 原 `_recover_interrupted_clear()` 保留为 wrapper，供 search/profile/status 等调用；它继续保留 active clear 时的快速返回，维持 read 路径现有的立即失败/`clearing` 行为。
- `reconcile()`、artifact activation 和新 `restart()` 都按 `_reconcile_lock -> module._lifecycle_lock -> root lifecycle lock` 的既有顺序，在同一 lifecycle 临界区内完成 marker 恢复和 child replacement。
- 新的 locked helper 自身不做 `_clear_active` 快速返回；持有 lifecycle lock 时不会与 active clear 并行，因此必须重新读取并处理 durable marker。

这只是消除现有 check/use 窗口，不新建通用 lifecycle coordinator。

### 3. 不排队的 restart 入口与有界 deadline

显式 restart 不能排在另一个 lifecycle 操作后面静默等待。否则 transport 可能先超时，controller 随后仍执行已经失去调用方的 restart，用户重试还会再排入一次。

- `restart()` 在任何 await 之前检查 `_reconcile_lock` 和 `module._lifecycle_lock`；任一已占用就立即返回 `{ok: false, error: 'memory_restart_busy'}`，不进入 lock waiter 队列，也不修改 process、claims 或 worker。
- 两把锁都空闲时，在同一 event-loop turn 内按 `_reconcile_lock -> module._lifecycle_lock` 立即取得它们，中间不执行其他 await。这样并发 restart、Settings PATCH、artifact activation 和 clear 只有一个 owner；已有 reconcile/clear 保持原语义，只有人工 restart 使用 fail-fast 契约。
- interrupted-clear recovery 和条件性的 embedding/root guard 复用 `CLEAR_CLEANUP_TIMEOUT_SECONDS` 作为上界。restart transport deadline 必须严格大于 `clear recovery/guard bound + 5s worker grace + process stop 的 TERM/KILL 两轮上界 + process startup bound`；测试直接从这些源常量计算，不复制注释里的数字。
- lock-busy 是一个已完成、可重试的业务响应，不是 `memory_restart_failed`，也不能启动后台 task。前端显示本地化 busy 原因并结束 spinner。

这不增加独立 restart lock 或队列；现有两把 lifecycle lock 就是 single-flight 所有权边界。

### 4. 强制替换与 lease 交接

`MemoryWorker._boot_id` 当前在对象创建后不变，而 `recover_after_boot()` 只回收“其他 lease owner”的 `processing` 行。若 restart cancel 了已经 claim add 的同一个 worker，再用原 owner 激活，该行会永久停在 `processing`。

给 `MemoryWorker` 增加一个私有语义的 helper，例如 `begin_replacement_activation()`：生成新的 UUID lease owner，然后复用现有 `begin_activation()`。MemoryStore 和 recovery SQL 不改。

锁内执行顺序固定为：

1. 校验 store、artifact、enabled 状态，并完成 interrupted-clear recovery。
2. `pause_claims()`，给当前 drain 最多 5 秒优雅结束；超时或普通 drain 错误只记录并进入强制阶段，不作为失败返回。仅 task cancellation 进入下面定义的取消清理。
3. cancel 并等待旧 worker task 结束。
4. 轮换 lease owner。该动作必须发生在旧 task 结束之后、任何新 worker activation 之前。
5. 仅当快照带 `embedding_change_pending` 时，在 claims 已 fence、旧 worker 已停止的状态下执行既有 embedding/root guard 和 marker settlement；通过后才恢复 `self._config`。
6. 停止旧 process；只有 `stop()` 成功后才能丢弃旧 supervisor，禁止在旧进程未确认回收时启动第二个 child。
7. 用 `_restart_config` 创建并启动新 process。`start()` / `on_ready` 成功后再恢复 claims 和 worker；同步返回 `{ok: true, state: 'ready'}`。

`restart()` 不调用 `_probe_processing`。条件性的 embedding/root guard 是防止混用向量空间的持久化安全约束，不是 processing 健康预探测；其余真实 processing 故障继续由 worker 的既有分类路径报告。

### 5. 失败后置条件

claims 被 fence 后的每条退出路径必须明确恢复到以下二者之一：

- **旧 child 仍由原 supervisor 持有且 `running`**：保留该 supervisor，使用新 lease owner 重新 activation，恢复 claims/worker，返回 `memory_restart_failed`。系统退回重启前状态，且不会出现双 child。
- **旧 child 已停或新 child 启动失败**：保留新 supervisor（如果 factory/start 已创建）；claims 和 worker 保持暂停，设置可见 runtime error。若 supervisor 后续 supervised `on_ready`，它会按既有路径恢复 claims/worker；没有 supervisor 时，用户可再次点击 restart。

`CancelledError` 在完成所有权和 claims 清理后继续抛出，不伪装成业务失败。为此要修正现有 `_stop_worker()`：只吞掉被 cancel 的 drain task 所产生的 `CancelledError`；若当前 lifecycle task 自身处于 cancelling 状态则继续抛出。restart 在 orchestration 边界以 shielded cleanup 收敛到上面的单-child/claims 后置条件，再重新抛出取消。若取消发生在新 `start()` 已创建 child、但 watcher/monitor 尚未建立的窗口，cleanup 必须 shield `new_process.stop()`：回收成功才置 `_process=None`，回收失败则保留 supervisor 引用并继续 fence claims，绝不遗留无所有权引用的 child。

`start()` 正常返回 `False` 时沿用更具体的 `memory_sidecar_unavailable`；stop、factory 或其他 restart orchestration 异常使用新的 transport-only `memory_restart_failed`。

### 6. 队列语义

- cancel 发生在 add 已 claim 之后：新 lease owner 的 activation 会把旧 owner 的行退回 `pending`，按既有 at-least-once 语义重投。若 provider 在取消前已接收但本地未观察到 ack，可能产生重复副作用；强制重启不能承诺 exactly-once。
- cancel 发生在 flush `in_flight`：activation 会把它永久标记为 `unknown` 并打开 processing fault。历史 `unknown` 不会在 5 分钟后自动重试或消失；以后新的 pending work 成功 flush 可以关闭 fault，但不会改写该历史记录。

## 接口与 UI 改动

### 后端与 transport

1. `core/memory/runtime.py`：增加 `_restart_config` 和 fail-fast `restart()`；成功 reconcile 提交快照；修正 `_stop_worker()` 对调用方取消的识别。
2. `core/memory/module.py`：抽出锁内 clear recovery helper；现有 wrapper 继续服务其他调用方。
3. `core/memory/worker.py`：增加 replacement activation 时的 lease owner 轮换。
4. `core/internal_server.py`：增加 `POST /internal/memory/restart`。runtime 缺失返回 `memory_runtime_missing`；未处理异常映射为 `memory_restart_failed`，不能复用语义错误的 `memory_reconcile_failed`。
5. `vibe/internal_client.py`：增加 `memory_restart()` 和独立 timeout。timeout 按上面的完整 restart lifecycle budget 计算，lock busy 不计入预算，因为它不等待。
6. `vibe/ui_memory_routes.py`：现有 `/api/memory/runtime/restart` 改调 `memory_restart()`，保持同源校验和外部路径不变。
7. `core/memory/types.py` 与前端 en/zh `errors`：加入 transport-only `memory_restart_failed` 和 `memory_restart_busy`；不加入 SQLite 持久化 schema。

### 前端

1. `memoryRead.ts`：在现有 Memory response classifier 附近定义并导出 `MemoryRuntimeRestartResult` 及纯函数 normalizer。接受 `{ok:true}`；把 `{ok:false,error}` 和 `{status:'failed',error}` 统一为失败；malformed body 也必须 fail closed。
2. `ApiContext.tsx`：导入该类型和 normalizer，`restartMemoryRuntime()` 只返回归一化结果，避免反向 import 形成循环依赖。
3. `SettingsMemoryPage.tsx`：当 `settings?.enabled === true` 且页面不是 cross-origin forbidden 状态时，在 `remoteUnavailable` / `memorySetupStage()` 条件树之前渲染 page-level Status 动作行。secondary `xs` 重启按钮使用 `RotateCw` / `Loader2`，`restarting` 时 disabled；因此 `runtime-required`、setup loading、status loading/error 都共享同一个入口。请求同步等待结果；成功 toast 使用完成式文案，失败 toast 显示本地化原因和字面 error code，无 code 时显示通用失败。
4. `MemoryStatusPanel.tsx`：删除 engine fault 横幅中的旧重启按钮和相关 props，避免正常 Status body 出现第二个入口；credential fault 的“打开设置”保持不变。
5. `en.json` / `zh.json`：同步增加按钮、完成、失败、`memory_restart_failed`、`memory_restart_busy` 文案，删除不再使用的 engine 横幅 action 文案。

## 最小充分测试

### Python

- `tests/test_memory_runtime.py`
  - 成功 restart：旧 process 确认 stop，新 process 启动并 ready，worker 恢复。
  - C1 reconcile 失败且 rollback 也失败后，restart 重放最后成功的 C0；无竞争时的 restart 仍在两把 lifecycle lock 内完成。
  - 分别预占 `_reconcile_lock` 和 `module._lifecycle_lock`，以及同时发起两个 restart，验证竞争者立即返回 `memory_restart_busy`、不进入 waiter 队列且不修改 process/claims/worker。
  - 分别挂起 add 和 flush，使用缩短的 grace 验证有界完成；add 可由新 owner 再 claim，flush 变 `unknown` 并打开 fault。
  - durable clear marker 恢复与 replacement 位于同一 lifecycle 临界区；恢复失败不启动 child。
  - 初始 `_restart_config.embedding_change_pending=true` 时：有旧向量数据必须拒绝启动；空 root 完成 marker settlement 后才允许启动。
  - disabled 不 spawn；`start() is False`、factory exception、stop exception 分别验证错误码、`_process`/claims/worker 后置条件和无双 child。
  - lifecycle task 分别在 worker 停止期间、以及 new `start()` 已创建 child 后被 cancel：完成最小 cleanup，验证 child 被回收或仍由唯一 supervisor 持有、claims 后置条件正确，并重新抛出 `CancelledError`。
- `tests/test_internal_server.py`：成功透传、runtime missing、未处理异常映射。
- `tests/test_internal_client.py` / `tests/test_internal_client_timeouts.py`：POST 路径、busy 响应透传、timeout 从 clear/worker/process 源常量覆盖完整有界预算。
- `tests/test_ui_memory_routes.py`：切到新 client、internal unavailable、既有 cross-origin 拒绝。

### TypeScript

- 给纯 normalizer 做表驱动测试：`ok:true`、`ok:false`、`status:'failed'`、malformed。
- 沿用 React SSR / `renderToStaticMarkup`，不引入 DOM 框架：给 `SettingsMemoryPage` 注入 enabled + runtime-missing fixture，断言 setup prompt 与唯一重启按钮同时存在；另覆盖 status loading/error、restarting disabled。`MemoryStatusPanel` 测试断言 engine fault 不再渲染重启入口、credential action 仍存在。
- 不为 click/toast 新引入 DOM 测试框架；由下面的单一手工场景覆盖。

## 验证

1. `pytest tests/test_memory_runtime.py -k restart`
2. `pytest tests/test_internal_server.py tests/test_internal_client.py tests/test_internal_client_timeouts.py tests/test_ui_memory_routes.py -k 'memory and restart'`
3. `ruff check` 所有改动的 Python 文件。
4. `cd ui && npx vitest run` 相关 normalizer / `SettingsMemoryPage` / `MemoryStatusPanel` 测试，然后 `npm run build`。
5. 只使用本地 Incus `master` 回归环境：先运行 `./scripts/run_regression.sh` 更新代码和检查服务健康，不重置配置。
6. 通过 `python3 scripts/incus_regression.py shell --target master` 在容器内确认受管 sidecar PID 后发送 `SIGSTOP`，制造“进程存活但不可达”；在 Web UI 确认状态 down、按钮常驻、spinner/toast 正确，并验证点击后 PID 被替换且状态恢复 ready。若 replacement 未完成，清理时对原 PID 发送 `SIGCONT`。
7. 再用 runner 检查 Avibe service health。不要重启本机 `vibe`，也不要用 `kill -9`（它只覆盖已有的 child-exit supervision，不是本需求场景）。

## 整体 review 结论

方案的必要复杂度来自五个已有安全/并发契约：成功配置不能被失败候选覆盖、人工 restart 不能在 transport 背后排队、pending embedding 不能绕过 root guard、已 claim 行必须换 lease 才能恢复、clear marker 必须与 root/child lifecycle 串行。实现只增加一个私有快照、两个窄 helper 和两个准确的 transport-only 错误码，并复用现有 lock、guard、recovery SQL、supervisor 和 UI primitives。

本方案明确不引入通用 coordinator、显式 restart 状态机、新 port、新数据库字段、自动重启策略或新前端测试框架。实现中若需要这些内容，应先回到本计划重新论证，而不是顺手扩 scope。

## Todo

- [ ] runtime snapshot + pending-embedding guard + fail-fast force restart + focused tests
- [ ] worker lease rotation + add/flush recovery tests
- [ ] locked clear recovery + race regression test
- [ ] internal server/client + closed/busy errors + bounded timeout tests
- [ ] UI route + response normalizer
- [ ] page-level status action + deduplicated banner + runtime-missing render test + toast/i18n
- [ ] focused Python/TypeScript validation + Incus `SIGSTOP` scenario
