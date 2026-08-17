# Workbench search: opt-in "Include archived"

Issue: https://github.com/avibe-bot/avibe/issues/1082
Branch: `feat/search-include-archived`

## Background

Archived sessions are currently unfindable. `search_messages()` hard-excludes them at
`storage/messages_service.py:291` (`.where(agent_sessions.c.status != "archived")`), so global
message search never returns them. The transcript still exists, the API still serves it, and
`ChatPage` still renders it — there is simply no way to find it. The only route back in is typing
`/chat/<id>` by hand, which requires already knowing the id.

Archive is also the only way to clear a session out of the sidebar, so it doubles as "I'm done with
this, get it out of my way" — a routine action. Every routine archive therefore drops a transcript
out of reach.

## Goal

Add an **opt-in** `include archived` flag to Workbench search. Default behaviour stays
byte-identical to today. When enabled, archived sessions appear in results, are visually marked,
and open **read-only**.

**Hard constraint:** the terminal-archive invariant is untouched. No un-archive, restore, resume, or
fork. `archive_session()` unchanged. `tests/test_session_archive.py` must pass unmodified.

## Resolved design decisions

1. **One boolean `include_archived`, session dimension only.** It lifts only
   `agent_sessions.status != 'archived'` (`messages_service.py:291`). The archived-**project**
   exclusion (`scope_settings.enabled = 0`, `:296`) stays unconditional: project archive is
   reversible (implicit un-archive at `storage/projects_service.py:224-230`) and has its own restore
   flow. Documented in the docstring; a second flag can be added later without breaking this one.
2. **Boolean, not tri-state.** "Archived only" has no demonstrated use case. Use the `Switch`
   primitive, not `SegmentedRadio`.
3. **Not sticky.** Mobile `SearchPage` carries it in the URL (`?archived=1`, mirroring the existing
   `?q=` back-nav contract at `SearchPage.tsx:26-51`); the desktop palette resets it off on every
   open (it already resets the query in `onOpenAutoFocus`, `SearchPalette.tsx:137`). An opt-in that
   silently persists would violate "default byte-identical".
4. **Keep the `visibility == 'foreground'` filters unchanged** in both `search_messages` (`:292`)
   and `list_sessions` (`workbench_sessions_service.py:142`). `archive_session` never touches
   `visibility` (`:957-966`), so archived rows stay `foreground` and pass the filter; background rows
   are internal and must stay hidden. No title/content inconsistency arises.
5. **No title-search backend change.** `SearchPalette` and `SearchPage` do server *content* search
   (`useMessageSearch`) plus client-side app-registry filtering (`search/appSearch.ts`); they never
   call `/api/sessions?q=`. That param serves the composer `#`-mention (`ui_server.py:6130-6131`),
   which must keep excluding archived — you cannot reference an archived session into a new turn.
