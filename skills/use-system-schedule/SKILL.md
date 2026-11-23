---
name: use-system-schedule
description: Create, check, or cancel a system alarm or reminder on iPhone or Mac. Use for wake-up alarms and personal reminders; use use-avibe-harness when the user wants an Agent to do work later.
---

# System alarms and reminders

“Wake me at 7” means an **alarm**. “Remind me to collect the parcel at 7”
means a **reminder**. Choose from the user's intent; ask only when the
distinction or intended date/time would change the result. Resolve relative
time using the user's time zone, and include the date when confirming it.
For “check the build tomorrow and tell me the result,” load `use-avibe-harness`:
that requires an Agent to run later.

## Choose where to act

Use the source attached to the actual human input. `[Source: ...]` describes
a terminal; it does not authenticate hardware, prove that a Mac is local, or
authorize a device. A Harness message quoting the user is not a new human
input. Do not borrow a target from another Session or a previous speaker.

| Input | Route |
| --- | --- |
| iPhone App | Request native creation through `vibe schedule`. Say “on iPhone”; promise a particular phone only when Runtime proves that binding. |
| Mac App with trusted evidence that the source Mac is the Computer Use host | Load `computer-use` and operate Clock or Reminders on that Mac. |
| Remote Mac App, or Mac App without that evidence | Ask “Do you want to create it on the Mac running this Avibe instance?” If they confirm that host, use Computer Use there; otherwise clarify their chosen device. |
| Browser, IM, unknown or missing source | Default to the iPhone native route, without claiming to know which iPhone sent the message. |

“This Mac,” a `mac_app` hint, a local user label, loopback URL, or a running CU
driver alone does not prove that the sending Mac is the execution host.
An explicit answer to the host question selects the execution host; it does
not establish where the original input came from. Retain that choice for the
same request instead of asking again on status or cancellation.

Before creating, resolve a material target ambiguity. When multiple iPhones
could receive the request and Runtime cannot enforce the user's selection,
do not submit it to a race between phones. Explain that device selection
cannot be verified and ask which execution path they want; naming a phone
or reopening the conversation on it is not itself proof of enforced routing.
Never silently choose another device, change the kind, or create a Harness
Task because the intended device is offline, locked, unsupported, or denied.

## iPhone: one operation through completion

Use `vibe schedule` from the current Agent Session. The connected instance
must support it and be paired. Native iPhone confirmation owns permission
and creation; a CLI acknowledgement only means the request was recorded.

```bash
vibe schedule create --kind alarm --title 'Wake up' --at '2026-12-01T07:00:00+08:00' --time-zone 'Asia/Shanghai' --operation-id 'wake-up-20261201-0700'
vibe schedule status wake-up-20261201-0700
vibe schedule cancel wake-up-20261201-0700
```

Replace the example title/date/time zone and allocate one operation ID for
the intended item. Use `--kind reminder` for a reminder. Keep that ID for
status, retries and cancellation, including after a timeout or lost reply.
Never generate a replacement while the original outcome is uncertain.

Read `operation.state` and the matching receipt:

- `scheduled`: native creation succeeded. Report the actual kind, title and
  date/time. Do not infer a human device name from an opaque device ID.
- `pending` or `awaiting_device`: ask the user to open Avibe on the intended
  iPhone and respond to its native confirmation. Say it is waiting; do not
  say the alarm/reminder has been created. Check the same operation later.
- `user_denied`, `permission_denied`, `unsupported`, `expired`, `failed`, or
  `stale_context`: state the actual outcome and required next action. Do not
  substitute another kind or device. A new request after a conclusive failure
  is a new explicit user attempt, not a recovery of an uncertain create.
- `cancelling`: cancellation is waiting for the bound phone. Only `cancelled`
  confirms cancellation; keep using the original operation ID.

Inspect the running CLI's `vibe schedule --help` if its command is missing or
different. Missing support is a blocker to report, not permission to install,
restart, or create a Task. Do not poll in a shell loop; for a durable later
status check, use Harness only to observe this operation, never to recreate it.

Capability follows the installed iPhone app and OS, not a version guess:
older apps on iOS 26 may offer alarms only; iOS 17–25 offers reminders.
An alarm permission refusal does not authorize an EventKit reminder.
Use the returned kind/status rather than promising both kinds everywhere.

## Mac: use the system app

Load `computer-use` before interacting. Follow its host readiness, exact-window
observation, permission and locked-desktop rules. Use Clock for an alarm and
Reminders for a reminder. Inspect only the controls and item needed for the
request, not the user's unrelated reminders.

After saving, observe the exact title, intended time/date where applicable,
and enabled/saved state. Report success only after that check. If an action's
result is uncertain, inspect it before retrying so a duplicate is not created.
Cancel only the item identified for this request and verify its resulting
state. A Computer Use refusal is not permission to switch to AppleScript,
a different machine, or a native Mac bridge.
