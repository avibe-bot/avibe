# Avibe Computer Use JavaScript SDK

**Status:** Owner-facing design record  
**Date:** 2026-10-14  
**Scope:** macOS desktop Computer Use, v1  

## Decision

Agents will drive the desktop by writing ordinary JavaScript scripts that
import the Avibe SDK. They will not import or call cua's SDK. A proven flow
becomes a reusable Skill: `SKILL.md`, one script, declared parameters, and
declared side effects, stored in the owner's personal Skill directory and
available across projects. An agent may propose saving a flow; the owner
approves before it is saved.

The SDK is the single user-facing abstraction over desktop actions. The
existing `vibe computer` CLI is re-based on the same implementation: a single
step remains convenient on the CLI, while scripts provide sequencing,
branching, waits, recovery boundaries, and a JSON return value.

## Why Avibe owns the SDK

Cua's SDK would bypass Controller admission and its allowlist, exact-window
checks, observe-first rule, desktop lease, and foreground consent. Its
in-process driver would also attribute macOS TCC to the wrong identity, and
its N-API runtime is MPL-2.0. The Avibe SDK therefore sends every action
through the same Controller path as `vibe computer`; there is one safety rule
set and one owner-visible policy.

The current CLI already establishes this boundary. It reads only the injected
`AVIBE_SESSION_ID`, calls the Controller's `/internal/computer-use` endpoint,
and never accepts a client-provided Turn identifier. The Controller checks that
the Session has an active Turn before admitting the request, then applies the
Session-keyed driver state and desktop lease. On POSIX the transport is HTTP
over an owner-only Unix `dispatch.sock`; on Windows the existing control IPC
uses authenticated loopback TCP with a bearer token and instance binding. The
Node SDK must reuse that discovery, authentication, and Session binding path,
not invent a second endpoint. See [`vibe/cli.py`](../../vibe/cli.py),
[`vibe/internal_client.py`](../../vibe/internal_client.py), and
[`core/internal_server.py`](../../core/internal_server.py).

## Object model and selectors

The object model is:

```text
computer -> app -> window -> element
```

An element is located declaratively with `role`, `label`, `labelContains`,
`value`, `within`, and `index`. The SDK hides driver element tokens. It
re-observes a window when a token is stale, while the Controller still owns
observe-first admission. The first use of a window observes it automatically.
Zero matches and multiple matches are errors; the SDK never guesses.

Every action returns the existing effect vocabulary, including `confirmed` and
`unverifiable`. Scripts use `waitFor(selector, timeout)` for eventual
availability and `expect(selector).toHaveValue(...)` and related assertions
for post-action state. These are robustness primitives, not permission
overrides: a timeout or failed expectation stops the script.

Representative script shape:

```js
import { computer, avibe } from "@avibe/computer";

export default async ({ to, subject, body }) => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ role: "AXWindow", index: 0 });
  await window.element({ role: "button", label: "Compose" }).click();
  await window.element({ role: "textbox", label: "To" }).fill(to);
  await window.element({ role: "textbox", label: "Subject" }).fill(subject);
  await window.element({ role: "textbox", label: "Message" }).fill(body);
  await avibe.confirm(`Send email to ${to}?`);
  const effect = await window.element({ role: "button", label: "Send" }).click();
  await avibe.notify(`Mail send effect: ${effect.status}`);
  return { to, subject, effect };
};
```

The exact export convention is implementation-owned; the important contract is
that the program is a normal Node module and its returned value becomes the
JSON result.

## Errors and failure recovery

The SDK exposes typed errors: `NotFound`, `Ambiguous`, `Timeout`, `DesktopBusy`,
`ForegroundConsentRequired`, `Unavailable`, and the Controller's stable error
codes. A script failure stops execution and returns the failing step plus a
screenshot path when one is available. This lets an agent fall back to
step-by-step work without claiming that later steps ran.

An action with an ambiguous transport outcome is not blindly retried. The
script or agent observes the resulting state first, matching the existing
Computer Use guidance in [`skills/computer-use/SKILL.md`](../../skills/computer-use/SKILL.md).
GUI text, accessibility labels, clipboard contents, and application output
remain untrusted data.

## Avibe primitives

`avibe.notify(message)` reuses existing message delivery. `avibe.confirm(message)`
asks the owner on the current Web or IM surface and waits for an answer.
Irreversible operations such as send, submit, and pay call `confirm` by
default. A Skill may declare a flow confirmation-free only with explicit owner
approval. Unattended runs may execute only scripts that require neither
confirmation nor foreground control; all others are refused.

Avibe already has `QuestionUIHandler`, which renders questions through an IM
client and waits on a per-Session event until a callback or reply supplies the
answer. It is not currently a generic script-facing, Web/IM-neutral primitive.
The implementation should reuse that existing delivery and wait lifecycle,
extracting only the smallest Controller-owned `ask-and-wait` boundary needed
by `confirm`. This is a continuation primitive, not a new approval system.
Whether the current Web surface can satisfy the same contract is **unverified**
on `origin/master`; the implementation PR must verify it before claiming Web
parity. See [`modules/agents/question_ui.py`](../../modules/agents/question_ui.py).

## Execution and runtime

The command is:

```text
vibe computer run <file.mjs> --arg k=v
```

`@file` values are supported. The command runs on the Node runtime Avibe
already ships, is bound to the current Avibe Session, keeps one persistent
Controller connection per script, and enforces one total script timeout.
Scripts have no privilege beyond the agent's existing shell; only desktop
actions pass through Controller rules.

Desktop packages currently carry private Node `v22.23.3` for macOS and
Windows, with the launcher injecting its absolute path as
`VIBE_SHOW_RUNTIME_NODE_BIN`; the packaged tree also carries npm. The desktop
package engine accepts `^20.19.0 || >=22.12.0`. See
[`desktop/runtime-sources.json`](../../desktop/runtime-sources.json),
[`vibe/desktop_runtime.py`](../../vibe/desktop_runtime.py), and
[`desktop/package.json`](../../desktop/package.json).

The repository does not currently contain `@avibe/computer`, an SDK module
resolver, or `vibe computer run`; those are implementation work. On a
non-desktop install, Node is currently an optional system dependency for the
Show Pages runtime and is not a private Avibe runtime. The exact non-desktop
packaging and the injected module-path mechanism for resolving
`@avibe/computer` are therefore **unverified**. The intended boundary is an
Avibe-injected absolute SDK module path selected by the runner; the exact
environment name and Node loader mechanism belong to the implementation PR.
The SDK must not resolve from the agent's working directory or global npm.
v1 must fail as `Unavailable` when the Avibe SDK runtime is absent. Any new npm
dependency must be recorded in the repository's dependency files in the
implementation change.

## Reuse and boundaries

Skills should prefer app scripting interfaces and service APIs when they exist;
GUI scripting is the fallback because fixed selectors break when UIs change.
The CLI and SDK must share one rule implementation, including effects, typed
errors, observation, lease, foreground consent, and scheduled-run admission.

This record does not design Route B internals, browser access, Windows
Computer Use, or a persistent JS REPL (`js`/`js_reset`). Those remain separate
implementation or design work. The implementation PR owns exact module loading,
script export mechanics, argument parsing details, screenshot artifact
placement, and the Web continuation adapter, subject to the decisions above.
