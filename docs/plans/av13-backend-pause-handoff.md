# AV-13 Runtime 后端暂停交接（2026-11-21）

分支 `feat/av13-local-rebind`。这是可恢复的本地提交，不是已验收的 backend head。仅在负责人明确恢复后由同一写方继续；不定时自启，不移交 CLI 或页面写方。

## 停在哪里

- 已接通受约束领取的本机 start/state/cancel、独立 cookie 和共用回跳；普通领取不能查看或取消重绑。合成发行端已覆盖授权、拒绝、保持 Off 和精确配对重放。
- `c8e510eac` 让启动轮询先驱动可重放的配对 journal；`aa2f6d97e` 在发布新配置前退休旧设备注册和远程推送；`b527e70e7` 在 reset intent 存在时阻止配对、改名和旧配对重放。
- 本提交保存 `vibe/binding_reset.py` 的**内部**分步执行器：在受保护目录记录阶段和代际指纹，退休旧权限、清配对配置、清 journal 和 outcome 后删除安装身份、清改名记录、最后清 Health。`vibe/instance_address.py` 增加清理响应的同进程排空。`tests/test_binding_reset.py` 用临时数据根模拟阶段副作用完成、但下一阶段尚未落盘的中断，另测源配置已更换时拒绝继续。

## 恢复后从这里接

1. 从 `binding_reset._seal`、`_verify_stage`、`resume` 及 `remote_access._drive_pending_pairing` 开始。现在启动 worker 见 reset 标记只暂停；**尚未调用 reset 恢复**。先测试损坏标记、文件被替换、部分损坏配置和真实 Health 数据，再接 worker；错误必须保留围栏，而不能重新开始领取。
2. `_seal` 的假设是调用方**已完成授权**，且持有 reset admission 与 config lock。当前只有内部测试调用，没有 Web 或 CLI reset 入口。Web preview/confirm 须绑定本次合格机器拒绝、原浏览器、当前代际和短期单用确认；CLI 的无代理本机交互例外要单独验证。不把通用拒绝、网络故障或状态 GET 当作授权。
3. 核实跨进程清理响应与旧流排空（CLI 与 UI 未必同进程）、同 ID/owner 的 Health 保留与新身份的 purge、旧 account sign-out 队列及改名清理授权。真实 Health 存储、损坏 journal/UUID、迟到 writer、恢复时已经出现新代等情形还不能由现有阶段测试担保。

详细契约：`docs/plans/local-remote-access-rebind.md`。聚焦测试用工作树 `.venv/bin/python -m pytest -q tests/test_binding_reset.py tests/test_binding_retirement.py`。未完成独审、CI、Incus 或 Production 演练；未开 PR，不发布或开启 reset。
