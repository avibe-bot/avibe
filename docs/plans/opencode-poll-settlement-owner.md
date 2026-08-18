# OpenCode poll settles the owning assistant, not messages[-1]

## Background

Two live OpenCode sessions hung with Avibe still polling: a completed
assistant already carried `UnknownError: unknown certificate verification
error`, then a user inject ("继续" / a watch callback) and an empty leftover
generation sat after it. The poll loop only inspected `messages[-1]`, so the
error never became a terminal result.

## Goal

Settle from the assistant that owns the current turn: skip trailing user
injects; treat an empty in-flight assistant after a completed error as leftover
generation, not a live turn. A follow-up assistant that already has parts stays
the live turn.

## Solution

`_settlement_assistant_message` walks the snapshot backward. Both prompt and
restored poll loops use it instead of `messages[-1]`.
