# OpenCode poll settles the owning assistant, not messages[-1]

## Background

Two live OpenCode sessions hung with Avibe still polling: a completed
assistant already carried `UnknownError: unknown certificate verification
error`, then a user inject ("继续" / a watch callback) and an empty leftover
generation sat after it. The poll loop only inspected `messages[-1]`, so the
error never became a terminal result.

## Goal

Settle from the assistant that owns the current turn, using both the message
snapshot and native session status. A trailing user while OpenCode is
busy/retry is a live steer or auto-retry. The same snapshot while idle is a
hang (watch callback / typed "继续" after a completed error) and can settle.
An unreadable status is treated as live so an accepted steer is not closed.

## Solution

`_settlement_assistant_message` walks the snapshot backward. Native liveness
comes from the same status sample that produced the snapshot when the
steering wrapper already read it; otherwise from ``/session/status``. A
successful omission (``None``) is idle. An unread or failed status is live.
