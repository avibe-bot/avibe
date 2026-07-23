# Show Page Annotation — Phase 2: Agent Reverse Marks (full loop)

Status: draft for owner review (CLI surface is the owner-review focal point).
Positioning ruled by owner 2026-07-22: reverse marks are **conversation
bubbles, not document comments** — every lifecycle rule below derives from
that.

## Goal

Close the second half of the annotate loop: the user points at the page and
asks; the agent answers **on the page**, anchored to what the user pointed at.
Chat keeps the transcript; the page carries the conversation.

Two complementary mark kinds:

| Kind | Authored via | Nature | Lifecycle owner |
| --- | --- | --- | --- |
| **Reply mark** (回应型) | `vibe show reply` (CLI, event-backed) | Answers a specific user annotation | Read-to-retire pairing |
| **Note mark** (说明型) | `vibe show mark` (CLI) or `agent-note` attribute (declarative, in page source) | Agent's own callout on an element | CLI: replace-on-same-target + read-to-fade; attribute: lives/dies with the source |

## Agent-facing API (owner review section)

### 1. `vibe show reply` — answer a user annotation on the page

```bash
vibe show reply <show-event-id> --message "因为 W30 那周切换了数据源，旧口径少统计了周末的通过数。"
```

- `<show-event-id>` is copied verbatim from the dispatched annotation message
  (see "Dispatch guidance" below — the message now prints a ready-to-run
  command, so the agent never has to construct anything).
- Auto-resolved: target anchor (from the referenced annotation), session id
  (injected env), mark kind (`reply`), pairing (this mark answers that
  annotation).
- Optional: `--message-file <path>` for long/multiline answers.
- Errors are instructive: unknown/foreign event id → lists the session's
  recent annotation ids; already-replied → says so and points to
  `vibe show marks`.

### 2. `vibe show mark` — proactive callout on an element

```bash
vibe show mark <target> --message "本区块已切换到新数据源，历史口径见下方注脚。"
```

- `<target>` (positional, new; `--target` kept as alias for compat):
  a `mark-*` anchor name (`summary.conclusion`) or a CSS selector
  (`#revenue-card`, `.chart-title`).
- Same target marked again → **replaces** the previous note (no stacking).
- Session id injected; `--scope` stays optional (default `default`).

### 3. `agent-note` attribute — declarative note in page source

```jsx
<section mark-default="q3.summary" agent-note="这里我改用了新数据源">
```

- Any element authored by the agent can carry `agent-note="<text>"`; the
  overlay renders the standard violet mark for it. Complements the `mark-*`
  anchor attributes: `mark-*` = anchors (WHERE), `agent-note` = the agent's
  words (WHAT).
- Lifecycle = source lifecycle: edit/remove the attribute and the mark
  follows on next render. No events involved.
- Naming revision (2026-07-23, review finding #269): originally specced as
  `mark-note`, which collides with the anchor attribute family — `mark-<scope>`
  is emitted by `markAttributes(id, scope)` for ANY scope name, so a page
  using scope "note" would have its anchor ids rendered as bogus notes, and
  no `mark-*` content attribute can ever be collision-free against
  free-form scopes. The content attribute therefore lives OUTSIDE the
  `mark-*` prefix. `markAttributes` behavior is unchanged; no public API
  break.

### 4. `vibe show marks` — inspect / tidy

```bash
vibe show marks                          # list active marks (id, kind, target, read state)
vibe show unmark <id|target> [<id|target> ...]   # retire one or more marks (space-separated)
```

`unmark` accepts multiple space-separated ids/targets in one invocation
(owner-approved 2026-07-23); partial failures report per-item results and
exit non-zero only if none succeeded.

### Dispatch guidance (teaching the agent, zero prompting burden)

When a user annotation with `intent: "question"` is dispatched, the message
appended to the agent turn ends with:

```
用户在页面上提出了疑问。请优先把回答放回页面上用户指的位置（chat 里保留一句简短结论即可）：
  vibe show reply show_evt_1a2b3c4d --message '<你的回答>'
```

Other intents get no reply exhortation (change/fix expect page edits;
comment/approve expect acknowledgement), but the event id is always printed
so `vibe show reply` remains available.

## Lifecycle rules (conversation-bubble model)

1. **Unread is loud, read retires.** A mark renders as the violet dot until
   the user expands it. Expanding emits `assistant.mark.resolved` (existing
   event type) → the mark fades to a small gray dot for the rest of the page
   view and is not rendered on subsequent loads. Read state is event-backed
   (cross-device), not localStorage — except attribute notes (no event
   identity), which record read state in localStorage keyed by
   session+anchor+text-hash.
2. **Reply marks are paired.** A reply mark references the annotation event
   it answers. Pair completes when read; a completed pair never re-renders,
   regardless of later page changes.
3. **Cap + aggregate.** At most **5** active (unread) marks render inline.
   Overflow collects into a persistent bottom-right badge (count); its list
   shows every active mark including inline ones. Agents are also guided to
   leave at most 1–2 marks per turn.
4. **Anchor failure degrades, never mis-pins.** Resolution uses the existing
   multi-level fallback with confidence. `missing`/low-confidence marks are
   NOT rendered at a guessed position; they appear only in the badge list,
   labeled "原位置已更新", content still readable.
5. **Replace, don't stack.** CLI `mark` on the same target replaces; `reply`
   on an already-answered annotation replaces the previous reply.

## Implementation slices

- **Lane A (avibe)**: `vibe show reply` + `vibe show marks`/`unmark` CLI
  (reuse mark plumbing; reply resolves the referenced event's anchor and
  emits `assistant.mark.created` with pairing metadata `replyTo`); dispatch
  template gains the question-intent guidance block + always prints the
  event id; `assistant.mark.resolved` accepted from the page (read receipts)
  — POST path already exists, ensure resolved-by-user authoring is allowed
  and author-stamped.
- **Lane R (vibe-show-runtime)**: mark rendering upgrade (unread violet →
  expand bubble → emit resolved → gray fade); `agent-note` attribute pickup
  (registry scan alongside `mark-*` anchors); cap-and-aggregate badge +
  list; missing-anchor list entries; replace semantics on render
  (same target/pair → newest wins).
- No frozen-contract changes: new CLI verbs are additive; `replyTo` rides
  inside the existing mark payload; read receipts use the existing
  `assistant.mark.resolved` type (author = the reading user).

## Acceptance sketch

1. On-page question → agent runs the printed `vibe show reply` → violet dot
   appears beside the questioned element; expanding shows the answer; after
   reading, it fades and never returns on reload.
2. `vibe show mark #revenue-card --message …` twice → single note (replaced).
3. Page with 7 unread marks → 5 inline + badge shows 7; clicking badge lists
   all; reading retires each.
4. Agent rewrites the page section → orphaned mark moves to the badge list
   as "原位置已更新" (never mis-pinned).
5. `agent-note` attribute renders a mark; removing the attribute removes it;
   read state survives reload via localStorage.
