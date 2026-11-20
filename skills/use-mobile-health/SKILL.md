---
name: use-mobile-health
description: Read steps, activity, workouts and sleep synced from the user's iPhone to their own Avibe instance, or connect, refresh and revoke that synchronization.
requires: mobile-device
---

# Mobile health

Use the local CLI. Every Agent in this instance may read. Anyone who can reach
this instance and its Agents can see synced data; the owner controls that
through the instance's access settings. Reading puts values into the current
model context and conversation. Read only the
metrics and dates relevant to the user's question.

```bash
vibe mobile health status
vibe mobile health read --metric sleep,steps --period last7d --max-age 6h
vibe mobile health read --metric activity,workouts --from 2026-11-01 --to 2026-11-07
vibe mobile health read --metric steps --period today --refresh --wait 20
vibe mobile health connect --wait 0
vibe mobile requests show mreq_0123456789abcdef0123456789abcdef
vibe mobile requests await mreq_0123456789abcdef0123456789abcdef --timeout 20
vibe mobile health revoke
```

If reading returns `not_connected`, run `connect`. Its conversation card and
notification let the user authorize on iPhone. Authorization happens once in
Avibe's native confirmation and iOS Health; the browser receives no health
values or device credential.

`read` uses local daily data. Check `availability`, freshness and all three
times (`latest_sample_end_at`, `queried_at`, `received_at`). `empty` means no
samples, not zero or denial. `not_synced` means no upload for that day. A null
sleep component is missing data; never replace it with zero.

`--refresh` asks the phone to query and upload. `device_wait_timeout` is not a
successful refresh: use existing data in `details.data` and state its time.
Callbacks default to `AVIBE_SESSION_ID`; `--callback-session-id ID` chooses
another session, and `--no-callback` suppresses continuation. The synchronous
waiter or the callback continues once. Callback messages contain status only;
run `read` to obtain the data.

Use `revoke` when the user asks to stop synchronization: it revokes devices and
deletes instance health data. Do not export values to Show Pages, memory, logs,
or another service. Sync stays bound to the instance owner; health data rolls
off after 30 days.

中文：用本机命令按需读指标和日期，并说明数据时间。未连接时发起连接，请用户在
iPhone 授权；空数据不代表零或拒绝。刷新超时使用已有数据，不声称刷新成功。
用户要求停止时执行 revoke。数值会进入当前模型和会话，不写入 Show、记忆或日志。