6. **Include the `update_session` archived guard in this PR.** Small, and it is the server-side
   backstop the read-only UI depends on. New `SessionArchivedError` raised in the service, mapped to
   a `409` carrying code `session_archived` in the PATCH route — same status and code as the
   messages-POST semantics at `ui_server.py:7713`, but in the structured error *body* (see round 4b;
   the flat body those routes use is unreadable to the Web UI's error parser). CLI is already safe
   (`vibe/cli.py:5118` calls `get_active_session` first).
7. **Read-only composer via the existing `disabled`/`placeholder` props** (`Composer.tsx:105-108`),
   not a replacement notice bar — smaller diff, reuses the busy-placeholder pattern at `:591`.
8. **New chat-scoped i18n for the 409.** Leave `errors.session_archived` (`en.json:993`) untouched —
   it is consumed by the global `handleApiError` mapper (`ApiContext.tsx:1921`) and its copy is
   Show-Page-specific. Re-affirmed in round 4b, where the PATCH 409 started actually resolving that
   key: the wording is imprecise for a rename but still states the real reason, and generalizing a
   string shared with every Show Page mutation is a separate call.

## Work items

### 1. `search_messages` flag + `archived` field — `storage/messages_service.py`

- `search_messages()` (`:227`): add kwarg `include_archived: bool = False`.
- Make the filter at `:291` conditional: `if not include_archived:` add the
  `status != "archived"` predicate. All other `.where` clauses unchanged.
- Add `agent_sessions.c.status` to the select (`:268-280`) and `"archived": row["status"] ==
  "archived"` to the session bucket (`:312-318`). Always present (additive; `False` in default mode).
- Docstring (`:235-259`): state the flag, and that archived-**project** exclusion is deliberate and
  unconditional.

Tests in the same commit — `tests/test_message_search.py` (`_seed_session(..., status="archived")`
already exists at `:40`):

- `test_search_include_archived_returns_archived_session` — archived matches surface with
  `archived: True`; active groups carry `archived: False`.
- `test_search_include_archived_still_excludes_archived_project_scope` — mirror
  `test_search_excludes_archived_project_scope` (`:266`).
- `test_search_include_archived_still_excludes_background_visibility`.
- Extend `test_search_excludes_archived_session` (`:249`) to also assert `archived is False` on
  returned groups. Do not weaken it.

### 2. Route: `GET /api/search/messages` — `vibe/ui_server.py:6830-6851`

- Parse `include_archived = request.args.get("include_archived") in {"1", "true", "yes"}` — same
  idiom as `:5728` and `:6158`.
- Pass `include_archived=include_archived` at `:6850`.
- Fix the docstring at `:6834-6836` ("excluding archived sessions" → "archived sessions excluded by
  default; `include_archived=1` opts in").
- Cheap addition: route test in `tests/test_ui_server_fastapi.py` (no `/api/search` coverage today)
  asserting param plumb-through for `0`/`1`.

### 3. Archived guard on `update_session` + PATCH 409

- `storage/workbench_sessions_service.py`: define `class SessionArchivedError(Exception)` near
  `SessionBackendLockedError`. In `update_session` (`:392`), add `agent_sessions.c.status` to the
  `existing` select (`:408-417`) and raise `SessionArchivedError(session_id)` when
  `existing.status == "archived"`, immediately after the `None`/404 check at `:418-419`.
- `core/services/sessions.py`: re-export it (imports `:46-54`, `__all__` `:64-72`).
- `vibe/ui_server.py` `sessions_update` (`:6531-6624`): catch in the try at `:6598-6614` → `409`
  with the repo's structured error body,
  `{"ok": false, "error": {"code": "session_archived", "message": ...}, "code": ..., "message": ...}`
  (revised in round 4b — the first cut used a flat string `error`, which the Web UI parser reads as
  the code). The pre-flight backend-lock read at `:6580` uses `get_session` and is unaffected.
- Tests: `tests/test_core_services_sessions.py` — archived row raises `SessionArchivedError` for
  `title` and for `agent_name` re-route.

### 4. Frontend API layer

`ui/src/context/ApiContext.tsx`:

- `MessageSearchSession` (`:1057-1063`): add `archived: boolean`.
- `searchMessages` signature (`:596`): `opts?: { limit?: number; includeArchived?: boolean }`;
  impl (`:2694-2699`): `if (opts?.includeArchived) search.set('include_archived', '1');` — matches
  the `getProjects` pattern at `:2604`.

`ui/src/lib/useMessageSearch.ts`: add `includeArchived?: boolean` to `UseMessageSearchOptions`; pass
to `searchMessages(trimmed, { includeArchived })` at `:65`; add to the effect deps at `:85`.

### 5. The toggle

- `ui/src/components/workbench/SearchPage.tsx`: derive `includeArchived` from
  `searchParams.get('archived') === '1'`; the toggle writes/deletes the param via the existing
  `setSearchParams(..., { replace: true })` pattern (`:40-51`); pass into
  `useMessageSearch(query, { includeArchived })` (`:31`). Render a `Switch`
  (`ui/src/components/ui/switch.tsx`) + `<label>` row at the top of the results body (above the
  hint/results, `:135`), labelled `t('workbench.search.includeArchived')`.
- `ui/src/components/workbench/search/SearchPalette.tsx`:
  `const [includeArchived, setIncludeArchived] = useState(false)`; reset to `false` inside
  `onOpenAutoFocus` (`:133-140`) alongside `setQuery('')`; pass to `useMessageSearch` (`:42`). Place
  the labelled `Switch` in the footer row (`:213-219`), left of the flexible spacer.

### 6. Archived results visually distinct

`ui/src/components/workbench/search/SearchResultGroup.tsx`: when `session.archived`, render
`<Badge variant="secondary">{t('common.archived')}</Badge>` in the group header (after the label,
before the count, `:26-32`) plus muted styling (e.g. `opacity-70` on the group, or `text-muted` on
the label; match `design.pen` if visual fidelity matters). Add `useTranslation` to the component —
the repo norm — rather than threading a pre-translated label from callers.

### 7. Read-only `ChatPage` — `ui/src/components/workbench/ChatPage.tsx`

- Derive `const readOnly = session?.status === 'archived'` (`status` is already on
  `WorkbenchSession`, `ApiContext.tsx:759`).
- `ComposeProps` (`:2299-2309`) + `Compose` (`:2311`): add `readOnly: boolean`; pass to `<Composer>`
  (`:2320-2331`) as `disabled={readOnly}` and
  `placeholder={readOnly ? t('chat.compose.placeholderArchived') : undefined}`; suppress `autoFocus`
  when readOnly. Pass `readOnly` at the usage site (`:1932-1943`).
- `ChatHeaderBar` (`:2335-2481`): add `readOnly` prop (pass at `:1823-1837`). When readOnly, render
  the title as a static truncated `<span>` (add `readOnly?: boolean` to `TitleFieldProps`
  `:2483-2486`, early-returning the non-editing branch without the button/pencil at `:2502-2512`),
  and replace `AgentRoutePicker` (`:2409-2425`) with static text (agent name or the default label)
  plus `<Badge variant="secondary">{t('common.archived')}</Badge>`. ~~Keep the Show Page toggle —
  viewing works and mutations are already rejected server-side.~~ **Reversed in review round 3 (see
  follow-up 3): viewing does not work either, so the whole Show Page cluster is withdrawn.**
- `sendMessage` 409 handling (`:1221-1223`): before the generic throw, branch
  `if (response.status === 409 && body?.code === 'session_archived')` →
  `setError(t('chat.archived.sendBlocked')); return false;`. Also fix the generic fallback: routes
  return `{"error": ...}`, not `detail`, so use ``body?.error ?? body?.detail ?? `HTTP ${status}` ``.
- `Composer.tsx` hardening (shared component, fix at the right layer): the plain-`textarea` path
  (`:618-635`) never applies `disabled` — add `disabled={disabled}`; gate the attach `+` and mic
  buttons (`:552-584`) with `disabled` too. ChatPage uses the `MentionEditor` path, which already
  honours it at `:592`; this closes the gap for other callers.
- Leave the `onSessionActivity` archived→`goBack()` handler (`:1067-1071`) alone: it fires only on a
  *live* archive event, which cannot recur for an already-archived session.

### 8. i18n — add to both `ui/src/i18n/en.json` and `zh.json`

| Key | en | zh |
| --- | --- | --- |
| `common.archived` | `Archived` | `已归档` |
| `workbench.search.includeArchived` (block at en `:1385`) | `Include archived` | `包含已归档` |
| `chat.compose.placeholderArchived` (in `chat.compose`, en `:2867`, beside `placeholderBusy`) | `This session is archived — read-only` | `会话已归档，仅可查看` |
| `chat.archived.sendBlocked` (new `chat.archived` object under `chat`, en `:2769`) | `This session is archived and read-only.` | `该会话已归档，无法发送消息。` |

Do **not** edit `errors.session_archived` (en `:993`).

## Test plan

- `tests/test_message_search.py` — item 1 cases. Fixture helpers `_seed_session` / `_insert_msg`
  already support `status=` and `scope_settings`.
- `tests/test_core_services_sessions.py` — `update_session` archived-guard cases (item 3). This file
  already covers `list_sessions` (`:169`, `:203`); no `list_sessions` change, so no new cases there.
- `tests/test_ui_server_fastapi.py` — route-param test for `/api/search/messages?include_archived=1`
  and PATCH-archived → 409 `session_archived`.
- `tests/test_session_archive.py` — **run untouched; must pass.**
- Scenario catalog: no capability in `tests/scenarios/INDEX.yaml` covers Workbench search, so no
  entry applies. State this explicitly in the PR description per CLAUDE.md §7.
- UI: vitest tests exist under `search/` (`SearchResultRow.test.ts`, `appSearch.test.ts`). No new
  logic-bearing module is introduced, so no new vitest file is required; `npm run build` type-checks
  the `MessageSearchSession.archived` threading.

## Breakage risks

- **`search_messages` callers** — only the route (`ui_server.py:6850`) and tests
  (`test_message_search.py`, `test_message_type_catalog.py:77`, which calls with defaults). The new
  `archived` payload field is additive.
- **Unread counting / inbox** — untouched by design. `unread_counts` (`:978-992`),
  `unread_counts_by_session` (`:1021-1032`) and `list_inbox_sessions` (`:1167-1171`) keep their
  archived exclusions, so badges can never count archived sessions.
- **`update_session` guard** — two production callers: the PATCH route, and `vibe/cli.py:5119`
  (pre-guarded by `get_active_session` at `:5118`, which already 404s archived). `archive_session`
  writes the row directly, not via `update_session`, so archiving cannot trip the guard. Residual:
  any future internal caller mutating an archived row now raises — the intended contract.
- **`errors.session_archived` copy** — shared via `handleApiError` (`ApiContext.tsx:1913-1921`), so
  the new PATCH 409 surfaces that Show-Page-worded string for non-ChatPage callers. Acceptable (only
  stale or hand-crafted requests reach it); ChatPage uses the new chat-scoped keys.
- **Palette keyboard nav** — archived groups join `flatTargets` (`SearchPalette.tsx:49-62`)
  automatically; opening one routes to the read-only chat. No nav change needed.
- **Composer disabled state** — `archive_session` resets `agent_status` to `idle` (`:962`), so `busy`
  is false and the archived placeholder (not `placeholderBusy`) wins at `Composer.tsx:591`.

## Review follow-ups (Codex, PR #1089)

All three findings were the same root cause: `readOnly` was threaded into the composer but no
further. The shared seam is now `ui/src/components/workbench/sessionArchived.ts` — one module
owning `isSessionReadOnly`, `isSessionArchivedConflict`, `markSessionArchived` and
`transcriptSelectionActions`, so the decisions are unit-testable without mounting `ChatPage`
(the `harnessRuns.ts` pattern).

1. **The archived 409 must converge, not just report** (amends item 7's `sendMessage` bullet).
   A backgrounded/offline tab can miss the archive SSE for a session that already has a
   `native_session_id`; `refreshSessionRowUntilNativeBound` then early-returns on every
   reconnect/focus, so the 409 on the first send is the only point where that tab learns the
   truth. The branch now patches local `session.status` to `archived` — which flips `readOnly`
   and disables the composer — and *then* fires a best-effort authoritative refresh.
   **Patch first, reload second**: the 409 *is* the server's answer, and this is precisely the
   tab whose connectivity is in doubt, so a `getSession` that fails must not leave the chat
   writable. `refreshSessionRowUntilNativeBound` was split so the plain
   `refreshSessionRow` reload is reusable.
2. **The transcript must lose its write controls too.** `Transcript` takes `readOnly`, and:
   - quick replies stay rendered (which options were offered, and the ✓ on the chosen one, are
     part of the transcript) but the group is **locked** via a new `QuickReplies` `readOnly`
     prop, reusing the "answered" lock it already had. Nothing is clickable, so no doomed POST.
   - `SelectionQuoteToolbar.onQuote` became optional and is omitted; `onAskInNew` is omitted too
     (fork is refused server-side for an archived source, and archive is terminal). Separators
     moved to before-each-item-after-the-first, and the toolbar renders nothing when only the
     touch-only Copy would remain on a pointer device.

   Same class, found in the same pass and fixed with it — each one writes to a session that can
   never accept a write, and each is reachable from a stale tab holding pre-archive state:
   - `QueueStrip` (Send now POSTs the flush; Recall appends into the disabled composer)
   - `VaultChatRequests` / `VaultApprovalFloat` approve/deny
   - `ShowPageAnnotateControl` — annotating enqueues an annotation *message* into the session,
     so it is hidden on an archived chat. ~~This narrows item 7's "keep the Show Page toggle":
     the toggle and the Share control stay (the Show Page store already refuses archived
     mutations), the annotate control does not.~~ **Superseded by follow-up 3 — the toggle and
     Share go too.**
   - the Show Page open path's prompt **retry** (`showPagePromptRetryRef`) — the store refuses
     to *create* a page for an archived session, but a session archived after a failed prompt
     stays in the retry set and would re-prompt into a 409.

3. **The Show Page controls go too — item 7's "keep the Show Page toggle" was wrong.**
   The round-2 rationale ("the store already refuses archived mutations") is the
   clickable-but-erroring pattern, not a fix: server-side refusal is a backstop, not a
   substitute for withdrawing a dead action. And *viewing* does not work either —
   `archive_session` forces every existing page to `visibility="offline"`
   (`storage/workbench_sessions_service.py`) and `ensure_active` refuses to create a missing
   one (`409 session_archived`, `core/show_pages.py`). So Visualize ends in a 409 toast when no
   page exists, or frames an offline page when one does; Share's popover re-`ensure`s on open
   and every one of its mutations (`update_visibility` / `set_share_id` / `rotate_share`) hits
   the same guard. All three controls — Visualize, Share, annotate — are now withdrawn via
   `showPageControlActions` in `sessionArchived.ts`. **No read-only page-serving path was
   added**: that would change Show Page archive semantics and is out of scope here.

   The stale-tab shape of the same defect: a tab already *in* Show Page mode when the archived
   409 converges would lose back-to-chat (it is the Visualize button) and sit on a hidden chat
   surface with no iframe — a blank chat. So Show Page mode is **derived**, not stored:
   `isShowPageActive(readOnly, showPageMode)` puts that tab back on the transcript in the same
   render that flips `readOnly` (an effect would paint one blank frame first).

   Found in the same sweep: `Composer`'s `onPasteFiles` was the one media affordance round 2
   left ungated (the picker and mic got `disabled`). A pasted file can never be sent, and
   `POST /api/sessions/<id>/attachments` has no archive guard, so it would persist an orphan
   upload. Gated on `!disabled` at the shared-component layer.

Regression coverage: `ui/src/components/workbench/ChatArchivedReadOnly.test.tsx`.

### Archived-chat affordance sweep (round 3)

Three rounds of the same defect class warranted an enumeration instead of more eyeballing. Every
interactive affordance reachable from an archived chat — `ChatPage.tsx` and every component it
renders — classified as **safe** (pure read / local UI state / machine-scoped pref), **gated**
(already withdrawn), or **fixed** (this round).

| Affordance | Site | Class | Why |
| --- | --- | --- | --- |
| Visualize / back-to-chat toggle | `ChatHeaderBar` | **fixed** | page forced offline + `ensure_active` 409 |
| `ShowPageShareControl` (visibility, share id, rotate, copy, pin-to-Dock) | `ChatHeaderBar` | **fixed** | popover re-`ensure`s → 409; all mutations refused |
| Framed Show Page (stale tab already in it) | `ChatPage` iframe | **fixed** | derived `isShowPageActive` returns to the transcript |
| `MentionEditor` paste-to-upload | `Composer` | **fixed** | orphan attachment; upload route has no archive guard |
| `ShowPageAnnotateControl` | `ChatHeaderBar` | gated (r2) | annotation is a session message |
| Composer text / Send / Enter / attach `+` / mic | `Composer` | gated (r2) | `disabled` ⇒ `canSubmit` false; editor non-editable |
| Chat-wide file drag-and-drop | `useFileDrop` | gated (r2) | `disabled: readOnly` no-ops every handler |
| Sidebar "reference this session" → `insertSessionReference` | `composerTarget` | gated (r2) | target is `null` when read-only |
| Quick replies | `MessageRow`→`QuickReplies` | gated (r2) | `readOnly` reuses the answered lock |
| Selection Quote / Ask-in-new | `SelectionQuoteToolbar` | gated (r2) | both handlers omitted; bar renders nothing on pointer devices |
| `QueueStrip` Send-now / Recall / Remove | `ChatPage` | gated (r2) | strip not rendered |
| Vault approve/deny (cards + float) | `VaultChatRequests`/`Float` | gated (r2) | not rendered |
| Title click-to-edit | `TitleField` | gated (r2) | static `<span>` |
| `AgentRoutePicker` | `ChatHeaderBar` | gated (r2) | static text + Archived badge |
| Show Page prompt retry (`showPagePromptRetryRef`) | `toggleShowPage` | gated (r2) | `!readOnly` backstop; toggle no longer rendered |
| Back button, `ForkSourceBanner` link, harness trigger links, activity-row navigation, "Manage in Harness" | header / transcript / `ActivityStrip` | safe | navigation only |
| Scroll: load-older, jump-to-latest, deep-link jump, anchor restore | `Transcript` | safe | reads `GET /messages` |
| Activity chip expand / retry-detail / activity-card jump | `AgentActivityGroup` | safe | reads `GET /activity`; retry is a re-read |
| Tool-row eye toggle | `ActivityCard`/`Chip` | safe | machine-scoped `config.ui.show_tool_calls` |
| Harness-row expand, `QueueRow` expand, image lightbox, `FileCard`/`FileViewer` | transcript | safe | local UI state / media reads |
| Debounced draft save + unmount flush | `onDraftChange` | safe | composer inert ⇒ never armed; and `PUT /draft` already drops an archived save with `{ok: true}` |
| Inbox mark-read | unread effect | safe | read-cursor write, accepted; archived rows aren't counted anyway |
| `Composer` Stop + busy placeholder (busy branch) | `Composer` | **fixed (r4)** | ~~safe — `readOnly && busy` is unreachable~~ **Wrong: it is reachable on a fresh load during the async-cancel window.** See round 4 below. |
| `SecretRequestCard` (`$<NAME>` in an agent reply) | `Markdown` | **fixed (r5)** | ~~safe — writes a machine-scoped vault secret, not the session, and the server accepts it~~ **Wrong, and wrong on the wrong axis.** Whether the write lands was never the question: archive **expired the session's provision requests**, so an enabled Provide button asserts that an agent is waiting for this secret when none is. The defect is the affordance claiming a live request. Now **locked, not hidden** — see round 5. |

Out of scope of "reachable from an archived chat" as ChatPage renders it: the sidebar / AppShell
session actions (rename, archive, fork), which are not ChatPage's children. Their archived
handling is pre-existing and untouched by this PR.

### Round 4

#### 4a. The Stop row above was wrong — `readOnly && busy` is reachable

The round-3 "safe" verdict rested on two true-but-insufficient facts (archive resets
`agent_status` to `idle`; the 409 convergence path clears `working` before flipping `readOnly`).
Both describe a session that is *already loaded*. Neither covers a **fresh load inside the
async-cancel window**, which is exactly the path this PR opens up — a search hit on an archived
session:

- `archive_session`'s own docstring: cancelling an in-flight chat turn "can't live here … so the
  DELETE endpoint does that (best-effort) after this commits". The status flip is durable
  immediately; the controller turn keeps running for as long as that internal-socket call takes
  (or forever, if the controller is unreachable — it is best-effort).
- `ChatPage` bootstraps the Stop state from `bootstrap.turn_state.foreground === 'running'`, i.e.
  the **controller's** view, not the row's `agent_status`. The `agent_status` reset is therefore
  irrelevant to the load path.

So a chat opened in that window renders `readOnly && busy`. `disabled={readOnly}` alone does not
make that composer inert: the busy branch renders an **enabled** Stop button (it never consulted
`disabled`) and `placeholderBusy` overrides `placeholderArchived`, so the user is told to "type to
queue" on a session that can never run another turn.

Fixed at the **shared-component layer**, not the call site: `busy && disabled` is incoherent for
every `Composer` caller, so `Composer` derives `busyControls = busy && !disabled` and uses it for
both placeholders and the busy/idle branch. A disabled composer now always falls back to the
ordinary Send button, which `canSubmit` already renders inert. Patching only ChatPage would have
left the next caller to rediscover the same trap. Coverage: four cases in
`ChatArchivedReadOnly.test.tsx` (two of them fail against the pre-fix component).

#### 4b. The PATCH 409 body must carry a machine-readable code

Item 3 shipped `409 {"error": "session is archived", "code": "session_archived"}`, copying the
message-append routes. That flat shape defeats `handleApiError` (`ApiContext.tsx:1921`), which
prefers `data.error` and only falls back to top-level `data.code`: a **string** `error` is taken as
the code, so `ApiError.code` becomes `"session is archived"`, `errors.session_archived` never
resolves, and a Chinese-locale user sees raw English — an AGENTS.md §6 i18n violation.

The route now returns the repo's structured shape,
`{"ok": false, "error": {"code", "message"}, "code", "message"}` (`_show_page_error_response`,
`_dock_error_response`, `_project_not_found`, the vault/icon handlers), which the same parser reads
correctly while keeping the flat top-level `code`/`message` for CLI/direct consumers. Fixed in the
route, not the parser: the parser's precedence is load-bearing for the many routes whose `error` is
a human string with no code at all (it is what turns those into a readable toast), and the nested
shape is the established convention — the parser is not the outlier, this route was.

`errors.session_archived` stays as-is (design decision 8): its copy is Show-Page-worded, which for a
stale rename is imprecise but true, and the localization regression the finding is about — raw
English under `zh` — is what preserving the code fixes.

Coverage. `test_sessions_patch_on_archived_session_is_409` previously asserted only `body["code"]`,
which is exactly the field the frontend does *not* read — it passed while the UI was broken. Three
layers now:

- that test pins the nested `error` object (and that `error` is never a bare string);
- `test_sessions_patch_archived_conflict_survives_ui_error_parse` applies the parser's own
  three-line precedence rule to the real response body, for a rename *and* an agent re-route;
- `ui/src/context/ApiErrorParse.test.ts` runs the real parser. The selection step was extracted from
  `handleApiError` into an exported `selectApiErrorFields` — behaviour-preserving, including the
  deliberate throw on a non-object body — because the parser lives inside `ApiProvider`'s closure and
  the repo has no DOM test environment to mount it in. The test pins the structured body → correct
  code, the flat body → mangled code (as a negative, so the contract is explicit), and the two
  pre-existing shapes.

Both Python assertions fail against the pre-fix route. The vitest cases cannot: the parser is
unchanged, so they document the contract the route must satisfy rather than re-detecting the bug.

Same latent defect, **not** fixed here: the pre-existing flat 409s on
`POST /api/sessions/<id>/messages` (`ui_server.py:7723`, `:7796`), `_session_fork_error_response`
(`:6231`, five codes) and `_backend_locked_response` (`:6457`). `ChatPage` reads `body.code` from
its own `fetch` for the messages POST, and `sessionArchived.ts:isSessionArchivedConflict` consumes
that raw body, so the archived-send path this PR relies on is unaffected; the fork and
backend-locked paths do route through `handleApiError` and do mangle their codes today. Left as
follow-ups: they are pre-existing, each needs its own regression coverage, and converting them here
would change response bodies for routes this PR does not otherwise touch.

### Round 5

Four findings; two of them are the *same* round-N mistake repeated (a per-verb fix
and a "safe" classification that looked at the wrong axis), so both are answered by
moving the decision up a layer rather than adding another branch.

#### 5a. The archived 409 must converge for EVERY verb — so convergence moved to the API layer

Round 1's convergence was wired into `sendMessage` only. The next verb the reviewer
tried (rename / agent re-route) stored the 409 as error text and left the title
editor and route picker live, re-issuing a permanently rejected PATCH. Patching
`patch()` would have set up round 6 for `forkSession`, then `ensureShowPage`, then
the Share control.

**Enumeration — every call reachable from `ChatPage` (or a component it renders)
that the server can answer `409 session_archived`:**

| Call | Site | Server source | Converges via |
| --- | --- | --- | --- |
| `POST /api/sessions/<id>/messages` (raw `apiFetch`) | `sendMessage` | `ui_server.py` pre-flight + atomic re-check | direct `convergeSessionArchived` call (r1, rewired r5) |
| `api.updateSession` (PATCH) | `patch` — title, agent re-route, model, pin, scope | `SessionArchivedError` → `_session_archived_response` | **API seam (r5)** |
| `api.forkSession` | `askInNewSession` (Quote → Ask in a new session) | `_session_fork_error_response` | ~~**API seam (r5)**~~ **FALSE WHEN WRITTEN — flat body, code destroyed. Fixed r6.** |
| `api.ensureShowPage` | `toggleShowPage` | `core/show_pages.ensure_active` | **API seam (r5)**, re-verified r6 |
| Show Page access / availability / icon mutations (+ the `api.updateSession` rename) | `ShowPageShareControl` and its inventory store | `core/show_pages` guards | **API seam (r5)**, re-verified r6; access mutations were later consolidated by the unified ShowAccess editor |
| Show-event POST (annotation submit) | the annotation overlay **inside the Show Page iframe** | `core/show_session_events.py:165` | out of reach — see below |

**The "converges via API seam" column was an assertion, not a verification** — see
round 6 for the empirical per-row check and the two rows it corrected.

The one row the seam structurally cannot reach: `ShowPageAnnotateControl` /
`useShowPageAnnotation` only `postMessage` into the iframe
(`avibe:annotation:control`); the actual show-event POST is issued by the annotation
overlay running in that **separate document**, with its own fetch and its own React
tree, so no `ApiProvider` of the host page sees its 409. Left as-is rather than bridged:
it is already withdrawn twice over on a read-only session — the annotate control only
renders while the page is framed, and `isShowPageActive` un-frames it the moment
`readOnly` flips — and a host↔iframe error channel is Show Page architecture, not this
PR's scope.

Verified **not** `session_archived`-capable, so deliberately not in the seam:
`api.cancelSession` (`/cancel` has no archive guard), `api.removeQueuedMessage`
(`DELETE /queue/<id>`, no guard), `api.sendQueuedNow` (`internal_client.send_now` →
`SessionTurnManager.send_now`, whose only 409 is `stop_failed`),
`api.setSessionDraft` (drops an archived save with `{ok: true}`, never a 409), the
vault approve/deny routes (machine-scoped), attachment upload, and every GET.

**Centralised, in the API layer.** `handleApiError` is the single funnel every JSON
helper's error passes through, so it announces the fact once via a new
`api.onSessionArchived(handler)` subscription; `ChatPage` subscribes once and applies
`markSessionArchived` + the best-effort authoritative reload in one
`convergeSessionArchived`. Consequences:

- `patch()` needed **no** convergence code of its own — the elegance test for the
  layer choice. Only its *message* is per-verb: `errors.session_archived` (what
  `handleApiError` resolves) is Show-Page-worded and wrong for a rename, so the
  catch substitutes the new `chat.archived.editBlocked`.
- the child-component calls (unified access Apply / availability / icon, and the
  store's own rename) converge without ChatPage plumbing a callback into any of them.
- a future session-scoped write inherits it.

`sendMessage` stays the one explicit caller because it *cannot* use the seam: it
uses a raw `apiFetch` so it can read `queued` / `already_answered` off a non-2xx-aware
response, and never reaches `handleApiError`. It now calls the same converger rather
than keeping its own copy of the reducer + reload.

Which session the announcement is about comes from the request path
(`archivedConflictSessionId(code, path)`, exported from `ApiContext` for testing like
`selectApiErrorFields`). Both `409 session_archived` route families are session-id-first
(`/api/sessions/<id>…`, `/api/show-pages/<session_id>…`). The pattern is deliberately
**unanchored** because `updateSession` passes a *label* (`"PATCH /api/sessions/<id>"`)
rather than a bare path — i.e. exactly the route this round is about.

#### 5b. `SecretRequestCard` — the round-3 "safe" call was wrong

The r3 rationale ("it writes a machine-scoped vault secret, not the session, and the
server accepts it") answered a question nobody asked. Archive **expired the session's
provision requests**; an enabled Provide button therefore tells the reader an agent
is blocked waiting for this secret when nothing is. The affordance is the defect.

**Locked, not hidden** — the same resolution the quick-reply group got, for the same
tension: the card is the transcript record of *what the agent asked for*, so deleting
it would erase a line of the conversation (hiding it degrades the marker to bare text,
losing the "this was a secret request" framing entirely). It renders as the same badge,
`disabled`, with a `title` stating why (`vaults.request.expired`).

Implemented by **splitting the component**, not by an early return inside it:
`SecretRequestCard` now dispatches to `ExpiredSecretRequest` (pure, i18n only) or
`LiveSecretRequest` (the existing `useApi` + provision lookup + `VaultSecretDialog`).
The locked card mounts **no** provide machinery at all — no request fetch, no dialog,
no reachable vault write — rather than a disabled button in front of a live one.
`Markdown` gained a narrow `readOnly` prop (documented as locking only the interactive
markers it can mint; reads — images, file cards, links — stay usable on a read-only
transcript), threaded from `MessageRow`'s existing `readOnly`.

#### 5c. Backend i18n for the archived 409 `message`

Round 4b introduced `message = "session is archived"` in the route — an AGENTS.md §6
violation this PR created. Surveyed the file first: **most** `ui_server.py` JSON error
bodies do hardcode English (`jsonify({"error": str(err)})`), and the only `t()` calls
in the file serve the pre-auth OAuth error *page*. But there is a clear established
pattern for a **localized, machine-coded, user-visible** error, and it is not the
majority one:

`core/show_session_events.py` — a `*_I18N_KEYS` constant beside the error class, a
`localized_*_error()` factory that resolves `V2Config.load().language` (bare `except`
→ `"en"`), the route serializing `str(exc)`/`exc.code`, and a parametrized
resolution guard in `tests/test_i18n_backend_keys.py`. `vibe/api.py` uses
`backend_t(...)` the same way for its `message` fields, and `ui_server.py:6276`
already reads the configured language for a user-visible string in this very route
family (`title_lang` for the fork title).

So: `SESSION_ARCHIVED_I18N_KEY = "error.sessionArchived"` and
`session_archived_message(lang=None)` live in `core/services/sessions.py` (the service
facade that already re-exports `SessionArchivedError`), and `ui_server.py` gained one
shared `_session_archived_response()` used by both the pre-flight and the exception
catch. `error.sessionArchived` added to both backend bundles.

**Not** converted, and stated as a gap: the pre-existing flat English 409s on
`POST /api/sessions/<id>/messages` (`:7743`, `:7816`). Round 4b already deferred those
for shape reasons; localizing their `error` string without also nesting the code would
mean the mangled `ApiError.code` becomes a *Chinese* sentence, and changing the shape
alters response bodies for a route this PR does not otherwise touch.

#### 5d. Archive must outrank the backend-lock preflight

Same async-cancel window as 4a, one layer out. `archive_session` cannot cancel an
in-flight turn inside its transaction, so the DELETE route commits the archive first
and cancels best-effort afterwards. A stale cross-backend PATCH landing in that window
hit the controller-consulting preflight first and came back `409 backend_locked` — a
**retryable** code masking a **terminal** state, so no client could recognize
`session_archived` and converge (5a's whole mechanism is defeated).

Fixed by short-circuiting archived rows at the **top** of `sessions_update`, before
`derive_backend_for_agent_name` and before the controller is consulted, using the same
`is_session_archived` write-guard the messages POST uses. Non-archived behaviour is
unchanged by construction: `is_session_archived` is "exists AND archived", so the 404s
and the 400 for an empty patch are untouched, the backend-lock ordering for every
live row is exactly as before, and a live PATCH pays one extra indexed read. The
`SessionArchivedError` catch stays as the commit-time race backstop.

#### Round 5 coverage

- `tests/test_ui_server_fastapi.py`
  - `test_sessions_patch_on_archived_session_is_409` — extended to assert the
    `message` **equals the resolved i18n value** (so an inlined literal fails) and
    is not the pre-fix sentence.
  - `test_sessions_patch_archived_message_follows_configured_language` — writes a
    `zh` config and pins the body against the `zh` string, covering config language →
    `vibe/i18n` → response body.
  - `test_sessions_patch_archived_outranks_the_backend_lock_preflight` — archived row
    answers `session_archived` **and** `internal_client.turn_state` is never awaited
    for it, with a live row in the same test as the positive `backend_locked` control.
  - `test_sessions_patch_missing_session_is_still_404` — the short-circuit doesn't
    swallow 404/400.
  - All three fail against the pre-fix route (verified by reverting both edits).
- `tests/test_i18n_backend_keys.py` — `test_session_archived_message_resolves_in_every_language`,
  following the `SHOW_EVENT_ERROR_I18N_KEYS` guard; the existing key-parity and
  no-blank-translation tests cover the new bundle entry.
- `tests/test_core_services_sessions.py` — `test_public_surface_is_stable` extended
  for the two new exports.
- `ui/src/context/ApiErrorParse.test.ts` — `archivedConflictSessionId` across every
  `session_archived`-capable route family, the `updateSession` label form, non-archive
  codes (a `backend_locked` on a session path must announce nothing), query strings,
  escaped and malformed ids, and non-session paths.
- `ui/src/components/workbench/ChatArchivedReadOnly.test.tsx` — `isSessionArchivedError`
  on an `ApiError` vs a plain network `Error`; the shared reducer + a read-only header
  render proving the rejected PATCH cannot be re-issued; distinct `editBlocked` /
  `sendBlocked` copy; the locked secret card (disabled, reason present, no dialog
  mounted); `readOnly` threading `MessageRow` → `Markdown` → card; and the pre-existing
  authorship gate re-pinned so `readOnly` isn't mistaken for what governs it. The
  threading case fails against the pre-fix call site.

**Stated gaps.** There is no DOM test environment in `ui/` (no jsdom/happy-dom, no
testing-library), so the *wiring* is not covered end to end: `handleApiError`'s
subscriber fan-out and ChatPage's `useEffect` subscription have no test. The whole
*decision* was extracted into `archivedConflictSessionId(code, path)` precisely so
everything except the `Set` iteration is pinned — the same mitigation round 4b used
for `selectApiErrorFields`. Also uncovered: the two deferred flat 409s in 5c, and the
`title`-only affordance of the locked secret card (SSR markup is asserted, hover
behaviour is not).

### Round 6

One finding: `POST /api/sessions/<id>/fork` still answered the **flat** coded body, so
`selectApiErrorFields` took its human sentence as the code,
`archivedConflictSessionId` returned null, and a stale tab whose first rejected action
is "Ask in a new session" kept every mutating control live after a *permanent*
refusal. Round 4b had already recorded that exact defect at that exact site — and
deferred it.

#### 6a. Why the round-4b deferral rule failed

4b's criterion was "**nothing in this PR depends on it**". That was true when written
and *stopped* being true one round later without anyone re-reading it: round 5a made
`handleApiError` the single load-bearing funnel for archive convergence, which
promoted every flat coded body reachable through the JSON helpers from "cosmetic
pre-existing wart" to "silently breaks the mechanism this PR added". The rule failed
because it measured dependency **at the moment of deferral** and nothing re-evaluated
it when the architecture moved underneath.

Worse, round 5's enumeration table then listed `api.forkSession` as *"converges via
API seam"* — a claim falsified by a decision recorded in this same document, twelve
lines above it. The table enumerated **call sites**, which is the easy half, and
assumed the **body shape** at the other end, which is the half that actually decides
whether convergence happens.

Two rules replacing it:

1. **Deferral needs an expiry condition, not a snapshot.** "Nothing depends on it
   *today*" is only valid alongside "and here is what would make it depend on it" —
   for these bodies, that trigger was "anything that makes a machine code
   load-bearing in the shared error path".
2. **An enumeration row is a claim about the whole path.** Listing a caller proves
   nothing about the response it parses; the row is unverified until the *body* at the
   far end has been read. Hence 6b, and hence the test that now checks it
   mechanically instead of by eye.

#### 6b. Per-row empirical re-verification of the round-5 enumeration

Every "converges via API seam" row traced to the response builder its route actually
returns (`vibe/ui_server.py`), then to a body assertion. Two rows were wrong:

| Row | Body it really emits | Verdict |
| --- | --- | --- |
| `api.updateSession` | `_session_archived_response` → structured | held (r4b/r5) |
| `api.forkSession` | `_session_fork_error_response` → **flat `{"error": "<sentence>", "code": …}`** | **WRONG — code destroyed. Fixed** |
| `api.ensureShowPage` | `_show_page_error_response` → structured | held |
| Show Page access Apply | `_show_page_error_response` → structured | held; later consolidated into revision-CAS Apply |
| Show Page availability | `_show_page_error_response` → structured | held |
| `api.uploadShowPageIcon` | `_show_page_icon_upload_error` → structured | held |
| store rename (`useShowPages` → `api.updateSession`) | same as `updateSession` | held |
| `api.cancelSession` / `removeQueuedMessage` / `sendQueuedNow` / `setSessionDraft` | no archive guard at all | held (not `session_archived`-capable) |
| Show-event POST (annotation overlay) | flat, but in a **separate document** with its own fetch | held (out of the host seam) |
| `POST /api/sessions/<id>/messages` | flat — ChatPage's raw fetch reads top-level `body.code` | **incomplete claim, see 6c** |

Two side notes from the same trace, neither a defect: the Show Page family answers
`session_archived` with **400**, not 409 (`_show_page_error_response` reserves 409 for
`not_public`/`share_id_taken`) — convergence is keyed on the code, not the status, so
it is unaffected; and `_backend_locked_response` was flat, which 5d's own
archive-outranks-the-lock contract depends on (see 6d).

**How each row was verified**, since "verified" is the word that failed last round:

- the *path* side by reading each `api.*` implementation in `ApiContext.tsx`
  (`:2530-2545`, `:2745`, `:2753`) — all are `/api/sessions/<id>…` or
  `/api/show-pages/<id>…`, so `archivedConflictSessionId` extracts an id from each;
- the *body* side by following the route's `except` clause to its response builder in
  `vibe/ui_server.py` and reading the dict literal it emits — **not** by trusting a
  docstring that claims the shape;
- then mechanically, so it is not a one-time read: an AST scan of `ui_server.py` for
  the anti-shape (`jsonify` with a machine `code` **and** a non-object `error`) which
  enumerated exactly 15 such sites pre-fix, and is now a test (below).

#### 6c. Scope decisions

**Fixed — `_session_fork_error_response` (all six codes) + the route's `LookupError`.**
In scope by dependency, not by choice. All branches, not just `session_archived`: they
share one builder, and fixing one branch of a six-branch mapping is the per-verb
mistake rounds 4a/5a were both about.

**Fixed — `_backend_locked_response`.** By the 4b rule this was a follow-up ("nothing
depends on its code"). That rule is retired, and this one has a stronger reason than
"cheap": round 5d deliberately made `sessions_update` answer the **terminal**
`session_archived` *ahead of* the **retryable** `backend_locked` precisely so a client
could tell them apart — and a client cannot tell them apart while one of the two codes
is being replaced by its own error sentence. So it is a dependency of 5d's contract,
which is this PR's. Cost: one delegation + one table row. No user-visible change (no
`errors.backend_locked` key exists, so the toast still falls back to the server
message); `ApiError.code` becomes correct. Existing assertions read the top-level
`code`, which is preserved (`test_ui_session_stream.py:726/1115/1324`,
`test_ui_server_fastapi.py:410`).

**Fixed — the two flat 409s in `POST /api/sessions/<id>/messages` (5c's stated gap).**
4b's reasoning that these are independent *held for the reachable caller*: ChatPage
uses a raw `apiFetch` and `isSessionArchivedConflict` reads top-level `body.code`,
which the structured shape keeps. But the reasoning was **incomplete** — `ApiContext`
also exposes `api.sendSessionMessage` (`:2792`) on the seam, currently with zero
callers, so the first caller to use it would inherit the mangled code silently. Both
sites now call `_session_archived_response()`, which also **closes 5c's localization
gap** (they were hardcoded English; nesting the code is what makes localizing the
message safe, since the sentence is no longer the code).

**Not fixed, with reasons** — the five remaining flat coded bodies, now an explicit
allowlist in the guard test rather than a memory:

- `POST /api/control` (`restart_in_progress`) — `StatusContext.control()` uses a raw
  `apiFetch` and reads top-level `body?.code` itself (`StatusContext.tsx:92-96`), the
  same pattern that made the messages POST safe for ChatPage.
- the four public Show Page / show-event bodies — served to the **iframe document**,
  which has its own fetch and React tree, so no host `ApiProvider` ever parses them
  (round 5's "out of reach" row, re-confirmed).

#### 6d. Fixed at the shape layer, not per route

Six call sites hand-rolled the identical structured dict
(`_show_page_error_response`, `_dock_error_response`, `_show_page_icon_upload_error`,
`_session_archived_response`, and now fork + backend-lock). Per the reuse ladder, that
is past the third repeat: they all delegate to one **`_coded_error_response(code,
message, status, **extra)`**, whose docstring states *why* the nesting exists (it is
the parser's precedence rule, and the reason the flat shape is a silent code-destroyer
rather than a style nit). The three pre-existing delegations are byte-identical
refactors, covered by the parametrized test below.

**Not changed:** whether fork refuses an archived source (it does, unchanged —
`core/services/session_fork.py:174`), `archive_session`, and
`tests/test_session_archive.py`. This round changes **error body shape only**.

#### 6e. i18n status of the fork messages

**Not localized, and deliberately left that way.** The five `SessionForkError`
messages are English f-strings in `core/services/session_fork.py` (`:171-183`), not
`vibe/i18n` keys. Localizing them belongs in a **separate** change: it needs the 5c
pattern (an `*_I18N_KEYS` constant + factory + a `test_i18n_backend_keys.py` guard) for
five codes that have **no** frontend bundle key either, and it is not what this finding
is about. Preserving the code is what fixes the *user-visible* localization defect
here: with `code == "session_archived"` intact, the Web UI resolves its own
`errors.session_archived` from the active locale and the English `message` degrades to
a CLI/fallback string.

One consequence to note rather than silently absorb: the fork path now resolves
`errors.session_archived`, whose copy is Show-Page-worded ("…so its Show Page can't be
changed") and is *wrong* for a fork attempt. Design decision 8 forbids editing that
shared key, and this round honours that — but the key now has three consumers with
three different verbs, so generalizing its copy (or keying the toast per route) is a
real follow-up, not a hypothetical one. The user-facing impact is muted:
`askInNewSession` shows its own `chat.selection.askFailed` toast, and this path is only
reachable from a tab that has not converged yet.

#### Round 6 coverage

The test that **would** have caught this — the shape of test 4b added for PATCH,
generalized so it cannot be route-specific again:

- `tests/test_ui_server_fastapi.py`
  - `_ui_error_code(body)` promoted to a module-level helper (the parser's three-line
    precedence rule, previously nested inside one test).
  - `test_machine_coded_error_bodies_survive_the_ui_error_parse` — **parametrized over
    a table of the response builders themselves** (12 cases: archived, backend-lock,
    all six fork codes, Show Page ×2, Dock, icon). Each asserts the parser recovers the
    code, `error` is an object, and the flat top-level `code`/`message` survive for the
    CLI. The next coded route is covered by adding one row.
  - `test_no_route_hand_rolls_the_flat_coded_error_body` — the **by-construction** half:
    an AST scan for the anti-shape, with `_FLAT_CODED_BODY_EXEMPTIONS` keyed by
    enclosing function (not line number) and a documented reason per entry. A new route
    that reintroduces the flat coded body fails without anyone remembering this
    contract exists.
  - `test_sessions_fork_on_archived_source_survives_ui_error_parse` — route-level, real
    archived row, real 409, nested code asserted.
  - **9 of these fail against the pre-fix `ui_server.py`** (7 builder cases, the
    structural guard, the fork route test), verified by reverting the file.
- `ui/src/context/ApiErrorParse.test.ts` — `ARCHIVED_CAPABLE_ROUTES`, the round-5
  enumeration as an `it.each` table run through the **real** `selectApiErrorFields` +
  `archivedConflictSessionId`: body in, `session_archived` and `ses_1` out. The
  enumeration is now executable instead of prose.

**Gaps.** The vitest table hardcodes body fixtures rather than importing them from the
Python side (no cross-language fixture pipeline exists), so the Python builder test is
the authority on the real bodies and the vitest table pins the parser contract they
must satisfy — the same split round 4b used. Still no DOM environment in `ui/`, so the
`handleApiError` → subscriber → `ChatPage` wiring remains untested end to end. And the
fork `message` strings stay English (6e).

### Round 7

One finding, labelled P2 and actually an invariant hole: **item 3's archived guard was a
bare read-then-write pre-check.** `update_session`'s `status == "archived"` check sat at the
top of the function, and the UPDATE at the bottom matched on `id` alone (plus the
backend-lock predicate, on the backend-changing path only). That read reserves nothing —
pysqlite opens no transaction for a bare SELECT, so SQLite takes the write lock at the
UPDATE — so an archive committing in between let the statement rename or re-route an
already-archived row. Terminal archive is this PR's hard constraint; the guard the read-only
UI depends on was the one write in the codebase not asserting it.

#### 7a. The pattern was already in the function we edited, and the doctrine is written down

Not a new idea to weigh:

- `update_session`'s **own** `backend_changes` branch re-asserts its predicate inside the
  UPDATE, with a comment saying why ("the guard above is read-then-write, and a turn start /
  native bind can commit in between"). Our archived pre-check was added a few lines above
  that comment.
- `storage/sessions_service.py:725-738` states it as doctrine: *"THIS READ IS A FAST PATH,
  NOT THE GUARD… every UPDATE in this function re-asserts the predicate itself… each
  rowcount-0 path re-reads the status… Proven by HFR-252."*

So the fix follows the in-file precedent rather than inventing one: `status != 'archived'`
on the UPDATE **unconditionally** (not only on the backend-changing path — the
`sessions_service` comment is explicit that *every* UPDATE carries it), and the pre-check
demoted in comment to what it always was, a fast path that spares an already-lost caller
the metadata/scope work.

#### 7b. Rowcount-0 is now ambiguous, and archive wins

Two independent predicates on one statement mean `rowcount == 0` no longer names which one
refused — and that branch already belonged to `SessionBackendLockedError`. It now re-reads
`(status, agent_backend)` and decides: vanished → `LookupError`, archived →
`SessionArchivedError`, still-locked → `SessionBackendLockedError`.

**Archive outranks the lock**, deliberately and for round 5d's reason one layer down:
5d made the *route* answer the terminal `session_archived` ahead of the retryable
`backend_locked` precisely so a client can recognize a permanent refusal and converge (5a's
whole mechanism). A service that answers `backend_locked` for an archived row undoes that
from underneath the route. The `not backend_changes` → `LookupError` fallback is kept
byte-equivalent for the non-archived case.

#### 7c. Audit — every write in `update_session`, and the sibling status-pre-check sites

`update_session` emits exactly **one** write statement (no DELETE). Its helpers write
nothing: `_backend_for_agent_name` and `get_session` are reads, `_load_metadata` /
`_dumps_metadata` / `reconcile_explicit_overrides` are pure. Every metadata change is
composed onto one `values` dict on purpose (documented at the `replaced_settings` block), so
there is no second statement to guard — and the new predicate is unconditional, so a future
branch that adds one inherits it only if it reuses `stmt`; a genuinely new statement would
need its own.

Sibling paths, per site:

| Site | Shape | Verdict |
| --- | --- | --- |
| `ui_server.sessions_update` preflight (`is_session_archived`, 5d) | separate connection, then `update_session` — an even wider window than the in-function one | **closed by 7a**: the service predicate is now the guard, the preflight is the fast path, and the existing `SessionArchivedError` catch is the commit-time backstop it was always documented as |
| `POST /api/sessions/<id>/messages` `_persist_user_row` | `is_session_archived` then `messages_service.append` + `clear_draft` + `touch_session`, inside one `engine.begin()` | **residual, pre-existing, not fixed** — see 7d |
| `PUT /api/sessions/<id>/draft` | drops an archived save with `{ok: true}` | no session-row write; nothing to make atomic |
| `POST /api/sessions/<id>/fork` | `session_fork` refuses an archived **source**; the write creates a *new* row | source row never mutated |
| `core/show_pages` guards | write `show_pages`, not `agent_sessions` | out of the invariant's table; `archive_session` forces them offline in its own transaction |
| `archive_session` | unguarded by design (it *is* the archive) | untouched, byte-identical to master |
| `touch_session` / `set_agent_status` / `reset_running_agent_status` | bare writers, no status pre-check to be stale about | no read-then-write gap; `set_agent_status`'s pre-check is on `agent_status` |
| `backfill_session_title` | `agent_sessions` UPDATE, correctly re-asserting its *own* (title-emptiness) predicate — but no `status` predicate anywhere on the path | **found, not fixed** — see 7d |

#### 7d. Two residual archive gaps, with expiry conditions (round 6a's rule)

Both are pre-existing, neither is reachable through anything this PR added, and both are
recorded with the trigger that would make them in-scope rather than a snapshot judgement:

1. **`_persist_user_row`'s atomic re-check is also read-then-write.** Its comment claims the
   re-check is "ATOMIC with the reservation" — true of the *transaction*, not of the *lock*:
   the SELECT still runs in autocommit, so an archive can commit before the INSERT. Impact is
   bounded and not a session-row mutation: it appends to `messages` and bumps
   `last_active_at`, and the controller has a third `is_session_archived` backstop
   (`core/internal_server.py:298`) before a turn runs. Fixing it means a `status`-aware
   predicate inside `messages_service.append`, which is a different change with its own
   coverage. **Expiry:** the moment anything relies on "no message row can exist after the
   archive timestamp" (e.g. an archived-transcript integrity check, or search treating
   archived transcripts as immutable).
2. **`backfill_session_title` can title an archived row.** It fires from a delayed async task
   after a turn (`modules/agents/base.py:_maybe_backfill_session_title`), i.e. inside the same
   async-cancel window 4a/5d are about, and nothing on the path (`core/session_titles.py`)
   filters archived. It only fills a *blank* title, so it cannot overwrite a user title, but
   "an archived transcript can never be renamed" is exactly the invariant. **Expiry:** any
   change that makes an archived row's title user-visible as immutable, or any second writer
   of that column.

#### Round 7 coverage

`tests/test_sqlite_sessions_store.py`, reusing HFR-252's own harness
(`_commit_competing_bind_after` + the real `_archive_write` payload) rather than a new one —
a stub of the read would prove nothing about the write lock:

- `test_update_session_cannot_rename_a_session_archived_inside_its_window` — a **second real
  connection** commits the archive when the fast-path SELECT completes; asserts
  `SessionArchivedError`, the title unchanged, and the archive's status / anchor / agent_status
  intact. Pre-fix: **DID NOT RAISE** — the rename landed on the archived row.
- `test_update_session_archive_race_outranks_the_backend_lock` — one commit carries both the
  finishing turn's native bind (which fails the lock predicate) and the archive, so both
  predicates fail; asserts `SessionArchivedError`, not `SessionBackendLockedError`. Pre-fix:
  raised `SessionBackendLockedError`.

Both verified red against the pre-fix service (stash the file, run, restore).

**Scenario catalog: `HFR-280`** in `tests/scenarios/harness_failure_recovery/catalog.yaml`.
The round-1 claim that no entry applied was correct for *Workbench search* and wrong for
*this*: `update_session` is the fourth session writer in the HFR-251/253/254 read-then-write
family, and HFR-252 is the archive-terminal half of it. Unlike HFR-252's proofs, HFR-280 is
red-green.

`archive_session` and `tests/test_session_archive.py` are untouched; `git diff master --
storage/workbench_sessions_service.py` is confined to `SessionArchivedError` and
`update_session`.

## Validation (pre-push)

```bash
ruff check storage/messages_service.py storage/workbench_sessions_service.py \
  core/services/sessions.py vibe/ui_server.py
python3 -m pytest tests/test_message_search.py tests/test_core_services_sessions.py \
  tests/test_session_archive.py -q
python3 -m pytest tests/test_ui_server_fastapi.py -q
cd ui && npm run build && npm run test
```
