# Model Hub — UI & Interaction Spec (gateway frames)

Companion to `model-hub.md`. That file is the **behaviour** authority; this one is
the **surface** authority: what each frame shows, which states it can be in, the
exact words it says in both locales, and what it does when the data is ugly.

It exists because a design file answers only *what it looks like*. It does not
answer *why it looks like that*, *how one state becomes another*, or *what happens
at 40 sources instead of 4* — so every implementation lane invents its own answer,
and the answers disagree. Everything below was decided while the frames were being
drawn; this file is where those decisions land instead of evaporating.

## Routing modes revision - 2026-09-06

Empty Route Inheritance (`352486374`, contract_version 10) supersedes empty-disable
semantics across all backends. Absent and valid empty route values inherit; canonical
manual_override is a nonempty object or null and canonical route maps omit empty values.
This is server normalization, never client inventory matching or a health-derived origin.

Owner scope decision `c1d398d5f` restricts unknown-model passthrough and inventory-
independent manual invocation to Hub API-key Sources. Subscriptions keep existing
known-model matching/admission and stale-hop retention. Unmatched subscription-only
defaults render Unconfigured. The route wire fields and approved helper interactions
remain unchanged; copy must not imply expanded subscription support.

The approved `model-hub-routing-modes.md` contract (`2db273891`) and frames `bmi25`
(dark), `ziils` (light), `NuxyR` (hover/focus/touch help), `jCs2A` (manual/restore),
`ztAos` (defaults), `P6Zi8k` (automatic/error/empty), and reusable row `tFl3R` govern
routing surfaces. Existing Source, OAuth, credentials and reconciliation workflows below
remain applicable. This revision replaces all-chain priority with backend defaults.

The UI consumes `AgentChain.manual_override`, `AgentChain.route_origin` and effective
`chain`, plus `AgentSupply.model_supply[].route_origin`. It never matches inventories,
derives origin from array equality, or treats historical manual arrays as known human
authorship. Nonempty plans display Automatic (mint), Manual (manual blue) or
Passthrough (amber); null origin displays Unconfigured (neutral), independently of health.
Use `--route-manual` and matching soft tokens: dark `#8BB4FF`, light `#245BCD`.
Approved route rows are 36px, chips 23px and radius 8px; exact pixels remain in Pencil.

Origin help works on hover and keyboard focus; touch tap opens the same help without
opening the row editor. Escape/outside dismiss it; avoid nested buttons. Retain approved
helper text and complete EN/ZH pairs in design/locales. Recorded model-not-found keeps
Passthrough and uses the retained-turn detail surface below without marking the Source failed.

Inherited dialogs open with Edit route. Manual editing retains add/edit/remove/reorder
and accepts exact API-key upstream ids absent from inventory; subscription targets retain
known-model admission. Restore automatic in the footer, or removing the final manual hop,
changes the draft to null and calls preview; its result may be Passthrough or Unconfigured.
Undo restore reinstates the prior manual draft. Cancel/close never writes. Save sends
DELETE for inherited intent or PUT for nonempty manual hops, consuming the complete canonical
mutation result and exact-plan guards. Failed saves retain draft intent; stale preview
responses cannot replace newer edits; duplicate submissions are disabled.

Lost-response reconciliation compares normalized `manual_override` as well as effective
pairs: equal arrays can represent different saved intent. Preview is read-only with runtime
stopped and never starts the engine. A valid empty value follows the actual inherited plan;
only inherited emptiness offers Configure default routing. Removing the final manual hop
previews that plan through existing Restore/Undo/Save/guard/Done, never a saveable empty
Manual state. Undo restores the previous unsaved draft; the saved nonempty override stays
untouched until confirmed successful Save. Backend changes
cannot leak drafts. Origin, runtime health, request errors and draft state remain separate.

### Latest recorded turn detail

The Recorded Error Detail Closure (`0de3d2f47`, corrected by `9cb9ebb53`) supplies the approved error panel's
data through on-demand GET `/api/models/agents/<backend>/provenance?model=<id>`.
This read is independent of route draft/preview and returns the latest retained exactly
attributed settled turn for both identifiers, regardless of outcome, or null in the
existing `{ok: true, contract_version: 10, provenance}` envelope. It reads
the existing 500-record store only; neither AgentChain nor the pure planner owns history.

Show an error panel only when that latest record has `terminal_error`. A newer served,
canceled or other non-terminal-error result clears the old error; null is absent retained
history, not a fabricated successful request. Use the exact label "Latest recorded turn"
/ "最近已记录回合", the record's timestamp, and historical `source_id` and
`configured_model_id`. Never infer a Source from today's chain; a deleted Source remains
identifiable by its recorded id. The details action opens the same structured record,
not a synthesized string or an independent request history.

Display optional observed `http_status` and `upstream_error_code` only as recorded.
Missing metadata in old records uses generic reason-based copy. Show model-not-found
copy only for observed `upstream_error_code: model_not_found`, not for the broader
`invalid_parameter` reason. Never display or persist arbitrary upstream text through
this panel. Route origin, Source health and fallback semantics remain unchanged.

The read has explicit loading, absent, terminal-error, non-error and retry states.
Closing the dialog or changing backend/model invalidates in-flight responses; failure
keeps a retry affordance without claiming no history. Cancel/Restore/Undo/Save neither
write nor rewrite the retained record. No polling or every-request completeness claim
is introduced.

## 0. Scope, authority, provenance

### 0.1 Frames covered

Design source: `avibe-docs/design.pen`, read through the `pencil` MCP tools only
(never `Read`/`Grep` — the file is encrypted). Section titles carry the node id so
a reviewer can open the exact frame.

| Frame | Node id | Title |
| --- | --- | --- |
| 01 | `pWQfB` | 模型网关 01 — 总览(上游 + 网关) |
| 02 | `Q1dkS` | 模型网关 02 — 路由链编辑 |
| 03 | `qZhJ3` | 模型网关 03 — 来源顺序抽屉 |
| 04 | `XvCC4` | 模型网关 04 — 添加订阅 |
| 04 exhibit | `nOgMQ` | 模型网关 04 — 粘贴回跳 |
| 05 | `GDErR` | 模型网关 05 — 添加 API Key |
| 06 | `wItw4` | 模型网关 06 — 来源详情 · 型号管理 |
| 08 | `Doqav` | 模型网关 08 — 故障实况(网关接管中) |
| 09 | `UVR97` | 模型网关 09 — 直连态首屏(升级后第一屏) |
| 10 | `g7MOA4` | 模型网关 10 — 为单个后端启用网关(动作与后果) |
| 11 | `cyaYh` | 模型网关 11 — 编辑来源 / 移除来源 |
| 12 | `qQvkP` | 模型网关 12 — needs_action 来源卡片 |
| 13 | `Q9q5lF` | 模型网关 13 — 添加订阅厂商菜单 |
| 14 | `IM4c2` | 模型网关 14 — 运行时关闭 |

There is no 07: it was removed during the design pass and the remaining frames
were deliberately **not** renumbered, so that every existing reference to "08"
keeps pointing at the same picture.

**All fourteen frame exports are covered and specified.** Frame 02's drawing remains the
visual authority; §1.2 now registers the interaction facts a drawing cannot carry: the
route-replacement draft, save and guard sequence, complete success-envelope consumption,
lost-response reconciliation, failure copy and keyboard path. G-32 remains in §0.5 only
as the audit row that records that closure.

The page viewport drawn by the full-page frames is 1440×1100 Dark. Frame 04's export root
extends to a 1440×1270 authoring sheet to hold the third exhibit below that viewport;
the paste-back PNG is a crop of that exhibit. Frame 13 is a 720×420 component exhibit.
Light and mobile variants are not drawn yet, so every geometric statement in this file
is a statement about Dark desktop until they are.

The four additions in this registration round were verified from the read-only design
export bundle: `MANIFEST.md` identifies the frame ids, `frames-geometry.html` is the
authority for layer names and CSS-pixel geometry, and the @2x PNGs are visual checks only.
That export provenance does not replace `design.pen` in the authority order below; it is
the measurement source supplied to this lane, and this lane does not edit the design.

**These frames do not draw a navigation path, and none may be inferred from them.**
The frame set was composed to make the model *legible* — the shell around it is
the shell that made the picture readable, not a claim about where these surfaces live
in the shipped app. Read a breadcrumb, a tab position or a sidebar entry off one of
these frames and you will be reading a drawing decision as a routing decision. Where
this file states a location, it is because the behaviour spec states it, and the
statement carries a `[spec]` marker; every other locational reading is out of scope
for the frame and for this file.

**The 「模型」 title and the two tabs are page furniture, not the specification of a
page.** They appear on 01/02/03/06/08/09/10 because a frame with no chrome reads as a
component sheet rather than a screen. What §1.0 fixes about them is their *metrics and
states* — so that whatever page hosts these surfaces renders them consistently — not
that a page with this title and these two tabs exists at this address.

### 0.2 Authority order

1. `model-hub.md` — behaviour, vocabulary, resolution. **§4.3 is the sole
   authority for the routing algorithm.** On any conflict, it wins and this file
   is the defect.
2. `model-hub-implementation.md` §8 — the behaviour acceptance ledger
   (AC-1…AC-31) and the `FC-01…FC-14` final-contract handoff. **Acceptance has one
   home, and it is that ledger.** This file states the properties the surface must
   have, each at the place that owns the fact, and keeps no checklist of its own.
3. `model-hub-contracts/` — the frozen wire shapes the two above are landed as.
4. This file — layout, copy, state reachability, interaction feedback.
5. `design.pen` — the pixels. Where this file and the frame disagree on a number,
   the frame is right and this file must be corrected, *unless* the number is
   marked `[derived]`.

This file **references anchors and never restates spec content**. If you want to
know what a chain is, read §4.3 there; this file only says where it is drawn.

**Verification basis.** The routing behavior and wire contract were frozen at
`2db273891`, its Engine Registration Amendment at `f8d14358a`, and the API-key scope
and registration-synchronization decision at `c1d398d5f`. Approved routing
frames are published by AVIBE Docs design commit `f846e515`; their ids remain those
listed in the routing revision. The retained legacy frame catalog describes unrelated
Source/OAuth/repair surfaces. It cannot restore the retired stored-chain-only policy.
D26/D27 and API/schema parity validate current producer/consumer contracts on one head.

### 0.3 Provenance markers

Every statement below carries one of five markers, because "the design says so" and
"somebody decided so while writing this" are not the same kind of fact and a lane
needs to know which one it is holding:

- `[frame]` — measured off the design file. Changing it is a design change.
- `[derived]` — not drawn; decided here because the frame does not cover the case
  (empty lists, overflow, keyboard paths). Changing it is a spec change to *this*
  file and needs no design pass.
- `[spec]` — owned by `model-hub.md`; repeated here only as a pointer.
- `[contract]` — backed by a named AC / FC item or a frozen wire shape. The anchor
  is part of the marker, so the reader can go read it.
- `[contract-gap]` — **the surface needs a value or a route that does not exist
  yet.** The gap is named at the point of the claim, and the claim is *not* an
  acceptance requirement until the gap closes.

The last marker is the one that earns its keep. Without it, a UI spec asserts a
control as functional and the reader has no way to tell whether it is implementable
today; the honest signal has to live *where the assertion is*, not in a boundary
paragraph six hundred lines away that the implementer will not be reading when they
build that control.

**`[contract]` and `[contract-gap]` are told apart by one question: does the sentence
you land on name the value?** A `[contract]` anchor may be a frozen artefact or an FC
handoff item — §0.2 ranks the handoff *above* the artefacts, so an FC item that states
a shape is authority even while `model-hub-contracts/` still carries the pre-S-1 one,
and several do. What an FC item may not do is stand in for a shape nobody has stated.
Where an authority names a response value but no drawn element consumes it — G-22's
one-response `added_to` placement report is the live example — the wire claim is
`[contract]` and the missing surface is `[contract-gap]` with a registry row. The two
cases look
identical in a citation and differ only in whether the anchor tells you the field name,
which is why the anchor is part of the marker and why a reader must follow it.

Reading a requirement as a gap is also how a gap gets registered that was never one, and
that mistake has exactly one tell: **the requirement's own authority is a total
statement.** G-9 registered the missing `409` on the order save against FC-12's
「row-for-row」 clause without reading the matrix that clause points at, which is
declared exhaustive and simply does not list that route. An exhaustive table that omits a
row is answering, not abstaining. Before registering, check whether the file that is
「missing」 something has already said the something must not exist.

**Where a fact may be written.** A marker says what *kind* of fact a statement is;
this rule says *where* it may live. §1 and the contracts own facts. §0.5's registry,
§0.6's conflict records and §2's decisions own decisions — what was chosen, and why.
A decision may not restate a fact it depends on; it cites the §1 anchor that owns it.
Concretely, outside §1 nothing may originate a rendered string, a control or state
enumeration, a contract identifier, a count, or a claim of the form *every / only /
never / no exception* about a set that §1 enumerates.

This is the general form of the lesson that deleted the acceptance checklist. That
deletion treated the checklist as the problem; it was one instance of the problem. A
fact written twice has two owners and the second one drifts, and the drift is
invisible at the moment of writing — both copies are true when the second is written,
and only one of them gets updated when the fact changes.

A totality claim is the same defect compressed, but only in one of its two forms. A
**descriptive** total — *every failure state keeps 取消*, *the one place a protocol is
rendered is …* — is a written fact about the whole of a set §1 enumerates, so it
falsifies itself the moment §1 gains a member, silently, with no edit anywhere near it.
A **normative** total — *no failure may make a mutation the only way out* — is the
decision itself, and it gets stronger as the set grows, because it binds the next member
somebody draws. §2 is for the second kind. When a decision needs the first kind to be
legible, it cites §1 instead of copying the roster out.

`UI-n` numbering was cancelled together with §3; each property now lives at the fact
it constrains, and acceptance has one home (§0.2 item 2). The numbering is not to be
reintroduced.

### 0.4 Not in scope

Behaviour invariants (persistence, event fan-out, schema constraints, resolver
precedence). Those belong to §8 of the implementation plan. Where drawing the
frames surfaced a *missing* behaviour invariant, it is listed in the PR
description under 「建议移交 AC 账本」 for routing — not written here, and not
written into §8 by this lane.

**Contracted mutations this frame set does not specify.** `api.md`'s route table
carries twenty non-`GET` rows and **nineteen** state-changing routes: `POST
/api/models/migration/scan` is declared 「Read-only.」 in that same table, so it is
a read that takes a body, and counting by HTTP method rather than by declared
semantics would put it on the mutation side of this section. This document reaches
most of the nineteen and states an absence in §0.5 for the ones whose affordance
is missing. The four below are the remainder.

All four belong to a surface outside this frame set, so silence about them here is a
boundary, not an omission. Frame 02's route-chain `PUT` used to be a fifth row while its
interaction contract was unwritten. §1.2 now owns that route and G-32 records its
retirement, so leaving it here would contradict the same accounting this table exists to
make explicit.

| Contracted route | Where it lives instead |
| --- | --- |
| `POST /api/models/migration/scan` | The migration surface. None of these frames offers an import, and a scan with nothing to show it is not a screen. This is also the one row here that is not a mutation — the read-only `POST` counted out above — and it is listed anyway, because the question this table answers is 「where is this route drawn」 and not 「what does it write」 |
| `POST /api/models/migration/apply` | The migration surface, following its own scan |
| `PUT /api/models/agents/opencode/menu` | The OpenCode model-menu dialog opened by frame 01's **Manage models** action. It is the one owner of the explicit open-menu selection; frame 01 renders the saved selection as route rows |
| `POST /api/models/agents/<backend>/probe` | Diagnostics. It answers "would a turn resolve right now", which none of these frames asks — 01 reports the supply it already has, and a probe run from a page that is not asking would report on something the user is not looking at |

### 0.5 Contract-gap registry

Every `[contract-gap]` in this file, in one place, with retired rows retained as an
audit trail. A live `[contract-gap]` statement describes the intended surface, and is
**not** a requirement on the build: where a frame draws an affordance that sits on a gap,
the section that owns that frame says so explicitly rather than quietly requiring it.

The evidence column is re-verified against the contract each time the branch takes a
merge, and names the commit it was verified at rather than the commit it was first
written at — a citation to a stale baseline reads exactly like a citation to a live one.
The register was re-verified at `1993f4fd0`, the current master containing the #1326
contract close-out and the first two UI-spec registration rounds.
A row whose Missing cell is struck through has since been withdrawn or registered; its
final column records that disposition instead of claiming the absence is still live.
Rows still open at that baseline retain their evidence rather than borrowing a nearby
contract as an answer.

**Gap retirement is split by ownership.** A contracted route or payload closes only the
wire half of a row. When the intended action still has no registered UI producer, that
surface half remains live as `[contract-gap]` until a frame registers one; wire
reachability never stands in for an affordance. A row is fully retired only when every
half named in its Missing cell has an owner.

| # | Surface | Missing | Evidence / disposition (contract baseline `1993f4fd0`) |
| --- | --- | --- | --- |
| G-3 | 06 model inventory — **retirement is contracted; its discovered-row affordance remains open** | a way to retire a *discovered* model from the drawn inventory; ~~a place to remember that it was retired~~ | `DELETE /api/models/sources/<source_id>/models/<model_id>` now persists `models[].retired: true` for a discovered row instead of deleting it. `source.schema.json` keeps that row readable, never supplying, and refresh never revives it; §4.5 applies the same exact-hop and last-supplier guards. §1.6 registers that representation as the ordinary row chrome with muted ink and the existing tag component. Frame 06 still draws removal only for manual entries, so no registered control invokes the discovered-row route; only that producer half remains live instead of treating wire reachability as a UI consumer. |
| G-9 | Default membership guard, revised 2026-09-06 | Existing exact-plan fields | Sources PUT and compatibility reorder compare effective plans. Pure reorder needs no guard; removal may require confirmation. Closed by the routing contract. |
| G-10 | 01 shell pill, install in flight — **and 08's 安装并切换**, the other press that promises one — **registered against the runtime install contract** | ~~a server-side install state, and the route that enters it~~ nothing | `POST /api/models/runtime/install` is the idempotent producer. `RuntimeDependency.status.health: installing` survives reload, successful verification settles at `not_started`, and failure settles at `not_installed` with the closed `status.error_key`. §1.0 and §1.9 consume that one machine; any mounted surface observing durable `installing` owns the derived 2s status loop, while a held initiating sequence only decides whether settlement lands at Not started or continues through Starting. Unmount stops the loop; reload owns G-10's first read and restores no intent. |
| G-11 | 09 direct-only home, zero backends — **registered against AgentSupply** | ~~an installation flag per agent backend, and the payload that carries it~~ nothing | Every `GET /api/models/agents` row now carries server-authoritative `cli_present`; the zero-installed state is exactly all rows false. §1.8 derives `installedAgents` once from that field and uses the same set for mode dispatch, rows and the count pill. Source presence is evaluated first so an empty installed set cannot hide retained Sources; only `sources == []` + the empty set enters No backend found. |
| G-12 | 01 upstream card and 06 header, `needs_action` — **registered by frame 12** | ~~the control that replaces a dead credential~~ nothing | §1.11 registers the two repair producers drawn on the source cards: 更换 Key sends the credential-replacement flow to `PUT /api/models/sources/<id>/credential`, and 重新授权 starts `POST /api/models/sources/<id>/reauth`. §1.1 and §1.6 cite that owner instead of pointing at each other. Kept as a registered row so the former absence and its closing frame remain auditable |
| G-13 | Default routing save | Complete default order and guard plan | Sources PUT preserves manual arrays and returns authoritative AgentSupply. Failed saves retain the draft. |
| G-14 | 08 adopt-gateway confirm, `effects.1` — **registered against the mode transaction** | ~~the adoption itself: turning the backend's existing CLI login into that backend's first `native_cli` Source~~ nothing | A qualifying `direct` → `hub` mode `PATCH` now atomically adopts the recognized CLI login as the first singleton `native_cli` Source and returns the updated `AgentSupply`; an absent or unrecognized login or an existing native Source creates and reorders nothing, and repeats create no duplicate. §1.9's consequence pair states both branches and never treats `cli_present` as recognition evidence; its M5 handoff rereads Sources before the committed result lands because the response cannot carry the possibly created Source. |
| G-15 | 06 source detail, a source's own name and Base URL — **registered by frame 11** | ~~any affordance that edits them~~ nothing | §1.10 registers the overflow action, edit dialog and guarded `PATCH /api/models/sources/<id>` producer drawn in frame 11. Kept as a registered row so the former absence and its closing frame remain auditable |
| G-16 | 01 upstream card and 06 source detail — **registered by frame 11** | ~~any affordance that removes a source~~ nothing | §1.10 registers the overflow action and the source-removal guard dialog drawn in frame 11 for `DELETE /api/models/sources/<id>`. The existing 06 model-row 移除 remains a different operation. Kept as a registered row so the former ambiguity and its closing frame remain auditable |
| G-17 | 04 add-subscription, a flow that expects something pasted back — **registered by the 04 paste-back exhibit** | ~~the field that takes it, and the control that sends it~~ nothing | §1.4 registers `nOgMQ`'s paste-back dialog and its `POST /api/models/oauth/submit` producer. The drawn `paste_code` variant supplies the frame geometry; `presentation.expects` selects the registered code or callback-URL copy without changing that geometry. Kept as a registered row so the former absence and its closing exhibit remain auditable |
| G-18 | 05 add-by-key, 拉取型号 and the observation 添加 runs before it saves — **registered against Source observation** | ~~the route that carries a non-persisting observation of a source that does not exist yet~~ nothing | `POST /api/models/sources/observe` accepts `{vendor, base_url?, key, protocol?}` and returns `SourceObservation` without persisting a Source or returning a credential reference. Omission selects the shipped vendor pin when one exists; otherwise only `custom` omission auto-detects and still requires matching response proof. A supplied value restricts observation to one interface and is established when authentication succeeds and either `vendor` has a shipped catalog pin, the client declared that protocol on `custom`, or a matching protocol-shaped response proves it. §1.5 consumes its closed outcome/reachability/authentication/protocol/discovery/models facts. |
| G-19 | 05 add-by-key, 取消 pressed while a persisting add is in flight — **registered against the Source-create commit boundary** | ~~what the server is left holding when the cancel lands after the transient phase~~ nothing | Before durable Source commit, AC-26 cleanup completes before cancellation settles. After commit, cancellation ends only the caller's wait: the Source and placements remain committed and the next Source/Agent read owns the outcome. §1.5 registers that boundary instead of promising a post-commit abort. |
| G-20 | 01 source card and 06 status bar on every Source read — **registered against `Source.adopted_by`** | ~~a *read* that carries `adopted_by`~~ nothing | Every Source returned by `GET /api/models/sources` now carries server-derived, complete, unique `adopted_by` for Hub-mode backends, sorted by backend then menu model. Creation responses echo the same projection at top level. §1.0 owns the grouping/de-duplication rendering and never derives it from chains. |
| G-21 | 01 upstream card, 添加订阅 → 04 — **registered by frame 13** | ~~the step that picks which vendor the subscription is for~~ nothing | §1.12 registers the vendor menu drawn in frame 13. Claude 订阅 passes `anthropic` and ChatGPT 订阅 passes `openai` into §1.4 before that dialog renders its vendor-specific title, options and `POST /api/models/oauth/start` request. Kept as a registered row so the producer/consumer break and its closing frame remain auditable |
| G-22 | 06 for a source just added — the add flow's terminal, reached from §1.4's *Awaiting sign-in* and §1.5's ② | an element that renders `added_to` | **The contract answers a question no frame asks.** `POST /api/models/sources` and the `create` OAuth terminal both return `added_to: AddedTo[]`, an entry naming `backend`, `menu_model`, `source_id`, `model_id` and `position`, the last one-based in the persisted Route chain after commit `[contract]`. Add-time placement (`model-hub.md` §4.2) is what makes it worth showing: the source the user just added has been written into chains they did not open, and this array is the only statement of where. Unlike persisted `Source.adopted_by`, `added_to` has one-response lifetime: later Source and Agent reads carry no placement report, so the landing cannot be recovered by a later read. Nothing in this document claims to render it; §1.3 names this number rather than specifying an element, because §0.2 leaves drawing to the frame |
| G-23 | `Qp6FI`, the shared guarded-change confirm — both callers, §1.6's *Refetch refused* and *Guard refused* | a body block that lists `would_interrupt` | **The refusal carries a list and the dialog draws a sentence.** `would_interrupt` is `SupplyGap[]`, each entry `{backend, model_id, agents}` with `model_id` the protected **menu** model and `agents` the enabled named Vibe Agents that pinned it `[contract]`, and `model-hub.md` requires that 「the confirm copy names affected Agents when any exist」. `Qp6FI` as measured has exactly one label, one count pill, one row list and one hint line, and all four are the `would_remove_hops` side; the only rendering of the gap array is `guard.hint.interrupt`, one sentence that reports the array is non-empty. The strings are specified — `guard.gap.label`, `guard.gap.subject`, `guard.gap.agents`, with `gateway.modelCount` as the pill — because copy is this document's register and an authority requires these; the block that holds them is drawing, so it is a gap rather than an invention. The same absence makes `source_last_supplier` unrenderable: its `api.md` example carries `would_remove_hops: []` beside a populated `would_interrupt`, which this dialog would draw as an empty list under a bare sentence |
| G-24 | 01 run pill, *Unsupported host* — **registered against RuntimeDependency** | ~~a host-platform or installability discriminator in the runtime payload~~ nothing | Every runtime response now carries server-authoritative `host_platform`; an exact match in `manifest.assets[].platform` is the support predicate. §1.0 never substitutes the browser platform. |
| G-25 | 01 gateway group, the unavailable marker — **registered against AgentSupply model supply; collapse ownership retired 2026-08-23** | ~~a per-model fact that separates a chain with a live hop from one whose hops are all stale~~ nothing | `model_supply[].has_runnable_hop` now carries that server-derived fact under the same runnability axiom as AgentChain. §1.1 uses it only to choose the row marker: a nonempty chain with no runnable hop renders `legend.unavailable`; the forced-false `chain_length: 0` subset branches first to the existing `models.launch.route_unconfigured` treatment instead of borrowing paused-supply copy. The six-row prefix owns collapse independently of this field. |
| G-26 | Default membership/manual independence | Shared effective planner | Sources outside defaults may remain in manual routes. Defaults update inherited routes under effective guards. |
| G-27 | 05 add-by-key, the persisting `POST /api/models/sources` — **registered against `source-create.schema.json`** | ~~the request shape that route accepts~~ nothing | The schema is the complete request: required `vendor` and write-only `key`; optional `display_name`, `base_url`, `protocol`, `client_nonce` and `accept_unavailable_inventory` `[contract]`. On `custom` with Auto, `protocol` is omitted and the server still requires matching response proof. A supplied protocol is persisted when observation is authenticated and either the vendor catalog pins it, the client declared it on `custom`, or a matching response proves it. §1.5 sends the consent boolean true only from ⑤, where a repeated observation still has to establish the protocol before a failed inventory may commit. |
| G-28 | `Qp6FI` guarded-change hop rows — **registered against `RouteHopRef.position`** | ~~the hop's position, on the reference the refusal returns~~ nothing | `guard-refusal.schema.json` carries one-based pre-mutation `position` on every `RouteHopRef`. §1.6 and §1.10 render it directly and issue no per-chain lookup. |
| G-29 | 05 add-by-key, ⑦'s lost-response reconciliation — **registered against Source-create nonce totality** | ~~anything the client holds *before* the send that the committed Source can afterwards be recognized by~~ nothing | The client generates `SourceCreate.client_nonce` before send. A list read finds exact `Source.client_nonce` after commit; in-flight and committed retries return distinct `409` conflicts, released/list-miss retries are fresh. A committed retry never replays an old response; it returns `source_nonce_conflict` and the client rereads the list to claim the Source. |
| G-30 | 04 add-subscription, *Start failed* entered by a lost response — **registered against OAuth start nonce totality** | ~~a way to reach a flow whose `flow_id` never arrived~~ nothing | The client generates `client_nonce` before start; the server claims the exact `(client_nonce, vendor, channel)` tuple before provider work. Concurrent retry coalesces to one pending start/result, every resulting flow echoes the nonce, and nonce-bearing cancellation remains bounded by its non-null `expires_at`. §1.4 retries the held tuple without opening a second provider start. |
| G-31 | 01/08 model rows and takeover visuals — **registered against `AgentChain.current`** | ~~the chain's **current** hop, on the one read that is supposed to carry it~~ nothing | `agent-chain.schema.json` now requires `current`, either null or an exact member of `chain`; §1.1 renders the current-source line and takeover predicate from that field rather than the first runnable hop. |
| G-32 | 02 route-chain editor — **registered by §1.2** | ~~the editor's interaction contract: how it sequences a save, what it does with a guarded refusal, what it reconciles against when the response is lost, and what the failure line reads~~ nothing | §1.2 now registers the local draft, non-forced save, exact-plan `409` confirmation, full `{chain, removed_hops, interrupted}` success consumption, M6 projection reconciliation, D-36 lost-response readback, failure copy, keyboard contract and atomic ET/AS transition totality. The shared guard contract carries no `guard_token`: confirmation is the exact echo of both refusal arrays, and a changed plan is presented again before any force commit. R6 and M6 make every response member and every invalidated projection explicit. Kept as a registered row so the former specification absence and its closing section remain auditable |
| G-33 | 04 add-subscription, a flow whose declaration carries a device code | anywhere to display or copy `presentation.device_code` | **The remaining contracted value has nowhere to land.** `oauth-flow.schema.json`'s Form B is `auth_url + device_code + expects none`. This round gives every `presentation.instructions_key` a PD-4 helper-line consumer, including the null/unresolved fallback for Form B, so that former half is closed. The paste-back exhibit accepts and submits Forms A and C, but Form B submits nothing and instead needs a read-only device code the user copies out. The new input cannot render that output, so G-33 remains independently open |
| G-34 | §1.4 / §1.11, E6 materialization failure — **registered against the OAuth terminal-response matrix** | ~~the contracted error-envelope shape that carries any committed, non-reconstructible impact from that failed materialization attempt~~ nothing | The standard error envelope carries exact nonempty `interrupted_pairs` if and only if acquisition-stage Source mutation already produced that report; otherwise the member is absent, never `[]`. R5 renders a present report before the mandatory attempt-scope reread and treats absence only as no response report, never as proof that materialization changed nothing. |

**G-8 is closed by an owner ruling, and its number is not reused.** It asked for the
route that saves an edited reasoning-effort list and the field it saves into. The ruling
of 2026-08-09 deletes the retired custom-model collection route outright — it was the one
route in this family that carried its parent id in the request body while the other six
carry the Source id in the path — and replaces it with three model sub-resources of a
Source. Its path is deliberately not written here: `check_model_hub_authorities` treats
that literal as retired wherever it appears, and a spec that keeps spelling a dead route
in order to say it is dead is how the name comes back.

| Operation | Route | Guarded |
| --- | --- | --- |
| Add a manual entry | `POST /api/models/sources/<source_id>/models` | no |
| Edit any entry's `reasoning_efforts` | `PATCH /api/models/sources/<source_id>/models/<model_id>` | no |
| Delete a manual entry | `DELETE /api/models/sources/<source_id>/models/<model_id>` | yes — the existing `force` + 409 envelope |

All three join §4.5's guarded-change matrix and #1215 lands the contract text; §1.6 is
written to them and no longer carries a gap. Three consequences are worth stating where
a reader will look for them. The first two operations are **unguarded** because they
change neither an entry's identity nor its routes, so nothing cascades — which is why
the add and the tier editor each commit straight through, with no confirm at all. The
delete is the opposite case and keeps the guard: it removes a row other configurations
may still point at, so it goes out unforced, may come back `409`, and shows the user the
consequences before a forced resend. Neither half of that contrast is stated by ellipsis
here, because a 「does not」 whose antecedent is two clauses back is a sentence a reader
can finish either way, and one of the two ways deletes a referenced model with no prompt. The edit route is
keyed by `<model_id>`, so FC-12's 「all-inventory reasoning-list edits」 names the
*capability*, not the request grain: one row's tiers save as one request, and there is
no whole-inventory `PUT` to reconcile. And the delete reuses `Qp6FI` rather than
introducing a second confirm style — the same surface §1.6's 重新拉取 already opens when
a shorter inventory would drop hops, which is why the second caller costs no new frame.
Those two, `POST /api/models/sources/<source_id>/refresh` and
`DELETE /api/models/sources/<source_id>/models/<model_id>`, are the whole set `[contract]`,
because they are the only two requests in this family that persist a change a guard can
refuse. **§1.3's 移出 is not one of them**: the drawer sends nothing until 保存, so a
removal there is a pending edit and there is no refusal for a confirm to render (§1.3's
*Dirty* row, and §1.6's own note that the drawer's 移出 is not a caller).

**One divergence in that area closed on its own, and is recorded rather than deleted.**
FC-03 spelled the model entry's authorship field `origin` while the then-frozen
`source.schema.json` spelled it `provenance`. This file was written to need neither
spelling — §1.6 derives what a row affords from *who authored the entry*, and the 录入
column renders that fact through its own copy keys — and the divergence was handed to the
contract lane rather than settled by a UI spec picking a winner. At `ceace07f` the
schema's `models` items carry `discovered_at`, `display_name`, `id`, `origin`,
`reasoning_efforts`: the contract lane picked `origin`, and nothing in this file has to
change. It is kept here as the record of a handover that worked, not as a live
divergence.

**G-7 is closed by an owner ruling, and its number is not reused.** It asked for a
marker that survives a reload on a model a chain still references but a *successful*
refresh no longer advertises. Three answers were on the table — add a per-model
retained/stale field, keep a shadow copy of the last-seen inventory, or state the
requirement where the contract can already meet it. The ruling took the third: **a model
a chain references that no source supplies any more must be visibly marked on the model
menu, and must never be silently skipped.** The original ruling landed on the retained
hop's live non-runnable reason and the empty-route case. The closed
`model_supply.has_runnable_hop` projection now makes the same rule total at page grain:
false keeps the row in the backend menu and gives it a marker when rendered, including
after the user expands the six-row prefix. A nonempty chain renders its explicit unavailable marker;
the forced-false `chain_length: 0` subset is visibly structural and therefore uses the
existing `models.launch.route_unconfigured` treatment instead. §1.6 states the rule,
while G-25 records the fields that carry the marker.

**What that ruling costs, stated plainly.** The requirement moved to the menu, so the
*source page* loses the story. Frame 06's inventory is the discovered set and stays the
discovered set, which means the dropped model's row simply disappears from it: a user who
is looking at the source it used to come from sees a shorter table and no trace that
anything left. The explanation 「it came from source A」 goes with it. Diagnosing 无来源可供
therefore costs one more surface — open the model's chain, read the retained hop, see
which source it names — instead of being readable where the user already is. That is a
real loss and it is worth writing down, because the cheap version of this note would
record only that a gap was closed without a new field. What was bought is that no user
gets silently unconfigured by a vendor changing its `/models` output; what was sold is the
inventory's memory of its own past.

**G-3 is contract-closed and UI-open, which are separate facts.** #1326 added the
route and durable representation: discovered-model DELETE persists `retired: true`,
refresh never clears the tombstone or lets the row supply, and the exact-hop and
last-supplier guards run before the write `[contract]`. Frame 06 continues to draw
removal only for manual entries, so the discovered-row route has no registered producer
`[contract-gap]` G-3. The contract half no longer blocks a future drawing; it does not
turn an undrawn action into a consumer.

No row of the table above is decided here, and the table is the only place that says
which rows are open. Each open row is routed by what it is missing, not by its number: a
row missing a *behaviour* — somewhere for the system to remember something — goes to the
AC ledger through §0.7; a row missing a *wire shape* — a route, a field, an envelope —
goes to the contract lane, which owns `model-hub-contracts/`. A row can be blocked on
both, and then it is handed to both. This lane owns the visible layer, and inventing a
persistence model to make a drawn control defensible is exactly the kind of quiet scope
grab that produces two disagreeing authorities.

**G-6 is closed by an owner ruling, and its number is not reused.** It asked what
frame 10's 切换到网关 does when the runtime is `not_installed`. The ruling: **neither
refuse nor install silently** — name what has to be installed and roughly how long it
takes, and put a button on it. §1.9 and D-26 are written to that, and the shared shell's
Not-installed row in §1.0 stops deferring to this registry.

**G-1 and G-5 were retired by the frame rebuilds, and their numbers are not reused.**
G-1 was 05 ③'s 仍要添加 needing a durable *saved, explicitly unverified* state; the
rebuilt frame 05 has no 仍要添加 and states identification as a precondition of 添加,
so nothing on the surface requires that state any more (see E-3). G-5 was frame 04's
two-channel 去登录 needing a partial-completion outcome; the rebuilt frame 04 is
single-select and one 去登录 produces exactly one effect, so there is no partial to
define. Both are struck rather than renumbered: a `G-n` that moves is a citation that
silently retargets.

**G-2 and G-4 remain retired, with E-2 superseded in part on 2026-08-26.** G-2 asked
for a saved-protocol edit route and remains closed by subtraction: changing protocol
still requires a new Source. G-4 asked for a badge conditioned on manual provenance;
that marker still does not exist. The new ruling adds an unconditional source identity
label (`provider or host · established protocol`) and a form-level Auto/manual selector.
Neither resurrects the retired edit route or invents provenance.

### 0.6 Conflicts raised by this pass — all five now ruled

Five places where the owner-approved frames and the behaviour authority at `176b41b7`
said different things. All five are closed by owner rulings dated 2026-08-09. Each is
kept on the record rather than deleted, because a resolved conflict is evidence about
how the next one should be handled — and because three of the five moved the *design*,
which is the argument for escalating instead of quietly writing down whichever side
this lane had already drawn.

A conflict is not the same thing as a gap. A gap is contract silence; the five below were
*contradicted* contract — something had to be retracted. Filing a contradiction as a gap
is how a lane talks itself into implementing the side it happened to draw. §0.5 retains
G-3 as the audit example of a former contract silence whose route and durable
representation were later added while its independently missing drawn producer remained
visible.

Four of the five were new at `176b41b7`, and they arrived the same way: the frames were
rebuilt on 2026-08-09 to the owner's rulings, and the ledger grew on 2026-08-09 to the
owner's rulings, and the two passes did not see each other. That is worth saying plainly,
because it is the argument for re-reading the basis rather than trusting a stale
verification — neither side is careless, and neither can be found by reading only one.

**E-1 is closed, and it was the design that moved.** It read: *is the source order
global, or one subset per backend?* The frames drew one product-global order with
native sources held out of it (「全局顺序」, 「跟随全局顺序」, 「全局 #n」, and 03's
「不参与排序」 section); `model-hub.md` §3 at the spec lane's head defines 来源顺序 as
an ordered subset eligible for **one backend** and 「never product-global」, bans a
standalone 优先级 as a global noun, and computes the order by a rule whose first step
*includes* the
native singleton. The owner ruled for the behaviour spec, and frame 03 was rebuilt as a
per-backend editor. What that editor contains is §1.3's to state — and it has changed
once since, because the first rebuild carried a follow-versus-custom ownership state
that S-1 later deleted. That is precisely why this record names the ruling and cites
§1.3 instead of listing controls: a conflict record that re-describes the surface it
moved goes stale on the next move, silently. §1.1, §1.2, §1.3, D-9 and D-10 are written
to the ruled model; the `gateway.globalOrder` / `chain.hop.globalRank` / `order.*` keys
are gone. It is
recorded here rather than deleted because a resolved conflict is evidence about how the
next one should go: the escalation was worth its cost precisely because the answer was
*not* the side this lane had drawn.

**E-2 was amended by the 2026-08-26 owner ruling.** A stored protocol still cannot be
edited and Source still carries no manual/automatic provenance. What changed is the
preflight and read presentation: frame 05 exposes Auto/manual protocol selection before
the first request, while every Source card and detail header show the endpoint identity
and established protocol unconditionally. The label never claims who selected it and has no
edit action. This separates three facts that the older ruling conflated: choosing a
candidate, establishing a protocol owner, and displaying the established protocol.

**E-3 is closed, and the design moved again.** It read: *can a source be saved without
a verifying upstream response?* The frames drew 05 state ③ with a 仍要添加 escape, on
the strength of D-4 (「校验是信息,不是闸门」); AC-27 requires a verifying response
before anything persists, and `source.schema.json` has no state meaning *saved,
explicitly unverified*, so the affordance had no landing place. The owner ruled for the
behaviour spec. Frame 05 was rebuilt: 仍要添加 is gone from every failure state, ③'s
subtitle now reads 「认出接口是「添加」的前置条件 · 先修好凭据再重试」, and the manual
button became 拉取型号 with 「可选 ·「添加」时会自动拉一次」. §1.5 and D-4 are written to
the ruled model, and G-1 is struck. The review's `Add anyway` finding was correct on the
contract; what the review could not decide — which of two owner positions yields — is
exactly what the escalation was for.

**E-4 is closed, and the design moved.** It read: *may the product explain that a
channel does not go through the gateway?* AC-23 requires 「Product copy contains no
『not through Gateway』 explanation」, AC-31 reserves **Direct** for `mode: direct` and
**Native** for a `native_cli` hop and forbids either from naming the other, and the
i18n landing row bans 「mechanism-copy keys」. Frame 04 explained the native option in
exactly that shape three times, and the shared legend — drawn by 01, 02, 03, 04, 05 and
08 — named one key 「原生直连」, which is both reserved nouns fused into one label.

The owner ruled for the behaviour spec, and the vocabulary is now split at the source
rather than merely policed in copy:

- **直连 / Direct is a property of a backend**, and names exactly `mode: direct`. It
  renders as the group subtitle's mode word and nowhere else. It is never an ink.
- **原生 / Native is a property of a hop**, and names a `native_cli` hop. It renders as
  the cyan relation ink, the legend key, and the upstream group and kind labels.
- Neither word may appear in the other's sentence, and no string explains what is *not*
  in the path.

Applied: the legend key is `legend.native` 「原生」 on all six frames that draw it (was
`legend.nativeDirect` 「原生直连」); frame 04's three 不经网关 strings are rewritten to
state what the option *does* — 「Claude Code 直接用这个订阅,凭据只留在本机。」,
「Codex 直接用这个 ChatGPT 账号登录。」, 「额度用完不会自动接管,也看不到用量。」; and the
direct-mode group subtitle is the bare word 「直连」 rather than 「直连 · 正常」. §1.0's
ink table, §1.4's copy table and D-21 are written to the split.

The last of those is not a copy edit but the consequence of C-6: in Direct mode
`supply_status` is `null`, so 「直连 · 正常」 was rendering a status the contract does not
produce. The taxonomy and the payload agree once the words are separated, which is the
usual sign that the split was the real fix and the copy ban was the symptom.

**E-5 is closed by drawing the missing state.** It read: *05 has no state for 「protocol
established, inventory unavailable」.* It was the only one of the five where the contract
asked for **more** surface than the frames drew. AC-27 now says 「『Add anyway』 is
available only after the protocol was established and a different result, such as model
inventory, remains unavailable; that uncertainty is a health fact.」 The rebuilt frame 05
drew neither that nor E-3's version, because the rebuild removed 仍要添加 from every
failure state — its two failure states were authentication failure (③) and unrecognized
interface (④), and both still lack an established protocol owner.

The owner ruled that the two 仍要添加 are different affordances wearing one word, and that
this one is legitimate. Frame 05 now draws **state ⑤** (`d6bFlX`): the interface was
recognised, the second fetch came back without an inventory, and the foot offers
取消 / 仍要添加 / 重试. §1.5 specifies it and §2's D-27 states the property it protects:

> 已保存的来源恒有一个已建立且有归属的协议;凡拿不到这个归属的路径,产物都是「没有添加成功」。

That single sentence is what separates the two 仍要添加 and is why E-3 stays closed while
E-5 opens a button with the same label. E-3's 仍要添加 would have persisted a source whose
*protocol* dimension was never observed, which the property forbids and
`source.schema.json` cannot represent. E-5's persists a source whose protocol **was**
observed and whose *health* dimension is merely unknown — and health-unknown is a state
the contract already has. The two look identical on the button and are opposite in the
schema, which is exactly why the label alone could never settle it.

**All five conflicts are ruled, and three of them moved the design.** E-1 rebuilt frame
03 as a per-backend editor, E-3 removed 仍要添加 from the protocol gate, E-2 deleted the
protocol badge from 06, E-4 split 直连 from 原生 across six frames, and E-5 added state ⑤.
Only one of the five ended with the contract yielding, and it was the one where the
contract asked for a surface rather than forbade one. That ratio is the argument for
escalating: had this lane adjudicated, it would have kept four sections it had already
written and been wrong about all four.

### 0.7 Behaviour invariants surfaced by this pass

Two former handoffs are retained here as closed audit entries because their second-order
requirements are what made a route-only patch insufficient:

- **Discovered-model retirement is durable user intent** `[contract]` AC-37. The DELETE
  writes `models[].retired: true`; refresh preserves the row and tombstone, and the row
  remains readable but never supplies. This closes G-3's route and representation halves
  together. The frame still draws no discovered-row producer, so §1.6 retains that
  independently open surface half rather than inferring an affordance.
- **Runtime installation is observable and singleton** `[contract]` AC-38. The install
  route durably enters `installing`; reload and concurrent repeats return the same state
  and start no second download. Verification settles at `not_started`; failure returns
  to `not_installed` with the closed `error_key`. This closes G-10 for both shell entry
  points without a client-only progress flag.

What used to be the list's second model-inventory item — a model a chain references that
a successful refresh stops advertising — remains independently closed as G-7 on the
retained hop and menu projection. Retirement and upstream disappearance are not the same
fact, and neither is reconstructed from the other.

The list was three items at the previous basis, and all three left it the way a handoff
list is supposed to empty — by being decided elsewhere:

- **Adoption versus installation** — ruled. 切换到网关 against a `not_installed`
  runtime neither refuses nor installs silently: it names what has to be installed and
  roughly how long, and puts a button on it. D-26 and §1.9 carry it; G-6 is struck.
- **A one-press both-channels action** — ruled. Subscription channels are added one at
  a time, and the engine is not owed a combined-action shape, so there is no partial
  completion or rollback to define. Frame 04's radio group and its
  「两条都要也可以,分两次添加。」 are the whole of it; D-25 states it as a decision now
  rather than as a property of how the frame happens to be drawn.
- **Which projection a backend-order surface consumes** — ruled. Order surfaces read
  `AgentSupply.sources`, the backend's stored Source order; source cards read
  `adopted_by`; the effective chain is a third projection owned by §1.2; **none may stand in
  for another**. D-28 carries the rule, the reason, the restatement that the Source order
  is a backend priority and never a runtime capability filter, and the note that S-1
  deleted the field the ruling originally named.

One earlier item left the same way: takeover-count agreement across grains landed as
**AC-30**, which at `ca45aeb6` states takeover is 「a projection of visible configuration
plus live runnability, not a stored sibling state」 and requires a fixture where a chain
with no runnable hop renders 「no takeover badge, connector color, or other takeover
visual semantics」, and this file cites that wording instead of restating it.

### 0.8 State completeness register

This is the one place the document declares its states. Every state and every
branch in §1 has a row here, and §1's frame sections carry no state tables of
their own — they point back to this one. The register is what makes
"is this state finished?" a question a program can answer instead of a question
a reviewer has to answer by hand, which is what `scripts/check_model_hub_ui_states.py`
does with it (§0.10).

**Derived-state predicate rule** `[contract]` `[derived]`. On every authoritative
payload at a state's grain, every derived state re-evaluates its complete entry
predicate. A false predicate retires that state before the new payload is rendered;
the previous state remains latched only when the contract explicitly defines that
latch. F2 is not an exception: a failed read delivered no authoritative payload, so it
keeps the last good projection rather than reclassifying it. A visual state therefore
cannot outlive the evidence that selected it merely because a later runtime action has
not yet updated another field in the same projection.

**Failure treatments.** A failure cell either names one of these five
treatments, or names the state it moves to as `→ State`. The set is closed: a
sixth treatment is a change to this section, not a local invention in a frame.

| # | Treatment | What the user sees |
| --- | --- | --- |
| F1 | Retry in place | The surface stays open. The message is replaced in the slot the result would have used, the primary becomes 重试, and every value typed is kept. A refusal that came back persisted nothing. A request that never came back leaves that unknown, and Retry first runs its registered reconciliation. Resend is legal only when that surface owns terminal evidence for the prior attempt; §1.2's unknown Route attempt never has such evidence and is the explicit read-only exception. |
| F2 | Keep the last good result | A failed read leaves the last successful result rendered, and the status line carries the cause. The action that failed stays enabled. |
| F3 | Guard refusal | The request came back refused because it would break a configured chain. The shared confirm (`Qp6FI`, §1.6) states the consequence; the same request is re-sent with `force`, or abandoned. |
| F4 | Issue and do not await | A cleanup call for something the user has already left behind is owned in the background while the visible surface moves on. The departing surface does not wait and renders no cleanup error. The cleanup owner may still serialize its own work — for OAuth it settles the cancel attempt first and then re-reads the affected Source projection — because a read made before that call settles cannot account for a write the cleanup itself may materialize (D-15). |
| F5 | No request | The state issues nothing, so it cannot fail. A local draft it holds is discarded by 取消. |

**Frame-group conservation checklist** `[derived]`. Registration is additive: an exported
frame may contribute controls and states, but it may not erase behaviour already owned by
the surface around it. Every new frame column MUST check all sixteen rows below. `[x] N/A` is
valid only with the reason in the cell; an empty cell or a missing row is an incomplete
registration, not something a reviewer is expected to infer from prose.

| Check | Required accounting | §1.2 / frame 02 | §1.10 / frame 11 | §1.11 / frame 12 | §1.12 / frame 13 |
| --- | --- | --- | --- | --- | --- |
| C1 — existing capability-gated actions | Preserve every action the containing surface already offers; add the frame delta beside it | [x] Opening 02 preserves the model row, group actions and Source cards behind it; the dialog adds only per-model Route editing | [x] Edit / Remove sit beside the existing capability-gated Reauthorize / Replace key producers | [x] The blocked card covers both `needs_action` and `error`: Hub error keeps Refetch, native unclassified login failure keeps Reauthorize, and the card target / healthy-source overflow producers remain | [x] The existing Add subscription trigger remains the owner; the menu adds only vendor selection |
| C2 — valid local draft | Preserve the exact valid draft across no-op exits and F1/F3; prevent a predictably invalid submit | [x] V5 validates new/changed exact pairs; F1/F3 keep the complete ordered `hops` draft and hold the refusal plan separately | [x] V1/V2 define the normalized display name and Base URL draft; F1/F3 retain it | [x] The channel-selected acknowledgement copy, typed key, flow intent and any paste value remain with their owning state | [x] N/A — a row activation passes one closed vendor value and holds no editable draft |
| C3 — focus return target | Name the mounted, focusable and currently admitted control that receives focus when a transient surface closes. A named target is invalid unless all three predicates hold after the destination projection is installed; a background install preserves the active target only while the same predicates remain true and otherwise invokes the registered fallback | [x] A no-op exit reveals the latest installed authoritative page projection, which is the opening row only until a newer reconciliation payload is installed. Every named target passes FF-1; a modal-closing landing uses PF-1's ordered active-target → exact-model-row → exact-backend-group → destination-page-control candidates because any earlier candidate may unmount, transferred M6 preserves a still-admitted active target, and mounted M6 keeps enabled Done | [x] A no-op close returns to the admitted source overflow trigger. A committed page landing uses PF-1 after installation; an in-modal receiving surface keeps its registered eligible focus | [x] A no-op repair exit returns to the admitted invoking card, vendor-observation action or menu control. A committed page landing uses PF-1 after installation; an in-modal result keeps its registered eligible focus | [x] Escape, outside dismissal and a no-op return from 04 restore the admitted Add subscription trigger; a committed 04 exit hands focus to its named eligible receiving surface |
| C4 — in-flight response owner | A busy state with no cancellation route cannot be dismissed; if cancellation is contracted, name the state that owns its late response | [x] Saving owns the route `PUT`; Cancel, close, Escape and outside dismissal stay disabled until its response is classified | [x] Saving source and Removing source disable Cancel, close, Escape and outside dismissal until the request settles | [x] Pre-flow reauth and credential replacement are locked while busy; after a flow is acquired §1.4 Dismissing owns cancellation and any late answer | [x] N/A — the menu sends no request; selection transfers ownership synchronously to §1.4 |
| C5 — existing visual state | Hold the exact rendered origin; a no-op exit must not manufacture another state | [x] The exact opening AgentChain and invoking row are held; abandoning a reversible draft restores that origin | [x] Edit/remove dialogs hold the exact §1.6 origin | [x] Blocked-card, vendor-observation, source-detail and key-entry origins are held exactly | [x] Closed is the same footer/trigger state that existed before Open |
| C6 — committed-report exit | Once a mutation has committed, every dismissal path is the report's Done-equivalent exit and may neither restore a pre-write origin nor discard held response or D-36 commit evidence. A failed projection member adds read-only Retry for only that subset and MUST NOT remove Done or any equivalent dismissal | [x] Route impact reported retains R6 through AR-M3; successful members remain installed while failed members become stale. Empty-tail and matching-readback commits enter Route committed, reconciling, whose DP-4 exits retain evidence and transfer the active generation | [x] Save/remove impact reports retain their envelopes and every Done exit through M1/M2 read failure; inferred commits retain their exact Source/absence evidence through the same reads | [x] Repair impact retains R3/R4's distinct envelopes through M3/M4 read failure, and Repair unresolved retains R3 through M3; Retry is additive and no path restores the invoking origin | [x] N/A — the menu commits nothing and owns no response report |
| C7 — authoritative field validation | Register every editable field against the authority that normalizes or rejects it; no field may rely on a generic request failure as its validator | [x] V5 consumes exact-pair uniqueness, server eligibility and retirement; API-key targets may be absent from inventory, while subscriptions retain known-model admission and unchanged stale pairs | [x] V1/V2 register every frame-11 field, including the complete Base URL normalizer | [x] V3/V4 register the replacement key and paste-back value; the shared reauth acknowledgement is the contracted literal `true`, not a free-form draft | [x] N/A — vendor rows emit closed enum values and expose no editable field |
| C8 — acknowledgement consequence coverage | For every capability that requires acknowledgement, register every applicable channel, the exact confirmed request value and one complete consequence sentence that is true for that channel before the irreversible boundary | [x] N/A — route replacement has no capability acknowledgement; F3 confirms an exact current server impact plan under the guard-totality table | [x] The existing overflow reauth action transfers its exact Source/channel to §1.11's shared confirmation phase; frame 11 adds no alternate shortcut | [x] Hub and `native_cli` both confirm and send literal `true`, but select separate complete bodies: Hub names only the failure-time cost and safe cancellation; native names the immediate shared-login outage and selected-Source-only recovery | [x] N/A — vendor selection starts create intent and crosses no existing-credential boundary |
| C9 — report-free reconciliation failure | For every committed mutation that skips its report because impact arrays are empty or unavailable after D-36 inference, register the pending/failure states, held write evidence and read-only Retry | [x] Route committed, reconciling holds the returned chain plus empty tails, or the exact matching AgentChain GET readback plus unavailable response tails, while AR-M is pending. An Agents-first failure settles as the exact failed Retry subset while mode-dependent companions remain deferred and non-stale; every other AR-M3 result likewise retains every success and retries only its exact acquired failed subset | [x] M1, or M2 through M0, enters Committed projection stale with the updated Source or committed absence plus exact empty/unavailable disposition held; Retry repeats only its complete-surface read | [x] A non-blocked M3/M4 empty envelope or RR-7 inferred commit with an unavailable response tail enters the same state; a blocked R3 result stays in Repair unresolved instead | [x] N/A — the menu commits nothing and invalidates no projection |
| C10 — mutation attempt scope and commit evidence | For every mutation covered by this frame group, name every projection its attempt may invalidate before the response is observed; both a received success envelope and authoritative D-36 commit evidence MUST pass through the owning M row before visible exit, with response-only members marked unavailable rather than invented | [x] R6 owns every received member; exact ordered D-36 readback enters Route committed, reconciling / M6 with `removed_hops` / `interrupted` unavailable | [x] R1/R2 own received save/delete envelopes; D-36 inferred save/delete commits enter the same M1/M2 reads with response-only impact arrays unavailable | [x] RR-1–RR-10 name repair attempt scope; R3/R4 classify received Source outcome, while RR-7 inferred repair commit enters M3/M4 with its absent response tail explicitly unavailable | [x] N/A — vendor selection commits nothing; create's later mutation remains owned by §1.4/RR-3 rather than the menu |
| C11 — workflow progress evidence | A broad transport or state class may not stand in for a later workflow milestone; register what each returned stage or external gesture proves and the exact next gesture/read it authorizes | [x] The route write has no accepted/intermediate state: only R6 or exact ordered D-36 readback proves commit; a shaped rejection is terminal for that attempt, while a nonmatching read is only an observation and never proves an unanswered PUT terminal or authorizes its resend | [x] Save/delete expose no intermediate accepted state: only an envelope or D-36 subject read proves commit, and both route through C10 | [x] Paste `starting` / `awaiting_action` proves action is still required; `verifying` proves submission acceptance. Opening a vendor link proves no recovery and enters the channel observation phase: Hub Refetch, native acknowledged Reauthorize | [x] Vendor selection transfers one closed value into §1.4 Default; it is not evidence that OAuth acquisition or login began |
| C12 — delayed required presentation | If a required presentation value may be absent now and supplied later, name the bounded read owner that can deliver it without starting another producer or browser context | [x] N/A — route replacement has no delayed server-declared presentation | [x] N/A — frame 11 has no server-declared presentation | [x] PD-5 gives a paste form whose `auth_url` is null to the held flow's 2s status-read owner; the first non-null value invokes PD-2 on the original context | [x] Vendor selection transfers to §1.4; it does not own a flow or delayed presentation value |
| C13 — terminal / absence attempt scope | Every terminal code and authoritative subject absence after a producer attempt MUST account for that attempt's full invalidation scope before visible handoff; scope is selected by the evidence milestone, never by channel alone | [x] The producer has no terminal-flow or subject-absence code; `direct_mode` enters DM-1/DM-2/DM-3 without claiming a target projection, while every proven commit still passes M6 | [x] Every `source_not_found` and inferred deletion enters M0; M2 is M0's deletion-attempt reconciliation | [x] Create E4 owns no pre-existing Source scope and create E6 re-reads `Source[]`. Every acquired-flow reauth terminal failure enters RR-5's full M3 read for Hub or native; a pre-flow reauth acquisition failure retains RR-6–RR-9's selected-Source read for either channel | [x] N/A — the menu has made no producer attempt and owns no terminal code |
| C14 — non-mutating failure exit | Every modal failure state MUST offer an exit that sends no mutation; a committed-state exit preserves write evidence and marks unread dependent projections stale rather than restoring the origin | [x] Terminal rejection may abandon through ET-8a; a mounted unknown attempt is read-only and ET-8b explicitly abandons it, while only a forced Direct handoff transfers unknown evidence to the page session. Neither a nonmatching observation nor a read failure may resend the old attempt. Every exit reveals all successful members already installed; committed exits preserve evidence | [x] Edit/remove failures cancel to their held draft/origin; Committed projection stale has Done-equivalent dismissal in addition to read-only Retry | [x] OAuth/repair failures can dismiss through their registered cleanup or Done-equivalent committed exit; neither path resends repair | [x] The menu's Escape / outside exit sends nothing and restores Add subscription focus |
| C15 — page-form landing evidence | Every response, terminal or absence branch that changes the mounted page form MUST name the authoritative payload or read that selects the destination. Successful authority is consumed monotonically: sibling failure cannot erase it, exact mode evidence cannot be overwritten, and a later failure cannot roll the page back to an older projection | [x] AR is the sole composite-read result owner. It records each member's acquisition eligibility and observation epoch against every causal frontier that can invalidate that member, dispatches each successful AgentSupply for mode/page authority immediately, uses only exact AgentChain GET equality as D-36 Route evidence, and recalculates the applicable member set after either AgentSupply Direct or chain `direct_mode`. A failed Agents prerequisite defers rather than fabricates companion observations. LF is the closed landing read-set owner: exact Direct retains every settled independent Source authority; each exact Hub landing installs AgentSupply immediately, establishes a new Source landing frontier and cannot call Sources current until a successor Source observation satisfies it. DM errors retire an illegal editor but never invent a landing payload; PF-1 is fallback-only after installation | [x] R1/R2 and M0–M2 hand their exact returned or reread Source/absence evidence to the page. Their complete-surface results dispatch returned mode before installing a page form; an error class selects no destination by itself | [x] R3/R4 and M0/M3/M4 hand their exact returned or reread Source evidence to the page. Their complete-surface results dispatch returned mode before installing a page form; flow/error classes select failure handling, never a healthy landing | [x] N/A — the menu changes only its own reversible phase and hands one explicit local vendor selection to §1.4; no response or terminal branch changes the page form |
| C16 — atomic event-transition totality | Every state-changing event MUST register one atomic owner for authority dispatch, cleanup, visible feedback, destination focus and next owner; unlisted transitions are forbidden. ET owns gestures/non-RO single producers, AR owns mode-neutral composite-read member settlement and prerequisite-gated acquisition, RO owns mutation-attempt settlement and exact-chain observation independently from page form, LF/AS own landing read-set/install actions, PF-1/FF-1 own focus eligibility and fallback, and CA alone admits **and dispatches** async controls. One row cannot group events whose outcomes differ. Every prerequisite failure must settle its acquired member while deferring ineligible dependants; every successful member must install before an owner/phase transfer; CA predicates must be a positive exhaustive/mutually-exclusive phase partition; every rendered dismissal must map to exactly one edge; every post-install focus transfer must exhaust PF-1's FF-1-filtered candidates; selector active-candidate and selection must have one owner | [x] AR records `(workflow generation, member, acquisition eligibility, observation epoch, member settlement, required causal-frontier set, decisive page evidence)`: `deferred-by-prerequisite` members are neither pending, failed, stale nor completion obligations, and successful Agents mode alone activates their applicable frontiers. RO separately records `attempt settlement × Route observation × owner × observation legality`: terminal rejection never enters D-36, unknown never emits a recovery PUT, and exact nonmatch cannot settle an attempt. Every successful Agents/Source/AgentChain member installs monotonically before cleanup, dismissal or page transfer. Mounted RO-O delegates its admitted member result to AR-D, while the owner table settles page-session results and names cleanup, destination, feedback, focus and next work. LF closes each returned mode's companion read set and every Hub post-Source success invokes the same RO hook. RL closes every started read with generation, cancellation/disown owner and late-result disposition; AP cites every consumed wire member's route/schema provenance. CA positively partitions every exact async phase so one row or none, never two, can emit; all rendered dismissal gestures use the registered settlement × owner × busy edge. PF-1 filters active target, exact row, exact group and destination-page control through FF-1 in order. ET-5a/ET-5d make the one active candidate the selected pair from which confirmation derives. Visible feedback cites a registered key or closed copy block | [x] §1.10's state rows consume R1/R2 and M0–M2, clear their modal phase under DP-1/DP-2/DP-4, render the registered F/copy treatment and name the source overflow or receiving page as focus owner | [x] §1.11's state rows consume R3/R4 and RR/M0/M3/M4, settle flow/key transient ownership, retain the registered report/failure copy and name the invoking card or receiving projection as focus owner | [x] Closed/Open and the §1.4 handoff name the explicit vendor event, menu cleanup, visible destination and Add subscription / receiving-surface focus; no server response edge belongs to frame 13 |

The C5 rule is the former held-state conservation rule in checklist form. In a DP-1
reversible phase, 取消, close, Escape, an outside press, or abandoning an F3 refusal
restores the held origin; none manufactures `Ready`. C5 does not apply merely because a
state is idle: after commit, C6/DP-4 owns every exit and the pre-write origin is no longer
a legal destination. Only a successful mutation or a later authoritative read may select
a different projection.

**Producer-envelope consumption register** `[contract]`. A mutation or materialization
state is incomplete unless it cites the row that owns its exact result shape and gives every member one
registered disposition: render it, hold it for a named receiving state, name the other
section that consumes it, or mark it irrelevant with the reason. Empty arrays produce no
report rows, but they are still consumed as the decision to skip that block; an unmentioned
member is never silently discarded. OAuth reauth is deliberately a separate producer from
the guarded Source-mutation family. The frame-02/frame-11/frame-12 producers use this one
register rather than borrowing a similar-looking tail from another route:

| ID | Producer | Exact authority | Envelope members | Member-by-member disposition |
| --- | --- | --- | --- | --- |
| R1 | §1.10 source metadata `PATCH` | `model-hub.md` Source-mutation matrix, `mutation.source_metadata` | `source`, `removed_hops`, `interrupted` | `source` is held as the updated Source projection. `removed_hops` and `interrupted` each render their non-empty block in Source save impact reported; an empty member skips only its own block. M1 owns every invalidated projection before handoff. |
| R2 | §1.10 source `DELETE` | `model-hub.md` Source-mutation matrix, `mutation.source_delete` | `removed_hops`, `interrupted` | Each array renders its non-empty block in Source removal impact reported; two empty arrays skip the report. The absent `source` is intentional because the Source was deleted. M2 owns the complete post-delete projection read on either path. |
| R3 | §1.11 reauth acquisition and OAuth terminal | `api.md` OAuth completion, terminal `intent: reauth` | acquisition `flow`; terminal `flow`, `source`, `recovered`, `interrupted_pairs` | RR-1/RR-2 consume the acquisition `flow`; only a non-terminal flow owns presentation/polling, while a terminal flow is status-read first. §1.4 consumes the terminal `flow`. Hold `source` and classify its complete `state` before reading array cardinality: `needs_action` / `error` → Repair unresolved, rendering any non-empty `interrupted_pairs` there; non-blocked + non-empty pairs → Repair impact reported; non-blocked + empty pairs → M3 handoff. `interrupted_pairs` is evidence to render, never the repair verdict. `recovered` remains past-state evidence and selects no copy. M3 owns the affected projections. |
| R4 | §1.11 credential replacement | `model-hub.md` Source-mutation matrix, `mutation.credential_replace` | `source`, `removed_hops`, `interrupted` | Hold `source`. Render non-empty `removed_hops` and `interrupted` blocks in Repair impact reported; each empty member skips only its block. This producer has no `recovered` or `interrupted_pairs` member: those names belong only to R3's OAuth terminal. M4 owns the affected projections. |
| R5 | §1.4 / §1.11 E6 materialization error | `api.md` OAuth terminal-response matrix, `oauth_terminal.materialization_*` | standard `ok`, `contract_version`, `error`, optional `detail`; conditional `interrupted_pairs`; `flow` absent | `ok: false` and `error` select E6; `contract_version` is validated and not rendered. Optional `detail` selects no copy and is not surfaced because only registered localized keys may render. If `interrupted_pairs` is present it is exact, nonempty historical impact: render every SupplyGap row in OAuth materialization failed and hold the report through RR-5. If absent, render no report; absence is not an empty success tail and proves nothing about Source outcome. RR-5 still reconciles the full attempt scope. |
| R6 | §1.2 Route save or Restore | `model-hub.md` Source-mutation matrix, `mutation.route_replace` / `mutation.route_restore` | `chain`, `removed_hops`, `interrupted` | Hold `chain` as the authoritative post-commit AgentChain and write evidence. Render each non-empty `removed_hops` and `interrupted` block in Route impact reported; an empty member skips only its own block. If both arrays are empty, skip the report but retain both exact empty values. M6 owns every Route-derived projection before handoff. |

**Mutation / authoritative absence → projection invalidation register** `[contract]` `[derived]`. A complete
response or an authoritative D-36 subject read can prove the write; an exact absent read or
`source_not_found` can prove that the Source is gone. Neither makes every dependent projection
current. Received and inferred commit evidence therefore share one rule: before a visible
success exit, it MUST pass through the owning M row; every authoritative absence passes M0
before visible handoff. A
D-36 inference has no response envelope, so response-only impact members are registered as
unavailable — never as empty and never reconstructed from a later projection read.
Each M row owns its exact invalidated projection set. M0–M4 and M6 use the model surface's complete
read: `Source[]` + `AgentSupply[]` (including each backend's stored Source order) + the
Route-chain index. M5 reads only `Source[]`, because its successful mode response already
carries the post-commit `AgentSupply` but cannot carry a native Source the same transaction
may have created. A report remains mounted with its response arrays while its owning read
runs. An inferred commit instead holds the observed subject evidence while the same read
runs. A failed read invokes F2 for the stale projection and never turns either kind of held
commit evidence into an unconfirmed write:

| ID | Authoritative evidence | Invalidated projections | Required reconciliation | Failure disposition |
| --- | --- | --- | --- | --- |
| M0 | Exact Source absence from `GET /api/models/sources` or a source-bound `source_not_found`; M2 supplies the same evidence after deletion | `Source[]`, every backend's Source order in `AgentSupply[]`, and every Route chain | Remove the exact Source locally, drop its overlay, retain the absence evidence and read the complete model surface before declaring dependent projections current. Every §1.6/§1.10/§1.11 caller enters this owner; M2's post-delete read is this same M0 reconciliation and may hand in its already-settled result rather than duplicate it | Enter Committed projection stale with the Source absence and last good dependent projections explicitly stale; reuse the source-removal refresh-failure line. Retry repeats only M0, while DP-4's committed exits remain legal. None resends a mutation, restores the Source, invents impact or questions the absence |
| M1 | R1 source metadata / Base URL, or D-36 reread matching every requested normalized field | `Source[]`; a Base URL inventory change may also change `AgentSupply[]` runnability and Route chains through the guarded cascade | After a non-empty impact report's Done-equivalent exit, before handing off an impact-free received success, **or before closing an inferred commit**, read the complete model surface. Keep R1's exact envelope when received; for inference hold the reread Source and mark `removed_hops` / `interrupted` unavailable | A report stays rendered with `sourceDetail.edit.impact.refreshFail`, read-only Retry and every DP-4 Done-equivalent exit; a report-free failure enters Committed projection stale with the exact evidence held |
| M2 | R2 source deletion, or D-36 reread proving the exact Source absent | `Source[]`, every backend's Source order in `AgentSupply[]`, and every Route chain | After the report's Done-equivalent exit, or immediately for a received two-empty-array / inferred commit, remove the exact Source locally and enter M0. This is M0's deletion-attempt complete-surface read, not a second owner. Keep received arrays when available and otherwise mark both response-only arrays unavailable | A mounted report stays rendered with `sourceDetail.remove.impact.refreshFail`, read-only Retry and every DP-4 Done-equivalent exit; a report-free failure enters Committed projection stale through M0 with the exact evidence held |
| M3 | R3 OAuth reauth terminal, RR-5 acquired-flow terminal failure, or RR-7 reread proving a held blocked Source clear | the returned or reread Source; every affected Agent-supply/order and Route-chain projection; for `native_cli`, every same-backend sibling invalidated when login started | After a received Repair impact / unresolved report takes its Done-equivalent exit, before handing off a received non-blocked empty-impact terminal, **before an RR-5 terminal-failure handoff**, or before rendering RR-7 inferred repair, read the complete model surface. Keep R3's tail when received; RR-5 has no success tail; RR-7 marks it unavailable | A received report stays rendered with `upstream.repair.impact.refreshFail`, read-only Retry and every DP-4 Done-equivalent exit; a report-free failure enters Committed projection stale with the exact evidence held |
| M4 | R4 credential replacement, or RR-7 reread proving a held blocked Source clear | the returned or reread Source plus every Agent-supply and Route-chain projection the credential attempt may have changed | Use the same complete-surface timing as M3. Retain R4's standard mutation envelope when received; for RR-7 hold the reread Source and mark `removed_hops` / `interrupted` unavailable | A received report stays rendered with `upstream.repair.impact.refreshFail`, read-only Retry and every DP-4 Done-equivalent exit; a report-free failure enters Committed projection stale with the exact evidence held |
| M5 | Any authoritative Hub landing carrying exact `AgentSupply.mode: hub`, including §1.9's successful `direct` → `hub` mode `PATCH` and D-36's held-origin Agents read proving that switch committed | `Source[]`; a qualifying mode transition may have created the backend's singleton native Source, while the returned or reread `AgentSupply` is already authoritative for mode/backend supply | Dispatch the payload through LF-H without retaining its producer identity. Hold the exact `AgentSupply` as write evidence where a mode mutation is proven and otherwise as landing evidence; acquire one landing-owned `GET /api/models/sources` epoch before declaring the Hub landing current. A successful read hands both current projections to 01, never reconstructs a Source from AgentSupply, then invokes LF-H's producer-independent post-Source hook | Keep the exact AgentSupply installed, mark only the last-good Source list stale and render `upstream.unread` with read-only `upstream.retry`; Retry repeats only M5's Source read. Ordinary navigation remains legal. The failure neither resends/questions a proven mode switch nor settles/questions RO's independent Route outcome; its later success still invokes the same post-Source hook |
| M6 | R6 route replacement, or an authoritative exact ordered AgentChain from `GET /api/models/agents/<backend>/chain?model=<id>` matching submitted `hops` under D-36 | `Source[]` including `adopted_by`, complete `AgentSupply[]` including mode/model supply and any present routes projection, and the Route-chain index; the attempt may change membership, mappings, order, runnability, current-hop presentation and page form | R6 receipt or AR-D6's matching exact-chain observation establishes a Route-commit frontier and starts AR-M. The returned/read chain is held as the Route member's self-satisfying CF-R evidence, but its applicability is deferred until mode is known. At entry Agents is `acquired`; Source and Route applicability are `deferred-by-prerequisite`, not pending. An Agents failure settles as the exact failed subset while those dependants remain excluded from completion/stale accounting. An explicit Agents-only Retry succeeds into an exact mode; only then do applicable companions become `ready`. Source acquires one frontier-satisfying generation; Hub activates the already-held commit chain as acquired/successful without another GET, while Direct makes Route not applicable. A Hub landing adds CF-H before Source acquisition. Earlier Source/Agents successes remain installed as latest-known authority but cannot satisfy a later frontier. Keep R6's entire envelope when received; inference marks `removed_hops` / `interrupted` unavailable. AP records `AgentSupply.routes` as optional and deliberately unconsumed here | AR-M consumes each successful acquired member monotonically and closes only when every activated applicable member settles at or after its required frontiers. Deferred members cannot block settlement or render stale. Either exact AgentSupply Direct or chain `direct_mode` takes the same decisive edge: retain every successful independent member, disown only pending/not-applicable Route work, and acquire an authoritative Agents landing read whose exact returned mode alone selects the page form. CA admits Retry only after settlement, for exactly the acquired failed subset, and never overlaps generations. Mounted completion retains report/evidence and Done; transferred completion preserves any FF-1-valid active target and otherwise uses PF-1. ET-20 alone acknowledges/closes/transfers; no path resends or questions the route write |

**Repair-reconciliation totality** `[contract]` `[derived]`. This is the one machine for
create OAuth, reauth and credential replacement. A Source snapshot and a mutation result
are different evidence: the former may prove that a held blocker cleared, but a Source
that was already healthy cannot prove that an elective mutation committed. Attempt scope
is also evidence: a response lost after a native reauth producer was accepted may still
have invalidated every sibling native Source and its downstream projections. The two new
columns make both facts mandatory before impact-array emptiness can select an exit. Each
row is a fixture/checklist cell; a consuming implementation is incomplete if any
applicable cell has no transition.

| Fixture | Producer / evidence now held | Held origin | Intent | Producer-attempt invalidation scope | Returned Source outcome | Required disposition |
| --- | --- | --- | --- | --- | --- | --- |
| RR-1 | OAuth acquisition returns `flow.state` `starting`, `awaiting_action` or `verifying` | none for create; exact Source projection for reauth | create / reauth | Create has no pre-existing Source to invalidate. Hub reauth holds the selected Source scope. An accepted `native_cli` reauth has already invalidated every same-backend native Source plus the dependent Agent/order/chain projections | None yet: acquisition carries `flow`, not a repair tail. Flow state cannot assert that the Source is healthy or that paste input was accepted | PD-1 transfers the click-owned browser context to the flow; PD-2 navigates it whenever the active flow first carries non-null `auth_url` and keeps the visible fallback. Select the server-declared presentation form, then E3a/E3b owns progress: a paste form at `starting` / `awaiting_action` waits for the value, while `verifying` owns completion polling. The acquired flow owns cancellation and the registered invalidation scope until a later read settles it |
| RR-2 | OAuth acquisition returns any terminal flow | same as RR-1 | create / reauth | Same attempt scope as RR-1; terminal acquisition does not erase it | None yet: the terminal `flow` is not the materialized Source tail | Do **not** open or reconstruct a presentation form. PD-3 closes the unused context and immediately reads `GET /api/models/oauth/status/<flow_id>`; only that route materializes and returns the terminal tail. E2 remains inconclusive under §1.4's evidence-class matrix |
| RR-3 | Status or submit returns terminal success with the create tail | no Source origin | create | The newly returned Source and its placement projections; no held pre-create Source exists | The returned Source becomes 06's subject. It is not classified by repair outcome rules | §1.4 consumes every member, closes into 06 for the returned `source`, and carries its placement arrays |
| RR-4 | Status or submit returns terminal success with the repair tail | exact Source projection | reauth | M3's complete model surface; for native this includes every same-backend native sibling already named by RR-1 | Three exhaustive outcomes: `needs_action` / `error` → Repair unresolved regardless of pair count; non-blocked + non-empty `interrupted_pairs` → Repair impact reported; non-blocked + empty pairs → M3 handoff | R3 consumes every member. Hold returned `source`; render every non-empty `interrupted_pairs` block in the selected result. `recovered` remains past-state evidence only. Empty pairs never claim that repair succeeded |
| RR-5 | Status/submit returns E6's `discovery_failed` / `migration_item_conflict`; or an acquired reauth flow returns terminal `failed` / `cancelled`, E8 `flow_not_found`, or E9 `flow_expired` | none / exact Source projection | create / reauth | Create re-reads `Source[]`. Every acquired-flow reauth terminal failure reads M3's complete model surface **before** handoff, for Hub or native; native includes same-backend siblings. The acquired-flow terminal milestone, not the channel, selects this scope. RR-6–RR-9 pre-flow reauth acquisition failure keeps a selected-Source read for either channel | No successful repair tail exists. For reauth the exact Source is absent, present, or unread; a present reread is projection evidence, not a substitute success envelope. E6 may additionally hold R5's exact nonempty error-envelope `interrupted_pairs`, which remains rendered through the read; absence is no report, not empty success | Stop polling, hold the intent-specific failure and any exact R5 report, then perform the registered read. Absent or E7 → M0 / Source gone. A present settled E6 read keeps OAuth materialization failed and only then permits fresh acquisition; a present non-E6 terminal enters OAuth failed. Unread keeps the originating failure/report mounted and enables **only** read-only Retry of RR-5 before any resend. E2 stays in the bounded poll |
| RR-6 | A pre-flow reauth or key-replacement request fails or has no answer; the reconciliation read cannot find the held Source | exact Source projection | reauth / replace key | A pre-flow reauth acquisition failure reads the selected Source for Hub or native: without an acquired flow there is no evidence that the sibling invalidation boundary was crossed. An uncertain credential replacement reads M4's complete model surface because that synchronous mutation may have committed | Absent | Enter §1.6 Source gone. No retry may recreate or select a lookalike Source |
| RR-7 | The same pre-flow failure; the held origin was `needs_action` or `error`, and the reread Source is no longer blocked | blocked Source projection | reauth / replace key | Same producer-attempt scope and mandatory pre-branch read as RR-6 | Present and non-blocked; compared with the held blocked origin | The cleared blocker is sufficient commit evidence, not a visible-exit shortcut. Enter M3/M4 before rendering the reread Source as repaired; if RR-6 already required the complete surface, that read satisfies the M row, while a reauth Source-only read expands to M3 here. Mark every response-only tail member unavailable and invent no impact rows |
| RR-8 | The same pre-flow failure; the held origin was blocked and the reread Source remains blocked | blocked Source projection | reauth / replace key | Same producer-attempt scope and mandatory pre-branch read as RR-6 | Present and still `needs_action` / `error` | Stay in Repair failed with the reread projection behind it; Retry repeats the held producer and preserves acknowledgement/key |
| RR-9 | The same pre-flow failure; the held origin was not blocked, whatever present Source the reread returns | `active`, `standby`, `cooldown` or another non-blocked projection | reauth / replace key | Same producer-attempt scope and mandatory pre-branch read as RR-6 | Present, but no state can prove an elective mutation committed without its success envelope | A healthy snapshot is **not** mutation evidence. Stay in Repair failed and repeat only on Retry. The current wire exposes no mutation-specific read marker, so the success shortcut is deliberately unavailable for elective repair |
| RR-10 | A flow-owning dialog is dismissed, or a bounded retry releases its current flow | none / exact Source projection | create / reauth | Create owns a Source-list read; reauth owns M3's complete model-surface read, including native siblings. The scope survives cancel failure and ownership handoff | Background-only projection result; it never restores a departed dialog or manufactures a terminal repair verdict | Dismissal closes visually under F4; retry may start its fresh acquisition without waiting. In either case the cleanup owner first settles its authorized cancel attempt, then performs the registered read. The read runs even when cancel fails or ownership has moved |

**Dialog phase × exit matrix** `[derived]` `[contract]`. Reversibility, not whether a
request happens to be pending at this instant, decides dismissal semantics. Every dialog
state in frame 02, frames 11/12 and the shared §1.4 machine names one of these three phases.

| Phase fixture | Registered states | Primary / Done | Cancel | Close / Escape / outside | Evidence and focus disposition |
| --- | --- | --- | --- | --- | --- |
| DP-1 — reversible draft | §1.2 Ready/Dirty/read or save failures and Route save refused; §1.4 Default and no-flow failures; Edit open, Remove confirmation, Reauth confirmation, Key entry, guarded refusals and pre-success F1 states | Starts/retries the named producer or its mandatory reconciliation | Restore the exact held origin, or close a create dialog that has no Source origin. §1.2's unknown-outcome exit instead hands the row to Chain unresolved because its origin is no longer authoritative | Same as Cancel where the frame affords that dismissal | No successful response is held. Preserve valid draft through F1/F3; a no-op return restores the invoking control's focus. An unknown write never presents its pre-write origin as current |
| DP-2 — in-flight, no cancellation route | Saving route, Saving source, Removing source, pre-flow Reauthorizing, Replacing key | Disabled while owned | Disabled | Disabled | The state remains mounted until its response is classified; the response totality matrix owns every success member |
| DP-3 — in-flight OAuth with contracted cancellation | Awaiting sign-in/paste-back/completion, Submitting paste-back and their retryable flow states | Submit/retry follows §1.4 | Close visually into Dismissing | Same as Cancel | RR-10 owns the late cancel and reread sequence; any late submit/poll also re-reads after it settles, and the departed surface never reopens |
| DP-4 — committed evidence | Route committed, reconciling; Route impact reported; Source save impact reported; Source removal impact reported; Repair impact reported; Repair unresolved and Committed projection stale | A report/result runs its registered Done exit. A report-free pending read exposes the same Done command; Committed projection stale keeps read-only Retry beside it | Not rendered | Close, Escape and outside press are always the Done-equivalent committed exit, including while a report-free read is pending or the projection is stale | Never restore the pre-write origin. Preserve the exact success envelope, D-36 chain/Source/absence evidence or authoritative direct absence; a pending exit transfers the owning read to the receiving page, while a stale exit carries the evidence there, marks unread dependent projections stale and leaves its refresh-failure line visible. Retry repeats only the registered M read. No exit questions or repeats the mutation, invents an unavailable member, or treats a read failure as failed commit |

**Editable-field authority register** `[contract]` `[derived]`. C2 owns preservation;
C7 owns validity. Save/submit is disabled for an invalid row, so F1 is never used as a
predictable field validator. The register is exhaustive for editable values added or
consumed by frame 02, frames 11/12 and their shared OAuth form.

| Fixture | Field / owner | Authority consumed | Normalization and valid value | Invalid disposition |
| --- | --- | --- | --- | --- |
| V1 | frame 11 `display_name` | source metadata handler: string, non-empty, at most 64 characters, `contains_credential_material(display_name) == false` | Cap the input at 64 characters and trim. Only a 1–64-character result for which the authority's credential-material detector returns false may be compared and sent | Disable Save when the trimmed value is empty or the credential-material detector matches; keep the draft local and never use F1 as its validator |
| V2 | frame 11 `base_url` | `normalize_model_hub_base_url`, used by the source metadata handler | Empty draft → `null`. Otherwise trim, require a parseable HTTP(S) URL with a hostname, no username/password or fragment, no credential-bearing query key and no credential-shaped material; compare/send the normalizer output, including its lowercase scheme and trailing-path-slash removal | Disable Save; keep the draft local. The field is editable only for an `api_key` / Hub Source with a stored credential |
| V3 | frame 12 replacement `key` | credential-replacement handler: required non-empty string; `force` remains a separate boolean | Trim; a non-empty result enables submit and is held through F1/F3 | Disable submit; never send an empty key |
| V4 | §1.4 paste-back `value` consumed by frame 12 reauth | OAuth submit shape plus the selected `presentation.expects` | Trim; a non-empty code or callback URL enables submit, and the same normalized value is retained through reconciliation | Disable submit; never infer a second format beyond the server-declared enum |
| V5 | frame 02 ordered `hops` draft | `model-hub.md` §4.4/§4.6 server-validated Route invariants plus AgentSupply eligibility and Source inventory evidence | Project every row to the exact `{source_id, model_id}` pair sent on the wire. Pairs are unique. A newly added or changed pair must name an existing server-eligible Source and a canonical nonempty, non-retired model id. API-key targets need not be in inventory; subscriptions retain known-model admission. Identity and explicit cross-model mappings are both legal. An unchanged persisted pair may be retained or reordered even while its live annotation is stale/non-runnable | Exclude invalid pairs from the Add-hop chooser and disable Save while any new/changed draft row is invalid or duplicated. Inventory absence alone cannot invalidate an API-key target. Never delete or rewrite an unchanged stale pair merely because it cannot be offered as a new choice. When a reconciliation refresh invalidates a new/changed row, ET-17e alone enters Invalid after refresh, renders `route.invalidAfterRefresh` on every offending row, keeps Remove enabled and gives Save that visible line as its disabled description |

**F1's last clause used to read 「nothing is persisted」, and a lost answer is not a
refusal** `[derived]`. Several states this treatment lands on are creates that are not
idempotent — §1.5's 添加 persists a source, §1.6's 添加 persists a model — and for a
request that came back refused the old clause was exact: the server answered, and its
answer was no. A request that never came back says nothing about whether the server ran
it; the write may have committed and only the response died. A surface that promises
「nothing is persisted」 there is stating something it cannot check, and 重试 on that
promise is how a user ends up with the same source listed twice. So the promise now covers
what the evidence covers, and 重试 out of a no-answer failure re-reads the collection it
was writing into before it re-sends — a row already sitting there closes as the success it
is, rather than colliding with itself. An unguarded idempotent replacement may owe nothing
extra only when its computed guard plan is empty. §1.3 default membership saves may
have effective impact: retain the draft, read canonical defaults after ambiguous results,
and apply the existing exact-plan confirmation before another write.
§1.2 is deliberately different even though it also replaces an array: the write
has a guarded plan and response-only impact evidence, so D-36 reads the exact Route before
any resend and an inferred commit marks that evidence unavailable. Which side a state
falls on is decided by the producer's identity/guard/response semantics, not by the F1
treatment or the HTTP verb alone.

`—` in the Copy column means the state introduces no key: it rearranges strings
the frame renders anyway. UI-local short keys are written without the `models.hub.`
prefix, as everywhere else in this document. A fully qualified presentation key owned
by the server is the other namespace: consume it verbatim and never add the UI prefix.

**SourceObservation dispatch register** `[contract]`. O1 is the only classifier for
both the explicit `POST /api/models/sources/observe` answer and the repeated server-side
observation performed by `POST /api/models/sources`. The producer does not change what
one terminal observation means. A Source-create success envelope takes the committed
success row; a classified pre-commit answer takes the matching observation row; only a
Source-create transport/no-answer result or a response with no terminal observation
classifier enters ⑦.

| O1 evidence | Explicit observe (`检测`) | Source-create repeated observation |
| --- | --- | --- |
| `authentication_failed`, `unreachable`, `timeout` or `adapter_error` | ③ | ③; no Source committed |
| `ambiguous` | ④ | ④; no Source committed |
| `observed`, protocol non-null, `discovery: failed` | ⑤ | ⑤ when `SourceCreate.accept_unavailable_inventory` is false / omitted; only ⑤ resends it as true `[contract]` |
| accepted producer result: explicit `observed` + `discovery: succeeded` | ①″ Identified — persist nothing | — |
| accepted producer result: a Source-create committed success envelope | — | Close into 06 with the returned Source and placement evidence |
| no terminal observation classifier | The explicit observe failure remains F1 and sends no create | ⑦; commit status is genuinely unconfirmed |

**Held-install status-read owner** `[derived]`. The existing `BackendOAuthPanel`
establishes the product's 2s polling cadence; installation borrows that cadence, not its
OAuth timeout. Whenever a mounted Model Hub surface's last
`GET /api/models/runtime/status` reading is `installing`, that surface reads the same
route every 2s, whether or not it holds a local initiating sequence. There is no
terminal bound:
`installing` is durable server truth rather than a client-owned flow lifetime. A failed
read is F2 — keep the last Installing projection, render the read cause and let the next
2s tick retry. Unmount cancels the read owner. Reload performs G-10's first status read;
if it reads `installing`, it starts this same owner, but restores no local sequence. Any
later sequence advance is therefore keyed only by the held-intent rule below, never by
seeing runtime health in isolation.

**Held runtime-sequence continuation register H1** `[derived]`. The initiating surface
holds exactly one sequence before the first request. Every phase advances only from
evidence for that phase; runtime health never creates, changes or reconstructs an
initiating sequence. A request or read failure keeps the exact sequence in its owning F
state, whose Retry resumes at the first phase that authoritative evidence does not prove
complete. Every RuntimeDependency received during recovery enters this table before any
state exit; held intent never bypasses the evidence column.

| Authoritative phase evidence | held `install_and_start` | held `install_start_switch` | no held sequence |
| --- | --- | --- | --- |
| Status reads `installing`, including after reload | Installing; retain the sequence and keep the 2s read owner | Installing; retain the sequence and keep the 2s read owner | Installing and keep the same 2s read owner; there is no continuation promise |
| Status reads `not_started` | Send runtime start, enter Starting and retain the sequence | Send runtime start, enter Starting and retain the sequence | Closed; do not infer either sequence; the page-level switch is the only activation |
| Status reads `down` | Send runtime start, enter Starting and retain the sequence | Send runtime start, enter Starting and retain the sequence | Closed with failed-start copy; do not infer either sequence; the page-level switch is the only activation |
| Status reads `not_installed` | The owning F state retains the sequence; Retry resumes install | The owning §1.9 Failed state retains the sequence; Retry resumes install | Dispatch `error_key` and manifest support through Install failed / Not installed / Unsupported host |
| Status reads runtime live (`ok` or `degraded`) | Perform the ordinary full live-page dispatch, then release the sequence | Send the mode `PATCH` and enter §1.9 Committing; retain the sequence until M5 accepts the committed AgentSupply evidence, then let M5 own its Source read | Perform the ordinary full live-page dispatch |
| Any owned request or read fails | The owning F state retains this exact sequence; Retry resumes its first unproved phase | The owning F state retains this exact sequence; Retry resumes its first unproved phase | The owning F state has no sequence to reconstruct |

| Frame | State | Entry condition | Failure / pending | Copy keys | Exit |
| --- | --- | --- | --- | --- | --- |
| §1.0 | Loading | Route entered, first payload outstanding | → Closed / Sources unread / Partial | — | What the payload says decides where it lands, not the fact that it arrived. In the order the page reads it: `health` first — `down` or `not_started` → Closed; `installing` → Installing; `not_installed` + non-null `error_key` → Install failed; `not_installed` + null `error_key` → Not installed or Unsupported host by the manifest; `degraded` → Impaired. Only `ok` / `degraded` expose the internal configuration. Under `degraded`, the page's two reads are dispatched beneath the Impaired pill at region grain; one failing → Sources unread or Partial in the region it owns. Then `sources == []` → Empty (no sources), and a payload that trips none of them → Ready |
| §1.0 | Ready | `health` reads `ok`, both page reads answered, and at least one source `[contract]` | F5 | `shell.running` | Any mutation re-renders in place `[derived]` |
| §1.0 | Empty (no sources) | `sources == []` | F5 | `upstream.empty` | 添加订阅 → 13, then a vendor row → 04; 添加 API Key → 05; first source → Ready |
| §1.0 | Not installed | `health: not_installed`, `error_key: null`, and an exact asset exists for server-derived `host_platform` `[contract]` | F5 | `shell.notInstalled`, `install.title` … `install.cancel` | Confirm holds initiating sequence `install_and_start` → Installing |
| §1.0 | Unsupported host | `health: not_installed`, `error_key: null`, and no `manifest.assets[].platform` exactly equals server-derived `host_platform` `[contract]` | F5 | `shell.unsupported` | Not from this page — 直连 (§1.8) is the documented escape hatch |
| §1.0 | Installing | `health: installing` from `POST /api/models/runtime/install`, a later status read or G-10's reload read `[contract]` | F2 — the installing-state read owner keeps the last progress state and read cause across a failed tick; the next 2s tick retries `[derived]` | `install.progress` | The observed state, not intent, owns the read loop. H1 dispatches every later RuntimeDependency: `installing` continues it; `not_started` or `down` starts runtime only for a held sequence; failed `not_installed` enters its F state; `ok` / `degraded` takes the held sequence's live row or ordinary page dispatch. Unmount stops polling; reload reconstructs no sequence |
| §1.0 | Install failed | `health: not_installed` with `error_key: settings.models.install.fail.detail` `[contract]` | F1 lands here | `install.fail.title`, `settings.models.install.fail.detail`, `install.retry` | 重试 sends the idempotent install route → Installing; dismiss leaves the persisted failed projection, and a later successful install clears it before entering Installing |
| §1.0 | Closed | `health` reads `not_started` or `down` `[contract]` | F5 | `shell.notStarted` or `shell.stopped`; `shell.closed.*` | Hide the tabs, Sources, Agent gateway models, route controls, supply lines and configuration dialogs. The off switch sends Start and enters Starting |
| §1.0 | Starting | Start accepted — `POST /api/models/runtime/start` | → Unreachable; H1 retains any initiating sequence | `shell.starting` | H1 owns a live payload before ordinary page dispatch: held `install_and_start` → perform Loading's full live dispatch, then release; held `install_start_switch` → send the mode `PATCH`, enter §1.9 Committing and release only after M5 accepts commit evidence; no held sequence → Loading's ordinary dispatch. No runtime reading may infer either intent |
| §1.0 | Impaired | `health` reads `degraded` `[contract]` | F2 at shell grain, over whatever the page already drew — on a first paint that is nothing, and the region whose read failed carries its own F1 beneath this pill (D-34) | `shell.degraded` | The next payload decides, read the way Loading reads one (D-33): `health` back to `ok` with both page reads answered and at least one source → Ready, `sources == []` → Empty (no sources), a page read still failing → Sources unread or Partial, another `health` value → whichever state that value names |
| §1.0 | Status unread | Status request fails with no retained live snapshot | F2; H1 retains any initiating sequence | `shell.unread`, `shell.closed.unread.*` | Internal configuration remains hidden until a live `ok` / `degraded` snapshot is available. A retained live snapshot may remain visible under F2, but the switch is disabled because the read is not authoritative |
| §1.0 | Partial | Sources load, per-backend supply does not | F1 on a first paint, in the region the group rollups would have filled; F2 on any later read, which keeps the rollups already drawn | `gateway.supply.unread`, `gateway.retry` on a first paint; `—` on a later one, which states nothing new because nothing it was showing changed `[derived]` | 重试 → the supply read runs again and what comes back decides, read against the source list this page is already holding (D-33): a reading with at least one source → Ready, or whichever rollup §1.1 names for it; a reading while `sources == []` → Empty (no sources), which a first-paint retry reaches whenever the list that succeeded beside it was the empty one; another failure → back here |
| §1.0 | Sources unread | The mirror: `GET /api/models/sources` fails while `health` and per-backend supply both answer `[derived]` | F1, in the region the list would have filled | `upstream.unread`, `upstream.retry` | The list decides, not the fact that one arrived: 重试 answers with at least one source → Ready, and with `sources == []` → Empty (no sources); a later payload carrying the list is read the same two ways |
| §1.1 | Ready | Sources + per-backend supply both loaded — the two page-level payloads every group-level element is drawn from. The 当前 lines are members of one third, per-backend chain-collection read, owned by Chain unresolved below, and this state neither waits on it nor fails with it | F5 | `gateway.group.subtitle.direct`, `gateway.group.subtitle.gateway`, `gateway.group.mode.direct`, `gateway.group.mode.gateway`, `gateway.group.status.ok` | Card → 06; 来源顺序 → 03; model row → 02; 切换到网关 → 10; 切到直连 → Leaving the gateway; collapse row → Group expanded |
| §1.1 | Empty | `sources == []` | F5 | `upstream.empty` | 添加订阅 → 13, then a vendor row → 04; 添加 API Key → 05 |
| §1.1 | Loading | First paint | → §1.0 Unreachable / §1.0 Sources unread / §1.0 Partial — the same three §1.0 disperses first paint into, because this row is that same first paint seen from the module | — | The payload decides where it lands, not the fact that it arrived — the same reading §1.0 makes one module up: `sources == []` → Empty; anything else → Ready, whose per-source and per-group rows below are drawn from that same payload |
| §1.1 | Per-source `cooldown` | Source reports cooling `[spec §4.5]` | F5 — a rendered report, not a request | `upstream.state.unavailableRetry` while `retry_at` is still ahead, `upstream.state.unavailableDue` once it has passed `[derived]`, `legend.unavailable` | A later payload reports the source in a different state → that state. `retry_at` is when it becomes worth asking again, not evidence that asking worked, so nothing here promotes the source on a clock — and the two keys are that same fact said in copy: the clock running out changes the sentence and not the state |
| §1.1 | Per-source `needs_action` | The source reports `needs_action` `[spec §4.5]` | F5 | `sourceDetail.status.needsAction.oauthExpired`, `sourceDetail.status.needsAction.balanceExhausted`, `sourceDetail.status.needsAction.credentialRevoked`, `sourceDetail.status.needsAction.accountBanned`, `upstream.repair.reauthorize`, `upstream.repair.replaceKey`, `upstream.repair.topUp`, `upstream.repair.contactVendor`, `upstream.repair.contactProvider` | A later payload reports the source in a different state → that state, whatever it says `[contract]`. The payload carries the source's current status and no history, so on a first load that already reads `needs_action` there is no prior state to go back to; and the recovery has a resulting status of its own that the authority writes — a usable refresh clears the blocker and lands `standby` `[contract]` — so remembering one here could only contradict it. Frame 12 registers the card-level repair: OAuth expiry → Reauthorizing; a revoked API key → Key entry; balance exhaustion or account ban on a known subscription vendor → the §1.4 static top-up or support destination, then Vendor recovery observation; the same two causes on an `api_key` Source → the non-linked service-provider fallback. None borrows another cause's action |
| §1.1 | Per-source `error` | Unclassified failure `[spec §4.5]` | F5 | `sourceDetail.status.error`, `upstream.state.supplyStopped`; Hub `sourceDetail.action.refetch`, native `upstream.repair.reauthorize` | The source leaves `error` → whichever state the payload reports. Frame 12 reuses its blocked-card geometry rather than inventing another card: card → 06; Hub 重新拉取 → §1.6 Refetching; an unclassified `native_cli` login failure 重新授权 → §1.11 Reauth confirmation |
| §1.1 | Group waiting | The enabled-Agent aggregate reads `waiting`: no `named_agents[]` member reads `ok` / `degraded`, and at least one reads `waiting` `[derived]` | F5 — no request of this state's can fail, and no elapsed time resolves it either | `gateway.group.status.waiting` — 供给暂不可用 / Supply unavailable for now | A later payload reports a usable Agent → Ready or Takeover active. Every underlying `retry_at` can pass with the group still waiting, so the exit is the next payload's reading and never the elapsed time. F5 says this state issues nothing, not that waiting is the cure |
| §1.1 | Group interrupted — CLI unavailable | The enabled-Agent aggregate reads `interrupted`, no Agent is usable or waiting, and at least one member is blocked because the native CLI its chain depends on is unreachable **in this process** `[derived]` | F2 — the group keeps its last rendering | `gateway.group.status.interrupted` | The CLI becomes reachable → the next enabled-Agent aggregate. Waiting does not resolve this one, which is why it remains distinct from the self-healing `waiting` umbrella |
| §1.1 | Group interrupted — a source needs action | The enabled-Agent aggregate reads `interrupted`, no Agent is usable or waiting, and at least one member is blocked by a source in `needs_action` or `error` `[derived]` | F2 — the group keeps its last rendering | `gateway.group.status.interrupted` | The source leaves `needs_action` / `error` → whichever aggregate the named Agents then produce. Frame 12 owns the credential repair controls; 06 keeps 重新拉取 for a source whose stored credential still works `[contract]` |
| §1.1 | Group unconfigured | `named_agents` is nonempty and every member either has no `effective_model_id` or reads `route_reason: route_unconfigured` `[derived from contract]` | F5 — a rendered configuration statement, not a request or a Source-health failure | `gateway.group.status.unconfigured` — 未配置型号路由 / No model route configured | Any member gains both an effective model and a configured Route → whichever aggregate the named Agents then produce. Distinct from *No enabled Agent uses this backend*, where there is no Agent member, and from *Group interrupted*, where at least one configured Route exists but cannot currently supply |
| §1.1 | Group interrupted — a hop's source is gone | The enabled-Agent aggregate reads `interrupted`, no Agent is usable or waiting, and at least one member's chain names a source that no longer exists `[derived]` | F2 — the group keeps its last rendering | `gateway.group.status.interrupted` | The payload stops reporting the blocker → whichever aggregate the named Agents then produce. Adding the source again produces a different source and does not re-satisfy the stored hop, so the exit is §1.2's explicit remove/change of that exact stale pair |
| §1.1 | Group interrupted — a hop's model is no longer callable | The enabled-Agent aggregate reads `interrupted`, no Agent is usable or waiting, and the chain reports a model-admission blocker. Missing subscription inventory or explicit retirement may explain that blocker; missing API-key inventory alone cannot `[contract]` | F2 — the group keeps its last rendering | `gateway.group.status.interrupted` | Render only the server's current projection. Subscription refresh may restore known-model admission; refresh never un-retires a row. Retained manual hops remain visible and never silently re-point; §1.2 owns explicit remove/change |
| §1.1 | Backend has no usable source | Every candidate filtered out | F5 | `gateway.supply.none` | Any source becomes eligible; 来源顺序 → 03 |
| §1.1 | Fixed backend has no models | A fixed-menu backend resolves to zero model rows `[derived]` | F5 | `gateway.group.emptyModels` | A model becomes available to that backend |
| §1.1 | OpenCode menu has no selection | OpenCode is in Hub mode and `menu.checked` is empty `[contract]` | F5 | `gateway.group.emptySelection`, `gateway.selectedModelCount_*`, `gateway.manageModels` | **Manage models** opens the OpenCode model-menu dialog; saving at least one eligible identifier returns to Ready with one route row per saved model |
| §1.1 | No enabled Agent uses this backend | `mode` reads `hub` and `named_agents` is empty `[contract]` | F5 — a rendered report, not a request | `gateway.group.subtitle.gateway`, `gateway.group.mode.gateway`, `gateway.group.status.unused` | An enabled Agent begins using this backend; its model, Route and live rollup enter the aggregate and select Ready, unconfigured, waiting, degraded or interrupted |
| §1.1 | Per-model route unconfigured | The page-grain `model_supply` row reads `chain_length: 0`; this structural branch is evaluated before the forced `has_runnable_hop: false` reading `[contract]` | F5 for the page-grain statement; an outstanding or failed chain detail read remains F2 only for its other derived columns | server-owned `models.launch.route_unconfigured`, consumed verbatim | Render the existing route-unconfigured family in the current-text slot; never gold 供给已暂停 / Supply paused. A later page-grain payload with `chain_length > 0` leaves this structural state and is dispatched by `has_runnable_hop` |
| §1.1 | Per-model supply paused | The page-grain `model_supply` row reads `chain_length > 0` and `has_runnable_hop: false` `[contract]` | F5 for the page-grain marker; an outstanding or failed chain-collection member remains F2 only for its other derived columns | `legend.unavailable` in the existing current-text slot | Render 供给已暂停 / Supply paused immediately in `$--gold`; do not wait for the backend chain collection. A later page-grain payload with `has_runnable_hop: true` removes the marker; `chain_length: 0` instead enters Per-model route unconfigured. The collection member may still fill its other derived columns, but a pending or failed detail read cannot erase or replace this page-owned marker |
| §1.1 | Takeover active | A member of `GET /api/models/agents/<backend>/chains` returns non-null `current` different from `chain[0]`, while the head is unavailable for a recoverable quota/cooldown or live connection-backoff reason `[contract]` | F5 | `gateway.group.takenOver`, `gateway.row.currentTakeover`, `gateway.group.status.degraded` | Re-evaluate the complete predicate on every later chain payload. `current` back at the head → Ready. A head that is runnable / no longer recoverably unavailable also retires Takeover immediately under the derived-state predicate rule: if `current` still names the later hop, render the ordinary serving/current-hop projection without violet takeover ink or copy until a later payload changes `current`. A local clock alone changes no payload. This is frame 08 (§1.7) |
| §1.1 | Serving past a blocked head | The serving hop is not the head, and the head is blocked by something waiting does not clear — **stated as the negation of the row above, not as a list of causes.** `runnable = health-permits AND process-available` `[contract]`, so a head is here whenever it is not runnable and its block is not a recoverable quota/cooldown or live connection-backoff reason: the head's source reading `needs_action` or `error`, a source that is `healthy` while the native CLI it needs is unavailable in this process (`reason: native_cli_unavailable` `[contract]`), and a head the chain reports as `source_missing` or `model_unsupported` `[contract]`. Defined by negation because the set of non-self-healing blockers is the contract's to extend, and a row enumerated by cause has to be reopened every time it does | F5 | `gateway.group.status.degraded` | The head becomes runnable again → Ready; the head enters a recoverable quota/cooldown or live connection-backoff while a later hop serves → Takeover active. Both are readings of a later payload and neither is a clock (D-16) — including the user-cleared blocks, which are reported by the same read as the rest |
| §1.1 | Chain unresolved | Row grain, not group. The backend chain collection is outstanding, failed or refused, or omitted this row, while the two page payloads are in hand | F2 read at row grain — the group keeps everything those two payloads drew. A page-grain `chain_length: 0` row keeps `models.launch.route_unconfigured`; otherwise a row whose `has_runnable_hop` is true renders `—` in its three derived columns and a false row keeps `legend.unavailable` in the current-text slot. Every case renders `—` only in the other unresolved columns. The engine is not implicated and nothing on the head changes | `models.launch.route_unconfigured` for the empty Route; `legend.unavailable` only for a nonempty false row | Its collection member answers → Ready, Takeover active, Per-model route unconfigured or Per-model supply paused. What re-issues it is the collapse row (D-35): collapsing and re-expanding the group re-reads the backend collection once, and it is the drawn control this row's repair uses, there being no per-row 重试 on the frame. The two triggers beside it are the page's own — any mutation that re-renders the group (*Ready* above) and the next load — so a row that failed is never waiting on a request nobody will send |
| §1.1 | Group expanded | Collapse row activated | F5 | `gateway.collapse` | Collapse toggled back → Ready |
| §1.1 | Leaving the gateway | 切到直连 pressed on a gateway group `[frame]` D-30 — `PATCH /api/models/agents/<backend>/mode` | F1, in place on the group head | `gateway.switchToDirect`, `gateway.fail.switchToDirect`, `gateway.retry` | Success → the group re-renders in its 直连 form; when it was the last gateway backend the page is decided by the sources that are still there, not by the switch — no source left → 09, at least one source retained → **01**, which is §1.8's own *Retained sources* branch and not this frame; a failure keeps the group on the gateway and puts the line and 重试 on the group head, which is the slot the re-rendered form would have used |
| §1.2 | Loading route | A model row opens 02 with the exact `(backend, menu_model)` held; `GET /api/models/agents/<backend>/chain?model=<id>` owns the dialog body | F1 → Route unread | `route.title`, `route.loading`, `route.cancel` | ET-1–ET-4, ET-8a and ET-18a own opening, response dispatch, focus, retry and the non-mutating exit; an unlisted answer cannot leave this state |
| §1.2 | Route unread `[derived]` | The dialog-owned opening chain read failed while the containing page remains mounted | F1, in place | `route.fail.read`, `route.retry`, `route.cancel` | ET-3/ET-4/ET-8a own entry, Retry and the non-mutating exit; the independent dialog read never rewrites the held page row |
| §1.2 | Ready | The exact AgentChain is held and normalized draft intent equals its opening manual_override; inherited display uses the server effective chain | F5 | `route.title`, `route.section`, `route.addHop`, `route.reorder`, `route.hint`, `route.cancel`, `route.save`; `route.removeHop` / `route.grip` when `hops` is nonempty; `route.empty` only when it is empty; `route.add.none` when no candidate remains; `route.add.source`, `route.add.model`, `route.add.search`, `route.add.confirm` while the Add selector is open, and `route.add.noMatch` only while a typed filter excludes every candidate; `route.sourceMissing` only on a hop whose authoritative chain annotation reads `reason: source_missing`; `route.reorder.grabbed`, `route.reorder.position`, `route.reorder.dropped`, `route.reorder.cancelled`, `route.reorder.sorted`, `route.reorder.unchanged` only for their registered live-region event | ET-5a–ET-7f, ET-8a and ET-9a own every selector, local edit, grab, exit and submission edge |
| §1.2 | Dirty | The nonempty manual draft or inherited null differs from the reversible normalized intent; a final-hop removal is a Restore preview, not empty Manual | F5 — every edit is local | Same normal-frame keys as Ready | ET-5a–ET-7f, ET-8a and ET-9a own every selector, local edit, grab, exit and submission edge |
| §1.2 | Invalid after refresh `[derived]` | ET-17e holds refreshed Hub AgentSupply/Source/chain authorities and at least one new or changed draft pair is now V5-invalid; unchanged persisted stale pairs do not enter this state | F5 — the refresh completed and no mutation is sent | Same normal-frame keys as Dirty plus `route.invalidAfterRefresh` on every offending row | ET-5a–ET-8a and ET-17e own repair, exit and focus. Every offending row stays removable; Save remains disabled with the rendered invalid lines as its description |
| §1.2 | Saving | 保存 or guard confirmation activated — nonempty manual intent sends PUT and inherited intent sends DELETE on `/api/models/agents/<backend>/chain?model=<id>`; the first request is non-forced and a confirmed retry carries `force: true` plus both exact refusal arrays `[contract]` | DP-2: the request owns the dialog and every dismissal path until classification | `route.saving` | ET-9a/ET-13b, ET-10, ET-12, ET-14, ET-15 and ET-18a own the complete response dispatch and destination focus for every answer/no-answer branch |
| §1.2 | Route committed, reconciling `[derived]` | R6 success carries two empty impact arrays, or AR-D6 proves the desired Route from the exact chain endpoint while response-only arrays are unavailable; exact commit evidence is held while the active AR-M generation has an acquired member pending, a ready member awaiting acquisition, or an acquired member whose observation has not satisfied every required causal frontier. Mode-dependent deferred members are not pending obligations | F2 applies only to acquired members that AR-M3 marks stale; an Agents-first failure makes Agents the exact stale/Retry subset while Source/Route remain deferred. The Route write/desired state is already proven | `route.refreshing`, `route.impact.done` | ET-11 or ET-17c owns mutually exclusive entry; AR-M/AR-MD, ET-18aM/ET-18aP, ET-19, AS and ET-20 own member eligibility/epochs/frontiers, Direct handoff, Retry, page installation and the sole user exit |
| §1.2 | Route committed, refreshed `[derived]` | AR-M2 completed a mounted report-free M6 generation with every activated applicable member satisfying all causal frontiers and no member deferred behind an unresolved prerequisite; it installed the exact page behind the still-mounted modal and holds commit evidence until acknowledgement | F5 — no request remains and no impact report exists | `route.impact.title`, `route.impact.done` | ET-20 is the only exit. The page projection is already installed, and modal close invokes PF-1 because Done unmounts |
| §1.2 | Route save refused `[derived]` `[contract]` | A non-forced or forced save returned the current exact refusal plan: nonempty manual PUT guards interruption; Restore/last-hop removal additionally guards effective hop removal | F3 — shared `Qp6FI`; G-23 remains the drawing gap for the detailed SupplyGap block | `guard.title.saveRoute`, `guard.subtitle.saveRoute`, `guard.confirm.saveRoute`, `guard.label`, `guard.count`, `guard.hop.position`, `guard.hint.interrupt`, `guard.gap.label`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `guard.cancel` | ET-12, ET-13a and ET-13b separately own plan replacement, cancellation and confirmation |
| §1.2 | Route save rejected `[derived]` | The server definitively rejected the route write outside the guarded-plan and `direct_mode` branches, so this attempt is `terminal-rejected` and did not commit | F1, in place; no D-36 read is started | `route.fail.save`, `route.retry`, `route.cancel` | ET-14 owns terminal entry. CA-R1 admits one explicit Retry only after ordinary V5 validation; ET-14a creates a **new** workflow generation and ordinary PUT or DELETE according to normalized intent, never a recovery/replay of the rejected attempt. ET-8a owns the non-mutating exit |
| §1.2 | Route save outcome unresolved `[derived]` | The route write initially produced no exact Route authority: either no HTTP/API answer or DM-2's shaped `direct_mode` response. The exact submitted draft and whether this was the non-forced or confirmed stage remain held while attempt settlement is `unknown`; an exact nonmatching chain is installed as the newest Route observation but does not settle that attempt. Direct changes only observation legality | F1; D-36 is read-only for this entire workflow | Mounted: `route.fail.unconfirmed` before/after nonmatch, `route.fail.reconcileRead` after a read or Source failure, `route.retry` only for the admitted read subset, and `route.cancel`. Page-owned Hub failure reuses the row's Chain-unresolved/D-35 read treatment; page-owned Direct makes no false Route claim | ET-15/DM-2, RO/AR-D/ET-16, LF/AS, ET-17 and ET-18a own entry, legality changes and D-36 reads. Every successful read installs before transition. Mounted dismissal explicitly abandons the old attempt through ET-8b and retains installed authority; it never transfers a recovery PUT right. CA-D admits exactly one read Retry; page-owned Retry is D-35 only. Reload is the sole implicit evidence drop and never resends |
| §1.2 | Route impact reported `[derived]` `[contract]` | R6 holds a successful route-replace or route-restore response with non-empty `removed_hops` and/or `interrupted`; the complete returned `chain` remains held while AR-M independently settles every activated member after all of that member's required causal frontiers | F2 only for the exact AR-M3 acquired failed subset; deferred companions are neither stale nor Retryable. The write already succeeded and DP-4 owns every exit | Report block keys always; `route.refreshing` while an acquired member is pending or a successful prerequisite is activating companions; `route.impact.refreshFail` and `route.retry` only in the settled-failure subphase; neither retry key after AR-M2 success | ET-10, AR-M/AR-MD, ET-18aM, ET-19, AS and ET-20 own report entry, acquisition eligibility/member settlement/Direct handoff/Retry/install and the sole user exit; successful authority is installed immediately and never dismisses the report |
| §1.3 | Ready | Drawer opened and the eligible sources resolved | F1, in the region the list would have filled → Sources unread | `order.title`, `order.subtitle`, `order.section.ordered`, `order.section.ordered.note`, `order.section.heldOut` | 取消 / 关闭 / Escape → close, discarding uncommitted moves; 保存顺序 → Saving |
| §1.3 | Sources unread | The eligible-source read came back failed while the page behind the drawer is still rendering a healthy runtime `[derived]` | F1, in place | `order.fail.read`, `order.retry` | 重试 → the read runs again, and what it answers with decides: at least one eligible source → Ready, none → Zero eligible sources, another failure → back here; 取消 / 关闭 / Escape → close, having changed nothing |
| §1.3 | Zero eligible sources | No source is eligible for this backend | F5 | `order.empty.noEligible` | 关闭. A source becomes eligible → Ready. 保存顺序 is disabled |
| §1.3 | Empty order, held-out sources remaining | The ordered section is empty and the held-out section is not | F5 | `order.empty.ordered` | 排进来 → Dirty. 保存顺序 stays enabled — an empty order is a real configuration |
| §1.3 | Dirty (uncommitted moves) | 排进来, 移出, a drag, or a keyboard move | F5 — nothing has been sent, so nothing can fail | `order.action.include`, `order.action.exclude` | 保存顺序 → Saving; 取消 → discard, close |
| §1.3 | Saving | 保存顺序 pressed — send `{order: string[]}` to `PUT /api/models/agents/<backend>/sources`, which stores defaults and preserves manual overrides under effective guards `[contract]` | F1 | `order.save`, `order.fail.save`, `order.retry` | Success → close; failure → keep every move held and 重试 the same save |
| §1.4 | Default | A frame 13 vendor row has supplied the vendor | F5 | `addSub.title` … `addSub.hint.chatgpt` | The recommended option is pre-selected; selecting the other replaces it. 去登录 synchronously preallocates PD-1's blank browser context, then sends `POST /api/models/oauth/start`; RR-1/RR-2 classify any accepted flow **before** presentation: non-terminal transfers the context to PD-2, then `presentation.expects: none` → Awaiting sign-in, a paste presentation in E3a `starting` / `awaiting_action` → Awaiting paste-back, and a paste presentation already in E3b `verifying` → Awaiting paste-back completion; each navigates when `presentation.auth_url` is non-null now or on a later flow read. Terminal → immediate status read with no form reopened. Refused because that backend already holds its one `native_cli` Source `[contract]` → Already bound, and any other answer that is not a flow → Start failed; either path closes an unused blank context. Polling, re-render and reconciliation never auto-open a second context `[derived]` |
| §1.4 | Second pass `[derived]` | Re-opened while this backend already holds its one `native_cli` source `[contract]` | F5 | `addSub.opt.added` | The native row is inert whichever account that source holds; the hub row stays choosable and is selected on open, whatever the recommendation says |
| §1.4 | Awaiting sign-in | An acquired flow carries `presentation.expects: none` and no `device_code`; Form B remains G-33. `GET /api/models/oauth/status/<flow_id>` is polled every 2s until the §1.4 evidence-class matrix selects an exit `[contract]` | → OAuth failed / OAuth materialization failed. E2 transport/outage evidence, including `engine_down`, is inconclusive and the next 2s tick retries under the same bound (D-16); E4 or E8/E9 with the held Source present/unread stops at OAuth failed; only E6's two materialization codes stop at OAuth materialization failed | `addSub.signIn`; PD-4 resolves `presentation.instructions_key` and uses the device-code helper on null or lookup failure | PD-2 keeps the authorization link actionable. E5 `success` → `intent: create` closes **into 06 for the source that terminal names** with `added_to` and `adopted_by` in hand; `intent: reauth` → §1.11's R3 repair terminal `[contract]`; E4 → OAuth failed; E8/E9 first run RR-5's registered read, then absent → M0 / Source gone, present or unread → OAuth failed (the unread branch retains read-only Retry); E7 → M0 / Source gone; the polling bound passes with no terminal reading → OAuth failed — the bound is `OAuthFlow.expires_at` when the flow carries one `[contract]` and 15 minutes from acquisition when it does not `[derived]`; dismissed any of the three ways → Dismissing |
| §1.4 | Awaiting paste-back | The flow carries `presentation.expects: paste_code` or `paste_callback_url` and E3a reads `starting` / `awaiting_action` `[contract]`; entry is either the acquired form with an empty draft or a submit/reconciliation return with its held value | F5 until submit; while `auth_url` is null, PD-5 owns a 2s presentation-delivery read under the existing bound and E2 remains inconclusive | `addSub.paste.title.code`, `addSub.paste.title.callbackUrl`, `addSub.paste.subtitle`, `addSub.paste.label.code`, `addSub.paste.label.callbackUrl`, `addSub.paste.placeholder.code`, `addSub.paste.placeholder.callbackUrl`; PD-4 resolves `presentation.instructions_key`, with `addSub.paste.hint.code` / `addSub.paste.hint.callbackUrl` on null or lookup failure; `addSub.paste.submit`, `addSub.cancel` | The first non-null URL from acquisition or PD-5 invokes PD-2 on the one original browser context; no second flow/context is opened. V4 gates 提交 → Submitting paste-back; only that explicit gesture sends or resends the held value. 取消 / close / outside press → Dismissing (C7/C11/C12/DP-3) |
| §1.4 | Submitting paste-back | 提交 pressed — `POST /api/models/oauth/submit` with the held `{flow_id, value}` `[contract]` | F1 → Paste-back failed for E2; only E6 → OAuth materialization failed | `addSub.paste.submitting` | E5 dispatches by held intent: `create` closes into 06; `reauth` → §1.11's R3 repair terminal. E4 → OAuth failed; E8/E9 first reconcile the reauth attempt scope, then absent → M0 / Source gone, present or unread → OAuth failed (read-only Retry when unread). E3a `starting` / `awaiting_action` proves the value was not accepted and returns to Awaiting paste-back with it retained; E3b `verifying` alone → Awaiting paste-back completion. E2 transport/no answer → Paste-back failed; E7 → M0 / Source gone |
| §1.4 | Awaiting paste-back completion `[contract]` | Submit or the reconciliation read below answered with `OAuthFlow.state: verifying`, which is E3b's positive evidence that the held paste value was accepted | The same §1.4 evidence-class matrix and expiry/15-minute bound as Awaiting sign-in: E2, including `engine_down`, continues; only E6 stops at materialization failure | `addSub.paste.submitting`, `addSub.cancel` | E5 dispatches by held intent: `create` closes into 06; `reauth` → §1.11's R3 repair terminal. E4 → OAuth failed; E8/E9 first reconcile the reauth attempt scope, then absent → M0 / Source gone, present or unread → OAuth failed (read-only Retry when unread); E3b stays here; E3a returns to Awaiting paste-back with the value retained for an explicit submit; E6 → OAuth materialization failed; E7 → M0 / Source gone. 取消 / close / outside press → Dismissing |
| §1.4 | Paste-back failed `[derived]` | The submit request returned E2 evidence — transport/no answer or an outage such as `engine_down` — so its flow outcome is unconfirmed | F1, in place; the input, `flow_id` and intent are kept | `addSub.paste.fail`, `addSub.retry`, `addSub.cancel` | 重试 first re-reads `GET /api/models/oauth/status/<flow_id>` (D-36): E5 dispatches by held intent to 06 or §1.11's R3 repair terminal; E4 → OAuth failed; E8/E9 first reconcile the reauth attempt scope, then absent → M0 / Source gone, present or unread → OAuth failed (read-only Retry when unread); E6 → OAuth materialization failed; E3a `starting` / `awaiting_action` → Awaiting paste-back with the value retained, where only an explicit 提交 resends it; E3b `verifying` → Awaiting paste-back completion without resubmission; E2 leaves this state unchanged; E7 → M0 / Source gone. 取消 → Dismissing |
| §1.4 | Dismissing | 取消, the close icon, or a press outside, while a flow is in flight | F4 — the dialog closes immediately; RR-10 owns cleanup in the background (D-15) | — | The cleanup owner first settles the authorized `POST /api/models/oauth/cancel` attempt and then always re-reads the affected projection: source list for create, M3's complete model surface for reauth. Cancel failure, an ownership handoff, or a terminal flow does not suppress that later read; no late answer restores the dialog (D-16) |
| §1.4 | OAuth failed | E4 reached a create terminal failure; or E8/E9 reached a reauth `flow_not_found`, `flow_expired`, `failed` or `cancelled` terminal and RR-5's mandatory attempt-scope read found the Source **or itself failed**; or the polling bound passed with no terminal reading. The flow failure selects this state only after any intent-owned reconciliation attempt; the intent and an unresolved E8/E9 read are held | F1 for the retry itself; **F4 for the cleanup below**, whose background owner sequences cancel then reread | `addSub.error.oauthFailed`, `addSub.retry` | **Fresh acquisition preserves intent** `[contract]`: `create` sends `POST /api/models/oauth/start`; `reauth` repeats §1.11's held producer with confirmed `{acknowledge_irreversible: true}` and never calls the create route. RR-1/RR-2 classify its answer, so an already-terminal retry response is status-read and never presented. Resolved-present E8/E9 may acquire fresh; unresolved E8/E9 first repeats **only** RR-5, then absent → M0 / Source gone, present → fresh acquisition, still unread → stay without resending. After the polling bound, first re-read `GET /api/models/oauth/status/<flow_id>` once (D-32): E5 dispatches by intent to 06 or R3; E6 → OAuth materialization failed; E8/E9 run RR-5 before Source gone / this state; E4 → fresh acquisition; paste E3a → Awaiting paste-back with value retained; E2, E3b or non-paste E3a → launch fresh acquisition and F4 cleanup together, while cleanup settles cancel before RR-10 reread; E7 → M0 / Source gone. 取消 → Dismissing |
| §1.4 | OAuth materialization failed `[derived]` `[contract]` | Status or submit returned E6's `discovery_failed` or `migration_item_conflict` after this dialog acquired the flow. Authorization entered materialization and the affected source projection may already have changed; no other named code enters this state. R5 consumes the standard error envelope and its conditional `interrupted_pairs` | F2 while RR-5 is outstanding or unread; polling stops immediately | `addSub.error.finalize` for `intent: create`, `addSub.error.finalizeReauth` for `intent: reauth`, optional `sourceDetail.impact.interruptedModels`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `addSub.retry` | Render a present nonempty `interrupted_pairs` report before RR-5's mandatory attempt-scope reread; an absent member renders no report and proves no clean outcome. Create rereads the Source list; reauth rereads M3's complete model surface and applies M0 / Source gone precedence. Keep the failure and exact report mounted while the read runs. An unread result enables only a read-only 重试 of RR-5; it cannot start another OAuth acquisition. After the read settles, Source gone wins where applicable; otherwise the same button may begin fresh acquisition with the held intent/channel body. 取消 → Dismissing |
| §1.4 | Engine unavailable | The gateway is not running and gateway-upstream was chosen | F1 | `addSub.error.engineDown`, `addSub.retry` | 重试 re-sends, and that press **is** the recovery observation — nothing here watches for one — so its answer decides: whichever of *Awaiting sign-in* / *Already bound* / *Start failed* the start call then names, or still down → back here; 取消 → dismiss, nothing bound `[derived]` |
| §1.4 | Already bound | 去登录 was refused by the start call because that backend already holds its one `native_cli` Source — 「the API rejects duplicate creation with the existing Source id」 `[spec §4.1]`. **This is the race the dialog cannot see**: it disables the native row from the sources it read on open, and the singleton can appear after that | F1, in place — nothing was sent to the provider, so there is no flow to cancel | `addSub.error.alreadyBound`, `addSub.retry` | 重试 → Second pass: the dialog re-reads the sources, the native row is now the inert one, and the hub row is what 去登录 sends; 取消 → dismiss, nothing bound `[derived]` |
| §1.4 | Start failed | `POST /api/models/oauth/start` did not put a flow in this dialog's hands, for any reason that is not the singleton refusal above — a refusal, or no answer at all. The exact generated `(client_nonce, vendor, channel)` tuple remains held `[contract]` | F1, in place — there is no `flow_id` to poll or cancel, so D-36 reconciliation repeats only the held tuple. The server coalesces an in-flight claim, returns its one committed flow, or releases the tuple for one fresh provider start | `addSub.error.startFailed`, `addSub.retry` | 重试 sends the same tuple and classifies the answer as Default does: accepted/coalesced flow → RR-1/RR-2, singleton refusal → Already bound, still no flow → back here. It never opens a second provider start beside the first. 取消 dismisses; a claim released before flow creation has no Source binding `[derived]` |
| §1.5 | ① Default | Dialog opened; Base URL or API key empty | F5 | `addKey.title`, `addKey.subtitle`, `addKey.field.vendor`, `addKey.field.vendor.custom`, `addKey.field.vendor.hint`, `addKey.field.name`, `addKey.field.baseUrl`, `addKey.field.baseUrl.hint`, `addKey.field.apiKey`, `addKey.field.apiKey.reveal`, `addKey.field.apiKey.conceal`, `addKey.field.protocol`, `addKey.protocol.auto`, `addKey.protocol.idleHint`, `addKey.protocol.manual`, `addKey.protocol.catalogPinned`, `addKey.detect`, `addKey.cancel` | 服务商 is the first field (自定义 · 兼容端点 by default). A catalog vendor prefills Base URL, locks 接口类型 to the catalog protocol with badge 「内置目录」, and hides the manual disclosure. On 自定义, interface type is a dimmed result area; the collapsed disclosure `addKey.protocol.manual` holds Auto plus the three concrete interfaces as a **declaration**, not a probe constraint. Auto carries no glyph; a concrete option carries its protocol-family glyph. 检测 is disabled until Base URL and API key are filled. 取消 → dismiss |
| §1.5 | ① Ready `[derived]` | Base URL and API key filled; no fresh observation | F5 | ①'s keys, undimmed | 检测 → ②. A catalog vendor sends its pin; a concrete disclosure selection is a declaration on `custom`; Auto auto-detects. 取消 → dismiss |
| §1.5 | ①″ Identified `[derived]` | ② came back `observed`, protocol non-null, `discovery: succeeded` | F5 — the request already succeeded | `addKey.pull.result`, `addKey.pull.empty`, `addKey.protocol.anthropicMessages`, `addKey.protocol.openaiResponses`, `addKey.protocol.openaiChatCompletions`, `addKey.confirm`, `addKey.protocol.manual`, and ①'s still-visible form | Mint strip: protocol-family glyph + established protocol label + count (or `addKey.pull.empty`). No model names. Editing Base URL, API key, or the protocol selection → ① Ready, the report dropped — §1.5's retirement property, of which this is the plainest case. A rename is not one of those three inputs and keeps the report. 确认添加 → ②″ with the established protocol and `accept_unavailable_inventory` false / omitted `[contract]`. 取消 → dismiss |
| §1.5 | ② Detecting | 检测 pressed, or 重试 pressed from ③ / ④ / ⑤ / ⑥ — `POST /api/models/sources/observe` `[contract]` | → ③ / ④ / ⑤ / ⑥ through O1 | `addKey.protocol.detecting` | O1 classifies the explicit observation. Ready → ①″, persisting nothing. 取消 aborts the in-flight probe and returns to ① Ready with the form intact. A second detect cannot start while one is in flight |
| §1.5 | ②″ Saving `[derived]` | 确认添加 pressed from ①″, or 仍要添加 pressed from ⑤ — `POST /api/models/sources` with one generated `client_nonce` `[contract]` | → ③ / ④ / ⑤ / ⑦ through O1 | `addKey.saving`, `addKey.saving.detail` | Cancel is blocked. An accepted create closes into 06. A classified create refusal returns to ③ / ④ / ⑤ exactly as the same observation would; only transport/no answer or a create response with no terminal observation classifier → ⑦ |
| §1.5 | ③ Failure | O1 received one of four closed unsuccessful outcomes from the explicit 检测 observation or Source create's repeated observation: `authentication_failed`, `unreachable`, `timeout` or `adapter_error` `[contract]` | F1 | `addKey.fail.subtitle`; exactly one outcome line: `authentication_failed` → `addKey.fail.auth` + `addKey.fail.auth.detail`, `unreachable` → `addKey.fail.address`, `timeout` → `addKey.fail.network`, `adapter_error` → `addKey.fail.unclassified`; `addKey.retry` | 重试 → ②, whichever producer returned the line — the retry re-runs the explicit observation before any create and does not depend on what the last one concluded. 取消 → dismiss |
| §1.5 | ④ Interface undetermined | O1 received `SourceObservation.outcome: ambiguous` from the explicit 检测 observation or Source create's repeated observation: reachable, protocol null, and authentication authenticated or unknown `[contract]` — Auto detect on `custom` with no shape proof | F1 | ①'s still-visible form plus `addKey.undetermined.title`, `addKey.undetermined.detail`, the four-segment selector expanded in place (concrete options carry protocol-family glyphs), `addKey.field.protocol.hint`, and `addKey.retry` | Choose one concrete interface + 重试 → ② as a **declaration**: authenticated on that path → ①″ (discovery succeeded) or ⑤ (discovery failed), persisting nothing yet; Auto still selected → 重试 stays disabled; still ambiguous only if Auto is retried. 取消 → dismiss |
| §1.5 | ⑤ Identified, inventory unavailable `[frame]` `d6bFlX` | O1 received `SourceObservation.outcome: observed`, protocol non-null, and `discovery: failed` from the explicit 检测 observation or Source create's repeated observation; the contract carries no request/status/reason evidence `[contract]` | F1 | `addKey.inventory.title`, `addKey.inventory.detail`, `addKey.retry`, `addKey.addAnyway` | 重试 → ② (the entire explicit observation). 仍要添加 is offered whenever this inventory cell carries an established protocol owner — it is not origin-scoped. 仍要添加 → ②″ with **`accept_unavailable_inventory: true`** `[contract]`; false / omitted remains the clean path and may not commit this repeated discovery-failed observation. The server still repeats response-backed observation: only the same protocol-established / discovery-failed cell may commit with `models: []`; every other classified result remains O1's own and the flag supplies no evidence. Only that committed success envelope closes into 06. This state renders no form, so it offers no edit to retire its waived cell; §1.5's retirement property binds it regardless and needs no exception. 取消 → dismiss |
| §1.5 | ⑥ Engine unavailable `[derived]` | The gateway is not running when 检测 is pressed | F1 | `addKey.fail.engineDown`, `addKey.retry` | F1 in full: the form keeps every value it holds and the primary becomes 重试. Pressing it **is** the recovery observation — nothing here watches for one — and re-attempts 检测 → ②, whose own outcomes then apply; the engine still down → back here; 取消 → dismiss |
| §1.5 | ⑦ Save unconfirmed `[derived]` | The observation 确认添加 / 仍要添加 owed has already come back with consent — clean in ①″, identified after a hint in ④, or waived in ⑤ — and the complete `SourceCreate` then returned no terminal observation classifier or never answered. Every classified pre-commit observation instead leaves through O1 to ③ / ④ / ⑤. The generated `client_nonce` and every request field remain held `[contract]` | F1 | `addKey.fail.save`; after `source_create_in_progress`, `addKey.fail.inProgress`; `addKey.retry` | Each user press of 重试 first reads `GET /api/models/sources` for the exact nonce. Match → claim the committed Source and enter 06 through the ordinary projection read. Miss → resend the same request/nonce. A response-backed observation enters O1; nonce conflicts follow the exact branches here. `source_create_in_progress` returns to this actionable state with `addKey.fail.inProgress`; there is no timer, automatic list read or automatic mutation retry. A later user press alone repeats the list-read + same-nonce resend algorithm. `source_nonce_conflict` rereads the list and claims the committed Source; a classified pre-commit observation → ③ / ④ / ⑤; a released slot starts one fresh create. Committed never replays the original response, and no branch matches by URL/name or creates a second Source (D-36). 重试 resends the held request rather than the form, so F1 here keeps the form's values while locking every field: the request may not outlive the fields it was built from, and this state resolves that by making them uneditable rather than by retiring on an edit that cannot happen. §1.5's retirement property still binds it. 取消 dismisses; after a committed boundary it ends only the caller's wait and the next Source/Agent read owns the result |
| §1.6 | Ready | Source detail loaded and the table holds at least one model — discovered, added by hand, or both. When `last_discovered_at` is null the inventory has no age, `{{time}}` is absent by §0.9's rule, and the status line drops that segment `[contract]` | F5 | `sourceDetail.status.inUse`, `sourceDetail.status.listUpdated`, `sourceDetail.summary` | `iGcAi` → 01; a rendered 重新拉取 → Refetching; 添加模型 → Manual draft; a tier area → Tiers editing; a row's overflow → Removing a manual entry. The §1.6 action-capability row decides whether 重新拉取 exists |
| §1.6 | Retired discovered row | A model row carries `origin: discovered` and `retired: true` `[contract]` | F5 — a durable projection, not a request | `sourceDetail.entry.retired` | Keep the ordinary row chrome, switch its ink to muted and append the existing tag component with 已退役 / Retired. The row stays readable and never supplies. It renders no discovered-row delete action; refetch preserves the tombstone and cannot revive it. No new component or reactivation exit exists |
| §1.6 | Empty (no models) | The table is empty **and** a discovery has completed — `last_discovered_at` is non-null `[contract]` | F5 | `sourceDetail.empty` | Manual add, or a successful refetch |
| §1.6 | Never fetched | The table is empty **and** no successful discovery has ever completed — `last_discovered_at` is null `[contract]` | F5 | `sourceDetail.emptyNeverFetched` | A successful 重新拉取, or a manual add that commits → Ready |
| §1.6 | Refetching | 重新拉取 pressed — `POST /api/models/sources/<source_id>/refresh`, guarded | F3 when the response is a guard refusal, F2 otherwise → Refetch failed | `sourceDetail.action.refetch` | Any answer this page can read → Refetch result, **whatever the row count comes back as, none included**. An emptying refetch is not a different kind of answer, and it is the one where the diff is worth most: every id the source used to advertise has just left the discovered slice, so it is where `sourceDetail.refetch.removed` names the largest set it will ever name. Leaving straight for Empty (no models) — as this row read until this round — dropped that report at that exact moment, and replaced it with a sentence that says a fetch returned nothing and not what stopped being there. The empty table is still an empty table: *Refetch result* renders `sourceDetail.empty` under the removal line, and of §1.6's two empty readings only that one is reachable from here, since this refetch completing is what makes `last_discovered_at` non-null. It is where *Refetch result* lands rather than what this state enters; a refusal → Refetch refused |
| §1.6 | Refetch result `[derived]` | A refresh came back usable — the response carries the complete updated source `[contract]`, and this page still holds the list it was rendering, so what changed is a comparison of two payloads it has | F5 — the request already succeeded | `sourceDetail.refetch.added`, `sourceDetail.refetch.removed`, `sourceDetail.refetch.unchangedOnly`, and the keys for whatever the table now is: Ready's own while any row is left, `sourceDetail.empty` when the answer emptied it. The diff line and the table's own copy are two statements about one payload, and the second never cancels the first | Any next action, or the next load → Ready, or → Empty (no models) when the answered list was empty: the diff is a report about one fetch and nothing re-derives it, so what remains afterwards is the table's own reading. 重新拉取 again → Refetching |
| §1.6 | Refetch refused | The refresh came back refused, naming the hops a shorter inventory would remove | F3 — `Qp6FI`, the same confirm this page already starts for a removal | `guard.title.refetch`, `guard.subtitle.refetch`, `guard.confirm.refetch`, `guard.label`, `guard.count`, `guard.hop.position`, `guard.hint.safe`, `guard.hint.interrupt`, `guard.gap.label`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `guard.cancel` | 仍要拉取 re-sends the same `POST` with `force` → Refetching; 取消 restores the exact held §1.6 origin by C5 |
| §1.6 | Refetch failed | The refresh came back a classified failure, **or never answered at all** — F2's own 「otherwise」 lands both here. For the classified one `api.md` has the server update the **source-global state**, preserve the last successful model list and timestamp, and return the normal safe error `[contract]`, so the source changed state and the answer does not carry which. For a lost answer even that is not known: F1's rule leaves the outcome unknown, and a refresh that did commit has already replaced the discovered inventory — on the `force` path, after removing the hops the guard named | F2 lands here. The **previous list is kept only where the contract keeps it**, which is the classified branch and is the contract's guarantee rather than this page's choice; a lost answer guarantees nothing, which is why the re-read below is a reconciliation and not a status refresh | `sourceDetail.fail.refetch`; the status word is not this row's and comes from whichever state the re-read below lands on | The failure line goes on the bar, and the page re-reads `GET /api/models/sources` — the only read that answers with this source, there being no single-source route `[contract]`. **That read settles the mutation as well as the status**, because the page has held `source_id` since it sent the refresh and the list answers with the complete `Source`, models included `[contract]` (D-36): what it renders afterwards is that source as read, so a refresh that committed unseen is visible rather than papered over by the list this page was holding. The state it finds dispatches through this section's own status mapping: `cooldown` → Cooling, `needs_action` → Needs action, `error` → Unclassified error; `active` or `standby` with no Hub-mode backend adopting it → Not supplying, and either status with a Hub-mode backend adopting it → Ready. The source absent from that list → Source gone. The re-read itself failing → the bar keeps the status word and the page keeps the list it was already rendering, a failed read being no reading at all (D-16), and the line above stays true of both. 重新拉取 stays enabled throughout → Refetching |
| §1.6 | Tiers editing | A row's tier area was activated | F5 — nothing is sent until a tier is committed | `sourceDetail.tiers.add`, `sourceDetail.tiers.inputHint`, `sourceDetail.tiers.empty`, `sourceDetail.tiers.addFirst` | Enter, or a chip's × → Tier commit; Enter on an empty input, or on a value this row already carries, commits nothing and stays here — `minLength: 1` and `uniqueItems` `[contract]`; blur / Escape → Ready, discarding whatever is still uncommitted in the input |
| §1.6 | Tier commit | A tier was added by Enter **or** removed by a chip's × — either way `PATCH /api/models/sources/<source_id>/models/<model_id>` carries the complete `reasoning_efforts` list `[contract]` | F1, on the row: the row keeps the pre-request list, states the failure and offers 重试 — an add leaves its text in the input, a removal puts its chip back | `sourceDetail.fail.tier`, `sourceDetail.retry` | Success → the answered list is what the row renders, still in Tiers editing |
| §1.6 | Manual draft | 添加模型 pressed | F5 — a local draft, sent by nothing | `sourceDetail.entry.manual`, `sourceDetail.addRow.hint` | 添加 is enabled only once the id field holds a value this source's table does not already list `[derived]`: blank, whitespace-only, or an already-listed id leaves it **disabled**, the same answer 「Tiers editing」 gives an empty or duplicate tier, and `sourceDetail.addRow.hint` is what the row states meanwhile. Enabled 添加 → Manual commit; 取消 discards the row and nothing is persisted |
| §1.6 | Manual commit | 添加 pressed on an enabled 添加 — `POST /api/models/sources/<source_id>/models` | F1, on the draft row: **the row and everything typed in it are kept**, and the primary becomes 重试 | `sourceDetail.fail.addModel`, `sourceDetail.retry` | Success → the row becomes an ordinary 手动添加 row → Ready |
| §1.6 | Removing a manual entry | The row menu's 移除 — `DELETE /api/models/sources/<source_id>/models/<model_id>`, guarded | F3 when the response is a guard refusal, F1 otherwise | `sourceDetail.fail.removeModel`, `sourceDetail.retry`, `sourceDetail.row.remove` | Success → the row is gone → Ready while any row is left; removing the last one → Empty (no models) or Never fetched by `last_discovered_at`, the same two readings §1.6 gives every other empty table |
| §1.6 | Guard refused | The `DELETE` came back refused, naming the hops it would remove | F3 — `Qp6FI`, this product's one guarded-change confirm | `guard.title.removeModel`, `guard.subtitle.removeModel`, `guard.confirm.removeModel`, `guard.label`, `guard.count`, `guard.hop.position`, `guard.hint.safe`, `guard.hint.interrupt`, `guard.gap.label`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `guard.cancel` | 仍要移除 re-sends the same `DELETE` with `force` → Removing a manual entry; 取消 restores the exact held §1.6 origin by C5 |
| §1.6 | Not supplying | The source is `active` or `standby`, and no Hub-mode backend adopts it `[contract]` | F5 | `upstream.state.standby` | A Hub-mode backend adopts the source — only the bar changes |
| §1.6 | Cooling | The source is `cooldown` `[contract]` | F5 | `upstream.state.unavailableRetry` while `retry_at` is still ahead, `upstream.state.unavailableDue` once it has passed `[derived]` | A later payload reports the source in another state → that state; `retry_at` passing is not that payload — it changes which of the two keys the bar renders and nothing else. Separate from *Not supplying* because the two answer different questions — standby says nothing is drawing from this source, cooling says nothing *can* yet and names when that changes. This section's own status mapping gives `cooldown` the gold `upstream.state.unavailableRetry`; a row that folded it into the muted standby word dropped the one fact the mapping exists to carry |
| §1.6 | Needs action | The source is `needs_action` `[spec §4.5]` | F5 | `sourceDetail.status.needsAction.oauthExpired`, `sourceDetail.status.needsAction.balanceExhausted`, `sourceDetail.status.needsAction.credentialRevoked`, `sourceDetail.status.needsAction.accountBanned` | The reported cause clears. The bar states which cause `state.detail_key` carries and the table stays live; frame 12 owns the card-level repair controls, while this page keeps 重新拉取 enabled only when the §1.6 action-capability row renders it |
| §1.6 | Unclassified error | The source is `error` `[spec §4.5]` | F5 | `sourceDetail.status.error` | The source leaves `error`. The bar reads 异常 and claims no cause; the table and every action the capability row renders stay live |
| §1.6 | Source gone | The selected source is not in a `GET /api/models/sources` read this page makes, or any source-bound mutation registered in §1.6, §1.10 or §1.11 answers `source_not_found` `[contract]` — removed by another tab, another API client, or a guarded cascade while the surface was open. This entry has precedence over every caller's F1/F3 branch; those treatments apply only after the absence reading is excluded. Distinct from every empty reading above, which is about this source's *models*; here the subject of the page is what is gone, and the list may still hold others, so §1.0's Empty does not answer either | M0 owns the read: preserve exact absence, remove the Source locally and refresh Sources + Agent supply/order + every Route chain. F2 if that read fails; the absence itself remains authoritative | `sourceDetail.gone`; on stale dependent projections reuse `sourceDetail.remove.impact.refreshFail`, `sourceDetail.retry`, `sourceDetail.remove.impact.done` | Drop the detail/card overlay immediately. M0 success hands current projections to 01. On read failure → Committed projection stale with the exact absence and last-good dependent projections. Retry repeats only M0; DP-4's Done, back, close, Escape, outside press and ordinary navigation remain legal non-mutating exits. No caller may bypass M0, re-send the mutation, restore the Source, question the absence or invent impact (C13/C14) |
| §1.7 | Nominal | **No route is taken over or exhausted** — every configured chain is serving its own first stored hop. AC-30 makes takeover 「a projection of visible configuration plus live runnability」 rather than a stored sibling state `[contract]`, so the subject of this frame is chains and the predicate is read per chain: a Source no chain draws from, and a non-head hop that is unavailable while the head still serves, are both outside it. Reading it globally — 「no source is unavailable」 — activated 08 for an unhealthy Source nothing was using, and left the frame with no valid state whenever one existed | F5 | — | A head enters a recoverable quota/cooldown or live connection-backoff while a later candidate serves → Takeover; a head stops being runnable for something waiting does not clear → §1.1 *Serving past a blocked head*, which is not this frame |
| §1.7 | Takeover | The head is unavailable **for a recoverable quota/cooldown or live connection-backoff reason** and a next candidate is serving `[contract]` — §4.3 derives takeover from exactly that pair, and the paragraph below says what the other blocker kinds render instead | F5 | `takeover.pill`, `takeover.chip` | Re-evaluate the complete predicate on every authoritative chain payload under §0.8's shared derived-state rule. As soon as it is false, retire Takeover: a recovered head serving → Nominal; `current` still naming the successor after the head recovers → the ordinary serving/current-hop projection, with no violet latch and no wait for another turn |
| §1.7 | Exhausted | The head is unavailable and **no** candidate remains | F5 | `gateway.supply.none` | Any candidate recovers → Takeover or Nominal |
| §1.7 | Multiple takeovers | More than one backend was rerouted | F5 | `takeover.pill` | Each backend recovers independently |
| §1.7 | Loading / Empty / Unreachable | As §1.0 | As §1.0 | — | As §1.0 |
| §1.8 | Ready (first run) | `installedAgents` is nonempty, every member is in 直连, and no source exists | F5 | `direct.card.current`, `direct.card.current.sub`, `direct.pill.direct`, `direct.backend.claude.detail`, `direct.backend.codex.detail`, `direct.backend.opencode.detail`, `direct.benefits.title`, `direct.benefits.1` … `direct.note.perBackend`, `shell.allDirect` | 切换到网关 on a row → 10's confirm for that backend; a source added or an installed backend switched → 01 |
| §1.8 | Loading | First paint | → §1.0 Unreachable / §1.0 Sources unread / §1.0 Partial — the same three §1.0 disperses first paint into, because this is that same first paint seen from the direct home: the runtime status failing or reading `down` → Unreachable, `GET /api/models/sources` failing → Sources unread, per-backend supply failing → Partial. Neither page read may be dropped into Unreachable, whose entry is a runtime-status reading: this frame needs the source list to tell *Ready (first run)* from *Retained sources*, and needs the supply read to derive `installedAgents`, so a read that failed is what has to be said `[derived]` D-34 | — | Payload arrives → Retained sources when any Source exists, even with `installedAgents` empty; otherwise No backend found when `installedAgents` is empty, Ready (first run) when every nonempty member is direct, or 01 when any installed member is on Hub. These are exactly §1.8's branch rules below |
| §1.8 | No backend found | No Source exists **and** `installedAgents` is empty: every `AgentSupply.cli_present` is false `[contract]` | F5 | `direct.empty.title`, `direct.empty.body`, `direct.empty.install` | **Install a backend CLI and reload.** Neither card renders and the pill is absent; a Source appearing selects 01, while an installed Agent appearing re-dispatches the no-Source page by mode. Hidden persisted modes cannot suppress this zero-installed branch |
| §1.8 | Retained sources | At least one Source exists, regardless of whether `installedAgents` is empty, all direct or contains a Hub member — reachable through `adopt.undo.3` and retained storage `[contract]` | F5 | — | The page is 01: render the upstream module and exactly the zero or more groups in `installedAgents`. An empty installed set renders zero groups and is never quantified or described as “all direct.” This frame stays absent until the last Source is removed, when the no-Source branch is evaluated |
| §1.9 | Default | 切换到网关 pressed on a backend row | F5 | `adopt.title` … `adopt.undo.3`, `adopt.confirm`, `adopt.cancel` | 取消 → dismiss unchanged; 切换到网关 → Committing |
| §1.9 | Committing | The confirm's primary was pressed — `PATCH /api/models/agents/<backend>/mode` | F1 → Failed | — | Success holds the returned `AgentSupply` as write evidence and enters M5's Source-list read. That read succeeds → the dialog closes and page 01 receives both current projections; it fails → Committed projection stale, never Failed and never a repeated mode `PATCH` |
| §1.9 | Failed | A step this confirm promised did not go through | F1 lands here | `adopt.fail.title`, `adopt.fail.detail`, `adopt.fail.reason.transport`, `adopt.fail.reason.refused`, `adopt.fail.reason.notReady`, `adopt.fail.reason.unknown`, `settings.models.install.fail.detail` | The dialog stays open, states the failure, keeps 取消 enabled and the primary retryable. The install step consumes the closed `settings.models.install.fail.detail` verbatim from `RuntimeDependency.status.error_key`; start and mode request failures render `adopt.fail.detail`, whose `{{request}}`, optional `{{status}}` and total `{{reason}}` come only from that request state. **重试 re-reads before it resumes** `[derived]`: `installing` stays busy; `not_installed` with the install error retries install; `not_started` or `down` resumes at runtime start; `ok` or `degraded` resumes at the mode `PATCH`; `GET /api/models/agents` reading `mode: hub` holds that AgentSupply as D-36 commit evidence and enters M5 instead of resending or closing. The install route is not retried from `down`, where the contract makes it a no-op. No branch replays a step the authoritative reads already prove complete (D-36) |
| §1.9 | Dependency missing `[derived]` D-26 `[contract]` | Runtime `health` is `not_installed` with `error_key: null` on a supported `host_platform` (§1.0) | F1 → Failed | `adopt.effects.install`, `adopt.confirm.install` | The confirm gains one line naming the component and roughly how long it takes, and the primary becomes 安装并切换 — one press holds H1's `install_start_switch` across three contracted steps: runtime install → runtime start → mode `PATCH`. `installing` keeps the dialog mounted across reload-equivalent reads; `not_started` advances to start only because that sequence is held; live advances from Starting to the mode `PATCH`; M5's acceptance of committed AgentSupply evidence releases the sequence and makes M5 the sole owner of the Source read. An install failure renders the closed `status.error_key` and retains the sequence in Failed; a later Retry resumes at the first step the authoritative reads do not prove complete. 取消 is unchanged before commit and unavailable while a step owns its response |
| §1.10 | Edit open | 编辑来源 chosen from 06's source overflow; that menu's capability-gated credential actions remain beside Edit / Remove (C1) | F5 | `sourceDetail.edit.title`, `sourceDetail.edit.name`, `sourceDetail.edit.baseUrl`, `sourceDetail.edit.hint`, `sourceDetail.edit.cancel`, `sourceDetail.edit.save` | V1/V2 are the exhaustive field gates. 保存 is enabled only when every changed normalized value is valid and at least one differs from the held Source; 保存 → Saving source. 取消 / close / outside press → dismiss unchanged and return focus to the overflow trigger (C2/C3/C7) |
| §1.10 | Saving source | 保存 pressed — `PATCH /api/models/sources/<id>` with the normalized changed `display_name` and/or `base_url` `[contract]` | F3 when a Base URL change is refused; F1 otherwise → Source save failed. The request owns the dialog: Cancel, close, Escape and outside dismissal are disabled until it settles (C4) | `sourceDetail.edit.saving` | R1 owns the complete success envelope: non-empty impact → Source save impact reported; empty impact → hold the returned `source` while M1 reads the complete model surface, then close. A guard refusal → Source save refused; `source_not_found` → §1.6 Source gone |
| §1.10 | Source save refused | The guarded `PATCH` names the hops or supply gaps the Base URL change would remove `[contract]` | F3 — shared `Qp6FI` | `guard.title.editSource`, `guard.subtitle.editSource`, `guard.confirm.editSource`, `guard.label`, `guard.count`, `guard.hop.position`, `guard.hint.safe`, `guard.hint.interrupt`, `guard.gap.label`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `guard.cancel` | 仍要保存 re-sends the same `PATCH` with `force: true` → Saving source; 取消 → Edit open with both values kept |
| §1.10 | Source save failed `[derived]` | The `PATCH` failed or never answered | F1, in the edit dialog | `sourceDetail.edit.fail`, `sourceDetail.retry` | 重试 first re-reads `GET /api/models/sources` by the held source id (D-36): the requested normalized fields already present are authoritative commit evidence → M1 with the reread Source held and response-only `removed_hops` / `interrupted` explicitly unavailable, then close only after its complete-surface read; source absent → §1.6 Source gone; otherwise re-send → Saving source |
| §1.10 | Source save impact reported `[derived]` `[contract]` | R1 holds a successful metadata response with non-empty `removed_hops` and/or `interrupted` | F2 for M1's complete-surface read; the write already succeeded and the authoritative response is held. DP-4 owns every exit | `sourceDetail.edit.impact.title`, `sourceDetail.edit.impact.detail`, `sourceDetail.edit.impact.done`, `sourceDetail.edit.impact.refreshFail`, `sourceDetail.impact.removedHops`, `sourceDetail.impact.interruptedModels`, `guard.hop.position`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `sourceDetail.retry` | Render each non-empty array once: the existing hop block for `removed_hops`, the §1.11 SupplyGap block for `interrupted`. 完成, close, Escape or outside press first run M1's complete model-surface read; success returns with the current surface. Failure keeps this report and envelope mounted, adds the refresh-failure line and read-only 重试, and leaves 完成 plus every DP-4 equivalent exit active; those exits carry the held evidence and mark dependent projections stale. 重试 repeats only M1. No path restores the pre-write origin or lets a reread replace response evidence |
| §1.10 | Remove confirmation `[derived]` | 移除来源 chosen from 06's source overflow; the exact §1.6 source-detail state behind it is held | F5 — no request has been sent | `guard.title.removeSource`, `guard.hint.removeSource`, `guard.confirm.removeSource`, `guard.cancel` | 移除来源 → Removing source with the initial non-forced delete; 取消 / close / outside press restores the held §1.6 origin by C5 |
| §1.10 | Removing source | The destructive primary was activated in Remove confirmation or Source remove refused — `DELETE /api/models/sources/<id>` is non-forced on the first path and carries `?force=true` only on the second `[contract]` | F3 on refusal; F1 otherwise → Source remove failed. The request owns the dialog and all dismissal paths are disabled until it settles (C4) | `sourceDetail.remove.checking` | R2 owns the complete success envelope: either array non-empty → Source removal impact reported; both empty → remove the exact Source locally, run M2's complete model-surface read, then §1.6 Source gone. An initial guard refusal → Source remove refused; `source_not_found` → §1.6 Source gone |
| §1.10 | Source remove refused | The non-forced delete returned the guarded envelope; frame 11 renders its source-removal variant while retaining the held §1.6 origin | F3 — shared refusal semantics and the frame 11 dialog | `guard.title.removeSource`, `guard.confirm.removeSource`, `guard.label.removeSource`, `guard.count`, `guard.hop.position.removeSource`, `guard.hint.removeSource`, `guard.gap.label`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `guard.cancel` | 移除来源 re-sends the same `DELETE` with `force` → Removing source; 取消 / close / outside press restores the held origin by C5 |
| §1.10 | Source remove failed `[derived]` | Either delete request failed or never answered | F1, in place | `sourceDetail.remove.fail`, `sourceDetail.retry` | 重试 re-reads `GET /api/models/sources` by the held id (D-36): absence is authoritative delete-commit evidence → M2 with both response-only impact arrays explicitly unavailable, then §1.6 Source gone only after its complete-surface read; present → re-send the same stage, non-forced before refusal and forced after one |
| §1.10 | Source removal impact reported `[derived]` `[contract]` | R2 holds a successful delete response with non-empty `removed_hops` and/or `interrupted`; the source is already gone | F2 for M2's complete-surface read after a DP-4 exit; the read cannot negate the held delete response | `sourceDetail.remove.impact.title`, `sourceDetail.remove.impact.detail`, `sourceDetail.remove.impact.done`, `sourceDetail.remove.impact.refreshFail`, `sourceDetail.impact.removedHops`, `sourceDetail.impact.interruptedModels`, `guard.hop.position.removeSource`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `sourceDetail.retry` | Render the authoritative success arrays before any reread, even when they differ from the earlier refusal preview. 完成, close, Escape or outside press first run M2's complete model-surface read; success drops the detail into 01 with the current surface. Failure keeps the report and envelope mounted, states only that the surface refresh failed, adds read-only 重试, and leaves 完成 plus every DP-4 equivalent exit active; those exits carry the held delete evidence and mark dependent projections stale. 重试 repeats only M2. It never says the committed deletion is unconfirmed |
| §1.11 | Blocked source card | Frame 12's card delta is rendered for a source whose status is `needs_action` or `error`; both states reuse the same geometry and card target | F5 | `needs_action`: the selected `sourceDetail.status.needsAction.*` plus `upstream.state.supplyStopped` and its registered cause action. `error`: `sourceDetail.status.error` plus `upstream.state.supplyStopped`; Hub uses `sourceDetail.action.refetch`, while an unclassified `native_cli` login failure uses `upstream.repair.reauthorize` | Card → 06. For `needs_action`, OAuth expiry → Reauth confirmation; revoked key → Key entry; a known subscription vendor's balance/account link opens its §1.4 static destination → Vendor recovery observation; an `api_key` Source keeps the non-linked service-provider fallback. For `error`, Hub 重新拉取 → §1.6 Refetching; native 重新授权 → Reauth confirmation. A later payload always dispatches the status it actually reports |
| §1.11 | Vendor recovery observation `[derived]` `[contract]` | A subscription's 补充额度 or 联系厂商 destination was activated from Blocked source card. Opening the external page proves no recovery; the same single-action card shell stays mounted and enters this channel-selected observation phase | F5 — no model mutation or hidden poll is sent | the original cause plus `upstream.state.supplyStopped`; Hub `sourceDetail.action.refetch`; native `upstream.repair.reauthorizeToRefresh` | Hub 重新拉取 → §1.6 Refetching. Native 重新登录以刷新订阅状态 → Reauth confirmation and MUST pass its existing acknowledgement before reauth. A later payload that changes the Source state leaves this phase; a reload that still reads the blocker reconstructs Blocked source card. No branch treats opening or returning from the vendor page as recovery evidence |
| §1.11 | Reauth confirmation `[derived]` `[contract]` | 重新授权 or 重新登录以刷新订阅状态 pressed for a Hub or `native_cli` source from Blocked source card, Vendor recovery observation or the capability-gated source overflow | F5 — no request has been sent | `upstream.repair.reauthConfirm.title`; exactly one of `upstream.repair.reauthConfirm.detail.onFailure` or `upstream.repair.reauthConfirm.detail.immediate`; `upstream.repair.reauthConfirm.confirm`, `upstream.repair.reauthConfirm.cancel` | The confirmation phase and literal request value are shared, but the complete consequence body is selected by channel: Hub renders only `onFailure`; `native_cli` renders only `immediate`. 继续登录 synchronously preallocates PD-1's blank context, then sends `POST /api/models/sources/<id>/reauth` with `{acknowledge_irreversible: true}` → Reauthorizing; 取消 / close / Escape restores the exact invoking card/menu/observation origin and its focus target (C2/C3/C5/C8) |
| §1.11 | Reauthorizing | The shared acknowledgement was confirmed. The activating gesture has synchronously preallocated PD-1's blank context; `POST /api/models/sources/<id>/reauth` sends the confirmed `{acknowledge_irreversible: true}` for either supply channel `[contract]` | F1 before a flow is held → Repair failed, with every dismissal path locked while that request is pending (C4); PD-1 closes the unused context. After acquisition, §1.4 owns cancellation, 2s polling and F1–F5 | `upstream.repair.reauthorizing` | R3 and RR-1/RR-2 own acquisition: a non-terminal `flow` enters §1.4 with held `intent: reauth`, where E3a/E3b and PD-5 select the actual presentation/progress read; an already-terminal `flow` is status-read immediately and never presented. RR-4 classifies the materialized terminal by returned `source.state` before pair cardinality: blocked → Repair unresolved; non-blocked + pairs → Repair impact reported; non-blocked + empty → M3 handoff. E8 `flow_not_found` and E9 `flow_expired` / terminal failure run RR-5 before OAuth failed / M0 Source gone. Only E6 stops polling for materialization; R5 renders any exact error-envelope `interrupted_pairs` before RR-5's complete-surface refresh. E2 remains inconclusive. Create-only arrays are absent by contract |
| §1.11 | Key entry `[derived]` | 更换 Key pressed from either Blocked source card or the capability-gated source overflow, or a guarded refusal is abandoned | F5 — the secret remains local and no request is sent until submit | `upstream.repair.replaceKey` | V3 gates submit. A valid submit holds the normalized key and sends `PUT /api/models/sources/<id>/credential` with `{key}` → Replacing key; cancel restores the exact invoking origin and focus target (C2/C3/C5/C7) |
| §1.11 | Replacing key | Key entry submitted, or the guarded confirm below was accepted — the latter reuses the held key and adds `force: true` `[contract]` | F3 on guard refusal; F1 otherwise → Repair failed, with the key kept under F1. All dismissal paths are locked until the request settles (C4) | `upstream.repair.replacingKey` | R4 consumes the standard Source-mutation success: a non-empty `removed_hops` and/or `interrupted` → Repair impact reported; both empty → hold the returned `source` while M4 reads the complete model surface; refusal → Key replacement refused; failure → Repair failed |
| §1.11 | Key replacement refused `[derived]` `[contract]` | The non-forced credential replacement returned the shared guarded `409`; the typed key and exact Key entry origin are held | F3 — shared `Qp6FI`, with only the operation strings below changed | `guard.title.replaceKey`, `guard.subtitle.replaceKey`, `guard.confirm.replaceKey`, `guard.label`, `guard.count`, `guard.hop.position`, `guard.hint.safe`, `guard.hint.interrupt`, `guard.gap.label`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `guard.cancel` | 仍要更换 re-sends the held `{key, force: true}` → Replacing key; 取消 / close / Escape → Key entry with the typed key kept, by C2/C5 |
| §1.11 | Repair impact reported `[derived]` `[contract]` | R3 holds a successful OAuth repair whose returned Source is non-blocked and whose `interrupted_pairs` is non-empty, or R4 holds a successful key replacement with non-empty `removed_hops` and/or `interrupted`; the complete returned `source` is held | F2 for M3/M4's complete-surface read; the successful response is already in hand and DP-4 owns every exit | `upstream.repair.impact.title`, `upstream.repair.impact.detail`, `upstream.repair.impact.refreshFail`, `sourceDetail.impact.removedHops`, `sourceDetail.impact.interruptedModels`, `guard.hop.position`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `upstream.repair.impact.done`, `upstream.retry` | Render every non-empty response array under its matching hop or SupplyGap block. 完成, close, Escape or outside press first run M3/M4's complete model-surface read; success returns with the current surface. Failure keeps the exact report and envelope mounted, adds the refresh-failure line and read-only 重试, and leaves 完成 plus every DP-4 equivalent exit active; those exits carry the evidence and mark dependent projections stale. 重试 repeats only M3/M4. The reread never replaces response evidence or restores the invoking origin |
| §1.11 | Repair unresolved `[derived]` `[contract]` | R3 holds a successful OAuth repair whose returned `source.state` is still `needs_action` or `error`, regardless of `recovered` and whether `interrupted_pairs` is empty | F2 for M3's complete-surface read; this is a successful terminal with a blocked result, not an F1 failure | `upstream.repair.unresolved`, optional `sourceDetail.impact.interruptedModels`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `upstream.repair.impact.refreshFail`, `upstream.repair.impact.done`, `upstream.retry` | Keep the result visible in the gold needs-action treatment and render every non-empty `interrupted_pairs` block as independent evidence below it. 完成, close, Escape or outside press first run M3's complete model-surface read; success returns with the current blocked projection. Failure keeps this exact result and R3 envelope mounted, adds the refresh-failure line and read-only 重试, and leaves 完成 plus every DP-4 equivalent exit active; those exits carry the result and mark dependent projections stale. 重试 repeats only M3. Never show repaired/refreshed copy or auto-close from an empty array (C6/C10) |
| §1.2 / §1.6 / §1.9 / §1.10 / §1.11 | Committed projection stale `[derived]` | M0 has authoritative Source absence; an M1/M2/M3/M4 mutation or R3 terminal has commit evidence and its complete read failed; M5 holds committed mode-switch AgentSupply and its Source read failed; or M6/AR-M3 holds Route commit evidence with a settled acquired failed member subset. Hold the exact success envelope when received; inference holds its authoritative subject and marks response-only members unavailable. Every successful acquired projection stays installed; deferred companions are not stale | F2 — the operation-specific sentence names only the failed read/member, never the completed write, authoritative absence, deferred member or successful sibling | M0/M2 `sourceDetail.remove.impact.refreshFail`; M1 `sourceDetail.edit.impact.refreshFail`; M3/M4 `upstream.repair.impact.refreshFail`; M5 `upstream.unread`; M6 `route.impact.refreshFail`; the admitted owning Retry and, where mounted, its Done key | M0–M4 render committed Source/absence with last-good dependent projections stale; M5 renders held AgentSupply with Source stale; M6 renders every acquired AR-M success exactly and marks only AR-M3's acquired failed subset stale. Before mode, an Agents failure therefore shows only Agents stale while Source/Route remain deferred and absent from stale/Retry. Direct mode removes Route-chain members from the set rather than marking them stale. M0–M5 Retry repeats their owning read; M6 Retry repeats only the acquired failed subset through CA-M2/CA-M4. A successful Agents Retry activates exact mode companions once. DP-4 exits and page navigation remain legal. Never resend mutation, invent impact, discard evidence, roll back installed authority or describe the write/absence as uncertain (C6/C9/C10/C14/C15) |
| §1.11 | Repair failed `[derived]` | The pre-flow reauth request or credential replacement failed before its terminal could be confirmed; the repair intent, channel acknowledgement, exact origin status and any typed key remain held | F1, on the repair surface | `upstream.repair.fail`, `upstream.retry` | RR-6–RR-9 first perform the evidence-milestone read C10 registers: a pre-flow reauth acquisition failure reads the held Source id for Hub or native, while uncertain credential replacement reads M4's complete model surface. Then absent → M0 / Source gone; a held `needs_action`/`error` origin that is now clear → RR-7's M3/M4 handoff before rendering the reread Source as repaired; a still-blocked origin → remain here; any origin already non-blocked → remain regardless of present snapshot, because health is not mutation evidence. 重试 repeats the held producer. After a flow is acquired, E8/E9 and reauth E6 run RR-5's complete M3 read before OAuth failed / Source gone; E4 is create-only and E2 remains inconclusive. Every branch preserves `intent: reauth` |
| §1.12 | Closed | 添加订阅 is rendered in 01's upstream footer | F5 | `upstream.addSubscription` | Activate → Open |
| §1.12 | Open | The frame 13 vendor menu is visible and focus is on its first row; the Add subscription trigger remains held as the focus owner | F5 | `addSubMenu.vendor.claude`, `addSubMenu.vendor.chatgpt`, `addSubMenu.recommendation.native`, `addSubMenu.recommendation.gateway` | Claude 订阅 → §1.4 with `vendor: anthropic`; ChatGPT 订阅 → §1.4 with `vendor: openai`. Selection closes the menu; if 04 later dismisses back to 01, focus returns explicitly to Add subscription, while a committed exit gives focus to 06. Escape / outside press → Closed with the same trigger focus (C3) |

`Qp6FI` is not a frame of its own — its refusal rows sit with each caller, including
§1.2's Route save and the frame 11/12 Source-level mutations registered above.

### 0.9 Interpolation slot register

Every `{{slot}}` this document writes into a copy string is declared here, once.
A slot is a promise that something will be there at render time, and the promise
is only worth as much as the state that fills it — a string that interpolates a
status code into a failure that never had one renders a hole. So each slot
declares what fills it and, when it can be absent, what the string does instead.

Each row also names the keys that interpolate it, and that column is not
bookkeeping. A slot declared once for every consumer is a sentence that reads
true of whichever consumer its author had in mind: `{{status}}` was "the HTTP
status the upstream returned" while a fourth string filled it with supply health,
and `{{time}}` was "a relative timestamp" while one string looked backwards and
another forwards. Neither description was wrong about the slot; both were wrong
about a key. Listing the consumers puts them side by side, which is where a slot
that means two things becomes visible as two slots — `{{health}}` and `{{delay}}`
below are that split, not new vocabulary.

**Absence rule.** A slot that can be absent is rendered by dropping the slot
*together with the separator that precedes it*. `A · B · C` with `B` absent
renders `A · C`, never `A ·  · C` and never `A · — · C`. A slot marked
"always present" may not be dropped; if a state cannot fill it, that state uses
a different key.

| Slot | Filled with | Absent when | Interpolated by |
| --- | --- | --- | --- |
| `{{count}}` | A cardinality. The i18next plural family on the key picks the form; the number is never written into the singular text by hand. | Always present | `addKey.pull.result`, `gateway.moreModels`, `gateway.modelCount`, `guard.count`, `shell.allDirect`, `sourceDetail.refetch.removed`, `sourceDetail.summary`, `takeover.pill`, `upstream.count` |
| `{{backend}}` | The backend's product name — Claude Code, Codex, opencode — never the internal id. | Always present | `adopt.subtitle`, `adopt.title`, `adopt.undo.2`, `adopt.undo.3`, `guard.gap.subject`, `order.title`, `upstream.state.supplyingNative` |
| `{{vendor}}` | The upstream vendor's product name, as the user chose it. | Always present | `addSub.title`, `addSub.paste.title.code`, `addSub.paste.title.callbackUrl`, `adopt.effects.1` |
| `{{host}}` | The source's host, as entered, without scheme or path. | **Absent when the source has no entered host** `[contract]`: `base_url` is `api_key`-kind only, null there means the vendor's official endpoint, and a subscription may not carry one at all. §1.6 states what the one string that interpolates it renders instead. | `sourceDetail.summary` |
| `{{source}}` | A source's display name. | Always present | `gateway.row.current`, `gateway.row.currentTakeover`, `guard.title.editSource`, `guard.title.refetch`, `guard.title.removeModel`, `guard.title.removeSource`, `guard.title.replaceKey`, `sourceDetail.edit.title`, `upstream.repair.reauthConfirm.title` |
| `{{model}}` | A model's display id, as the source reports it. | Always present | `gateway.row.current`, `gateway.row.currentTakeover`, `guard.title.removeModel`, server-owned `models.launch.route_unconfigured` |
| `{{models}}` | Several model ids, joined by `、` / `,` — the ids that left the discovered slice on one fetch. Ids as the source reported them, never display names, because the row that carried a display name is the row that is gone. | Always present in the one key that carries it: `sourceDetail.refetch.removed` renders only when at least one id was removed, which is the same branch guarantee that keeps its `{{count}}` off zero. | `sourceDetail.refetch.removed` |
| `{{mapping}}` | The complete localized source-to-upstream-model phrase produced by `gateway.row.current` or `gateway.row.currentTakeover`. | Always present | `routeDialog.openWithMapping` |
| `{{n}}` | A hop's 1-based position in the configured order. | Always present | `guard.hop.position`, `guard.hop.position.removeSource` |
| `{{menuModel}}` | A protected **menu** model's id — from the held Route identity or `SupplyGap.model_id`. It is its own slot rather than a second use of `{{model}}` because the two name different things and the guard turns on the difference: `{{model}}` is an id a source reports, and `model-hub.md` says 「the protected identifier is always the menu model, never a hop's upstream `model_id`」. | Always present | `guard.gap.subject`, `guard.title.saveRoute`, `route.title` |
| `{{agents}}` | The enabled named Vibe Agents that pinned this menu model, by name, joined by `、` / `,` — `SupplyGap.agents` `[contract]`. | Always present in the one key that carries it: `SupplyGap.agents` 「is present and may be empty」, and an empty one renders no line at all, which is `model-hub.md`'s 「names affected Agents **when any exist**」 read as a branch rather than as an empty list. | `guard.gap.agents` |
| `{{time}}` | A relative timestamp, looking back — 3 分钟前 / 3 minutes ago. | **Absent when `last_discovered_at` is null** `[contract]` — no discovery has ever completed, so the inventory has no age to report. The one string that interpolates it is not rendered, and the status line drops that segment: 使用中 alone. A hand-populated source that has never fetched reads exactly that way; §1.6 states why it is Ready rather than an empty state. | `sourceDetail.status.listUpdated` |
| `{{delay}}` | A rough interval, looking forward — how long until the automatic retry. Same shape as `{{time}}` and the opposite direction, which is why it is its own slot: one string says a fetch happened 3 分钟前, the other says a retry comes 3 分钟后, and a single "relative timestamp" covers both while meaning neither. | Always present in the one key that carries it, because a `retry_at` still ahead is what selects that key. **A `retry_at` that has passed cannot fill it** — the interval renders zero or negative — and that is an ordinary reading, not an edge: no row in this document promotes a source on a clock, so `cooldown` can be reported with its retry time behind it for as long as the next payload takes. That reading renders `upstream.state.unavailableDue`, which is the absence rule's 「a state that cannot fill it uses a different key」 applied to a slot that may not be dropped. | `upstream.state.unavailableRetry` |
| `{{request}}` | The request that failed, as `METHOD path`. | Always present in the remaining consumer, which is selected only for a request-backed §1.9 step. Source observation exposes no request member. | `adopt.fail.detail` |
| `{{status}}` | The HTTP status that request returned. | **Absent when the §1.9 request had no HTTP response.** The separator drops with it. Source observation exposes no status member. | `adopt.fail.detail` |
| `{{health}}` | One of exactly six words — `gateway.group.status.ok`, `gateway.group.status.degraded`, `gateway.group.status.waiting`, `gateway.group.status.interrupted`, `gateway.group.status.unconfigured`, `gateway.group.status.unused`. The first four roll up `named_agents[].supply_status`; the fifth means every enabled Agent lacks either an effective model or a configured Route, from `effective_model_id` plus `route_reason`; the sixth means no enabled Agent uses this backend. Supply/configuration state, not an HTTP status: nothing here is a code, and the group renders it whether or not any request was made. | Always present | `gateway.group.subtitle.gateway` |
| `{{reason}}` | The §1.9 classified cause, from that state's closed and total copy set. No consumer interpolates an upstream string here. Source observation exposes no reason member. | Always present | `adopt.fail.detail` |
| `{{mode}}` | One of exactly two words — `gateway.group.mode.direct` or `gateway.group.mode.gateway`. The subtitle interpolates the word rather than carrying two whole strings, because the health half varies independently of it. | Always present | `gateway.group.subtitle.direct`, `gateway.group.subtitle.gateway` |
| `{{backends}}` | The backends in Hub mode that have this source configured into a route, by product name, joined by `、` / `,` — `adopted_by`'s projection, grouped and de-duplicated, never a computation over live chains (§1.0). | Always present — `upstream.state.supplying` is entered only when the list is non-empty; a source no Hub-mode backend has configured is 可用 · 当前未供给 instead | `upstream.state.supplying` |
| `{{component}}` | The gateway component's name, as the manifest names it `[contract]`. | Always present | `adopt.effects.install`, `install.effects.1` |
| `{{duration}}` | A rough install time — 约 1 分钟 / about a minute. It is an estimate stated as one, never a countdown. | Always present | `adopt.effects.install`, `install.effects.1` |

A new slot is a row here first. A key that interpolates a slot with no row is
what `scripts/check_model_hub_ui_states.py` reports as a class B gap, and so is a
row whose 「Interpolated by」 set is not exactly the set of keys that interpolate
it — in either direction, because a consumer nobody declared is a consumer whose
meaning nobody checked against the row it borrowed.

### 0.10 The state-completeness gate

**The path below is a forward reference.** This section specifies the gate; the
script it names is added by a separate change, so the file is not in the same
commit as this document. Naming it is what a specification has to do; running it
is what the other change delivers.

`scripts/check_model_hub_ui_states.py` regenerates its input from this file in the
same run it reports — it reads the live document, never a snapshot committed
beside it — and reports five gap classes, each of which is a set it computes
from the text:

| Class | What it reports |
| --- | --- |
| A | A mutating call (`POST` / `PUT` / `PATCH` / `DELETE`) named in §1 that no §0.8 row states a failure treatment for, or a §0.8 row whose failure cell is empty or names a treatment outside F1–F5 and outside this register's own states. |
| B | Rendered copy with no key, a key row missing its English column, a key cited that no copy table defines, or a `{{slot}}` with no §0.9 row. |
| C | A §0.8 row with no exit, or a frame section that draws an element inventory and contributes no §0.8 row. |
| D | A copy key whose leaf names a condition — error, empty, failure, undetermined, unavailable — that no §0.8 row cites. Under 约束四 that is either copy for an unreachable error, or a state this document forgot to write. |
| E | A claim this document makes about the system — a route, a schema field, an enum value, a repo symbol — that the file with authority over that claim does not make. |

Every class asks one question of a different inventory: does this citation name
exactly one thing that exists? So the script holds **one** comparison, and each
class names the inventories it reads through it. It matches whole tokens rather
than substrings, reports a citation that resolves to nothing rather than passing
it, and reports one name defined twice. No class compares names on its own,
because five classes each inventing that comparison is five chances to invent it
wrong — which is how nine of these gaps were reported as separate bugs.

It reports the **input scale** it measured over before it reports any finding,
and it fails loudly rather than passing when an extraction comes back empty,
because a checker that silently runs on nothing reports green.

What it does not claim: that this document is complete, that the copy is right,
or that the states match the product. It claims those five sets are empty. The
claim is worth exactly the coverage of its extractors and no more, which is why
the scale is printed with the verdict rather than in a comment.

---

---

## 1. Per-frame specification

### 1.0 Shared shell

Seven original full-page frames render the same chrome, 06 renders a drill-in variant
whose header left is a bare back icon, and 04/05/10 render it behind a scrim. Frames 11
and 12 reuse 06 and 01 respectively as exhibit backgrounds; the 04 paste-back crop and
frame 13 are component exhibits. Specifying the shell once is not a shortcut: a shell
copied into every section is one that will drift in all but one of them.

**The tabs belong to the overview shell, while the source detail replaces them with
Back.** Usage and switch history both outlive the current Source inventory, so every
overview landing keeps the three-tab strip even when the Sources & gateway body is
frame 09's direct-only state. Frames 09 and 10 predate this section navigation and
remain authoritative for their bodies, not for whether the shell exposes its other
sections. §1.8 states the direct-only body condition.

**Geometry** `[frame]`

| Element | Metric |
| --- | --- |
| Frame | 1440×1100, `fill: $--background` |
| Sidebar (`ref`) | 240 wide, existing shared component — **reference it, never rebuild it** |
| `Main` | 1200×1100, vertical, `gap: 22`, `padding: [36,40]` |
| Content track | 1120 wide (1200 − 2×40) |
| `header` | 1120×38, `space_between`, `align: center` |
| `title` | 26 / 700 / Inter, `$--foreground` |
| `tabs` | 1120×39, `gap: 4`, `border-bottom: 1 $--border` |
| tab | `padding: [10,14]`, `gap: 7`; label 13 / 600 |
| active tab | `border-bottom: 2 $--mint` |
| `body` | 1120 wide, height follows its content; the enclosing settings route pane is the sole page scroll owner |
| `cols` | 1120 wide, natural height, `gap: 16` → upstream 384 + rail 72 + gateway 632 |
| Module card | `$--surface`, `border 1 $--border`, `radius 14` |
| Legend row | 1120×34, `gap: 18`, `space_between`, swatch 20×2, label 11 / 500 `$--muted` |

**Element inventory**

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| `title` + info icon | Page name | static | icon: hover, focus **and** activation `[derived]` | Tooltip: `shell.modelsInfo.body`, which is the icon's accessible description; `shell.modelsInfo.label` is its accessible name |
| Run pill | Engine liveness | `runtime-dependency.schema.json` → `status.health` `[contract]` | see the mapping below | see the mapping below |
| Tabs ×3 | Section switch | — | yes | 来源与网关 / 用量 / 日志; the active one gets the mint underline |
| Upstream module | Source inventory + info icon | `GET /api/models/sources` `[spec]` | info icon and rows: yes | Tooltip: `upstream.info`; open 06 for that source |
| Wire corridor | Routing space between Sources and Agents; it draws no independent axis | derived, decorative | no | — |
| Gateway module | One group per backend, each with model rows + info icon | per-backend supply + chains `[spec]` | info icon, rows, collapse, 「默认路由」, mode switch | Tooltip: `gateway.info`; open 02 / expand / open 03 for **that backend** / open 10's confirm |
| Legend | Colour → meaning | static; kept in bijection with the inks the page draws | no | — |

**The Logs tab owns the switch-history feed** `[derived]`. It is absent from the
Sources & gateway body and is read only when Logs is opened, so event history cannot
delay the routing surface. Each activation refreshes the head through
`GET /api/models/events?limit=20`; the card shows three rows initially, 查看全部 expands
the held rows and follows the `before` cursor until exhausted, and 收起 returns to three.
An unread feed keeps the card and its retry action distinct from the authoritative
暂无切换记录 empty state. The removed 高级 placeholder has no surviving surface or copy:
request logging and diagnostics must return as real capabilities, not as a dead row.

**The info icons are controls, and their strings are this file's** `[derived]`. Hover is not
an affordance a keyboard or a touch user has, and each tooltip explains the title beside it
— so every icon is a focusable button, activating it toggles the
tooltip, Escape dismisses it, `shell.modelsInfo.label` is its accessible name and
`shell.modelsInfo.body` its accessible description. The string is registered here
because no frame carries it, which is the one way this icon differs from §1.1's legend
icon: that note is measured off the frame (§0.2), so that row states the trigger and
this one states the trigger and the text. An explanation reachable only by hover is
reachable by neither of the two inputs most likely to need it, and an unregistered
string is one a developer has to invent at the keyboard — which is how a hardcoded
sentence in one locale ships.

**Every icon-only control is named, and one that repeats a labelled control beside it
takes that control's key** `[derived]`. The icon above is one instance and §1.6's `iGcAi`
back icon is the other; both register a string because a glyph is not a name. The three
overlays are the shape that had been missed: 03's `fUvS9`, 04's head close and 10's head
close were each specified as a 15px glyph and a colour, with the name left to whoever
built them. They do exactly what the 取消 in the same foot does — 04 says so outright,
that 取消, the close icon and a press outside 「are one action, not three」 — so each
takes its own surface's cancel key as its accessible name: `order.cancel`,
`addSub.cancel`, `adopt.cancel`. That is the whole rule, and it is worth stating once
here rather than three times below, because the alternative is not one new key but three,
all of them saying 取消 in two locales. Reuse also keeps them from drifting: a dialog
cannot rename its button and leave its icon behind, since there is one string. What it
does **not** extend to is an icon whose action has no twin — §1.6's back icon names its
destination and gets its own key, which is the case this rule is the complement of.

**States** — §0.8, rows marked §1.0. Every other frame inherits them, and the rows
that belong to a frame alone are marked with that frame instead.

Ready and Runtime off are drawn. Every other state above is **not drawn** `[derived]`.
Required behaviour:

- Empty: upstream module keeps its head and footer and shows one line —
  「还没有来源。先添加一个订阅或 API Key。」 The gateway module shows its backend
  groups with 「没有可用来源」 per group rather than vanishing; a backend that
  exists is a fact independent of whether anything can supply it.
- **Not installed**: the pill reads `shell.notInstalled`; the adjacent off switch is the
  activation target — for installation, not for start `[derived]`. The pill carries the same idle styling
  as Not started, for the same reason: a missing optional component is not a fault. It
  must never offer 点击启动, because starting is not the action that resolves it. The
  runtime contract enumerates `not_installed` alongside `not_started`
  (`runtime-dependency.schema.json`, `health`) `[contract]`, so a UI that collapses the
  two renders a start button that cannot succeed and reports the failure as if the
  engine had crashed. Activating the switch opens an install confirm that names the
  component and its rough duration before anything is downloaded, exactly as D-26
  requires — but it is the **non-switching variant**, and the difference is not
  cosmetic `[derived]`.

  | | From a backend's 切换到网关 (D-26) | From the runtime switch |
  | --- | --- | --- |
  | Title | 把 {{backend}} 切换到网关 | 安装网关组件 |
  | What it promises | install, start, and move that one backend to the gateway | install and start the component; **no backend changes mode** |
  | Bullets | `models.hub.adopt.effects.*` — consequences for `{{backend}}` | `models.hub.install.effects.*` — the component is installed, the gateway starts, and every backend stays where it is |
  | Primary | 安装并切换 | 安装并启动 |
  | Where it lands | that backend on the gateway | the same page, run pill in Starting then healthy |

  Reusing D-26's confirm here would be underspecified and then wrong: the runtime switch is a
  page-level control with **no backend in hand**, so an implementation would have to
  invent one to fill `{{backend}}` in the title and the four `effects.*` bullets, and
  whichever it picked would silently switch a backend the user never named. The user
  turned the gateway on; the confirm may promise installation and nothing else. Both variants
  share the component name, the duration and the download-nothing-before-consent rule —
  which is the part D-26 exists to keep from diverging — and differ on exactly the
  consequence each entry point actually has.
- **The initiating sequence, not the frame, owns post-install continuation** `[derived]`.
  Before the install request, the non-switching runtime-switch confirm holds
  `install_and_start`; §1.9's three-step confirm holds `install_start_switch`. These are
  local operation intents, never a wire field or a second runtime state. H1 is their one
  continuation register: while either sequence remains held, a status read settling at
  `not_started` proves installation and immediately advances to Starting; a later live
  reading releases `install_and_start` after ordinary page dispatch, but advances
  `install_start_switch` to the mode `PATCH` and holds it until M5 accepts commit
  evidence. An install-only status read or a reload with no held initiating sequence
  lands at Not started instead. Reload therefore recovers the server-owned install
  without inventing a client promise it no longer holds, and runtime health never stands
  in for one.
- **Installing**: the confirm does not close on acceptance. Its primary becomes an
  in-place progress state — `install.progress`, spinner, both buttons inert — because a
  dialog that closes on 安装并启动 hands the user back a page whose pill still reads
  未安装, which is the one reading that means *nothing happened* `[derived]`. Dismissal
  is unavailable while it runs, for the reason D-15 gives from the other side: the way
  out of this state is the operation finishing, and a 取消 that cannot actually stop a
  download in flight would be a button that lies. What it costs to say so is one
  sentence; what it costs to leave unsaid is a user who presses 取消, sees the dialog
  close, and has no idea whether a binary is landing on their disk.
  Every mounted surface displaying a durable `installing` reading owns the 2s
  `GET /api/models/runtime/status` loop registered above. This dialog is one such
  surface; a page remounted by reload is another even though it restores no initiating
  sequence. Unlike the borrowed `BackendOAuthPanel` cadence, installation has no client
  timeout: each `installing` reading continues the loop, a read failure keeps the
  progress and cause under F2, and unmount ends the owner.
- **Install failed**: the dialog stays open, the message is replaced, and the primary
  becomes 重试 `[derived]` — the same shape §1.4's failure rows take, and for the same
  reason. The authoritative failure reading is `health: not_installed` with the closed
  `status.error_key`; no raw downloader or verifier text renders. A new install request
  clears that key before it enters `installing`, while dismissal preserves the failed
  projection until a later status says otherwise `[contract]`.
- **Re-entry while an install is in flight is server-owned** `[contract]`. The install
  route durably enters `health: installing`; reload and concurrent repeats read or return
  the same state and start no second job. Service bootstrap verifies or restarts an
  orphaned job before serving runtime endpoints, so the client neither fabricates a local
  progress flag nor resumes a download itself.
- **Unsupported host**: `manifest.assets` is per platform, and the README states that
  unsupported hosts **fail closed** with 直连 as the escape hatch `[contract]`. So on a
  platform with no pinned asset the pill reads `shell.unsupported`, carries the idle
  treatment, and has **no** activation target. This is the one place the pill's mapping
  splits a `health` value on a second field, exactly as frame 06's bar splits `active`
  on adoption: offering 点击安装 here would be D-9a's dead control with a download URL
  behind it — a confirm that names a component, promises a duration, and then cannot
  find a file to fetch.
- **That split reads the Avibe host, never the browser** `[contract]`. Every runtime
  response carries server-derived `host_platform`; support is exactly an equal
  `manifest.assets[].platform`. A browser platform is a different subject and is never a
  fallback. Unsupported `not_installed` therefore renders the inert escape immediately,
  while a supported host renders the install affordance.
- **Not started**: the pill reads `shell.notStarted`; the adjacent off switch is the
  page's start affordance. The pill is styled as an *idle* pill — `$--muted` label on `#FFFFFF0A`,
  **not** the error treatment `[derived]`. The runtime contract classes
  `not_started` as lazy-start idleness rather than an alarm `[contract]`, and a
  page that paints idleness red teaches users to ignore the colour that matters.
  The closed page-level gate replaces every internal column; no `—` placeholder is
  rendered for configuration that is intentionally unavailable while the runtime is off.
- **Starting**: the pill reads `shell.starting` with the `loader-circle` spinner and
  the switch stops accepting activation, so a second click cannot queue a second start
  `[derived]`.
- **Down**: the run pill flips to `shell.stopped` with the error treatment, because an
  engine that *was* running and stopped answering is a fault. The closed page-level gate
  replaces the internal configuration and its body explains that the previous start did
  not become ready. Recovery offers the same switch action as Not started.
- **Status unread**: a first-paint status transport failure uses `shell.unread`, hides the
  internal configuration and disables the switch. A retained live F2 snapshot may remain
  visible, but its switch is disabled until the next authoritative status read.
- **Impaired**: the pill reads `shell.degraded` with the error treatment and **no**
  activation target `[derived]`. The engine is answering, so the page keeps the data it
  has and no column falls back to `—`; on a first paint the data it has may be none, and
  a read that failed there renders Sources unread or Partial **underneath this pill**
  rather than instead of it (D-34) — F2 keeps a last good result and a first paint has
  no such result to keep, so a dispatch that stopped here would leave the failed region
  with no line and no 重试. But it is reporting a fault about itself, and a pill that
  read 网关运行中 would claim a health the payload explicitly denies (D-3). There is no
  activation because no route repairs it — starting an already-running engine is not the
  fix, and offering it would be the dead control D-9a exists to prevent.
- **Partial**: only the sub-tree that failed degrades. A failed supply payload must
  not blank the source inventory, which loaded fine. **This is reachable from the first
  paint, not only from a loaded page**, and saying so is the whole of this clause: the two
  page-level payloads are separate requests, so the supply read can fail on the very first
  one while the source list succeeds — the exact condition this row defines — and a
  first-paint dispatch offering only Closed would replace the whole internal surface with
  a runtime switch, for an engine that answered. It also discards the inventory that did load, so
  the repair costs the user a fetch that already succeeded. A partial payload is partial
  whether it is the first or the fiftieth.
- **Sources unread**: the mirror of Partial, and it needs its own row for the reason
  Partial does. A failed source list used to arrive at Closed, which is the sentence
  for an engine that is not running: it renders the closed gate and offers the runtime
  switch. None of that is true here — the status read answered, the
  supply read answered, and one list did not — so the pill keeps whatever `health` said,
  no other region degrades, and the repair is to ask for the list again. Reading an
  outage off the failure of one read is the same mistake in the other direction as
  reading health off a stale value: both state a fact the payload did not carry.

**The runtime control is a total rendering of `health`, and the states above are how it
gets there** `[contract]`. The pill names the state; the adjacent switch owns activation.
Every value `runtime-dependency.schema.json` admits is rendered, and no state is inferred
from an empty relationship array. One value splits on a second field — the same shape
frame 06's bar has when it splits `active` on adoption:

| `RuntimeDependency.status.health` `[contract]` | State | Pill | Treatment | Activation |
| --- | --- | --- | --- | --- |
| `ok` | Ready | `shell.running` | idle | on switch; stop only when every Agent backend is Direct (`POST /api/models/runtime/stop` `[contract]`) |
| `degraded` | Impaired | `shell.degraded` | error | on switch; same guarded stop rule |
| `down` | Closed | `shell.stopped` | error | off switch; start (`POST /api/models/runtime/start` `[contract]`) |
| `not_started` | Closed | `shell.notStarted` | idle | off switch; start |
| `installing` | Installing | `install.progress` | idle / busy | none; status reads own progress |
| `not_installed`, `error_key` non-null `[contract]` | Install failed | `install.fail.title` / `settings.models.install.fail.detail` | error | retry `POST /api/models/runtime/install` |
| `not_installed`, `error_key: null`, exact asset for `host_platform` `[contract]` | Not installed | `shell.notInstalled` | idle | the non-switching install confirm — **never** the start route |
| `not_installed`, `error_key: null`, no exact asset for `host_platform` `[contract]` | Unsupported host | `shell.unsupported` | idle | none — 直连 (§1.8) is the escape hatch |

The runtime is a page-level gate. Only `ok` and `degraded` expose the tabs, Sources,
Agent gateway-model rows, route controls, supply graph and internal dialogs. Every other
authoritative health renders a closed/setup surface instead. Turning the gateway off is
not a bulk routing mutation: while any backend remains in Hub mode, the switch is disabled
and names those backends; the server independently rejects the same race with
`runtime_in_use`. Configuration is preserved while hidden and reappears unchanged after a
successful start.

Starting is the one pill with no `health` behind it: it is the client's own optimistic
state between accepting the press and the next payload. A transport failure — the status
request not returning at all — renders Status unread and disables the switch because the
page has no authoritative action basis. And `degraded` here is the *engine* speaking about
itself, unrelated to `supply_status: degraded` above, which is a backend speaking about
its arbitration; the two hold independently and D-24's rule applies to both words.

**Shared copy** — namespace `models.hub.shell.*` / `.upstream.*` / `.gateway.*` /
`.legend.*`. Both `ui/src/i18n/zh.json` and `en.json` must carry every key. The
property is a set equality over the two files' key sets, checked by diffing them —
never by comparing two counts, which agree in exactly the cases that matter least.

**Count-bearing keys** `[derived]`. Every key interpolating `{{count}}` ships as an
i18next plural family — `<key>_one` and `<key>_other` — in **both** locale files,
never as a single bare key. Two consequences worth stating, because getting either
wrong is invisible until a user hits `count = 1`:

- English needs the distinction (`1 source`, not `1 sources`), and the rule has to
  hold at `0`, `1` and `2` alike. A bare key cannot.
- Chinese has no plural categories, so `zh` never selects `_one`. It still carries
  both variants, with identical values, so that locale parity stays a plain set
  equality. A parity rule with a per-language exemption list is a parity rule that
  stops catching anything.

A plural family is well-formed at `count = 0` and can still be **false** there, which is
the failure this rule does not catch on its own. `_other` renders 「0 个来源」 without
complaint; whether that sentence is worth showing is a question about the surface, not
about the string. So each count-bearing key needs one of the same two shapes the slot
rule asks for below — a **branch guarantee** that its surface is unreachable at zero, or
a **zero-case string** that says the true thing instead — and the section that renders
the key is where that shape is chosen, since only that section knows what zero means
there. §1.8 takes the guarantee for `shell.allDirect` (no installed backend, no pill),
§1.6 takes the second shape for `sourceDetail.summary` (an empty inventory gets
`empty` / `emptyNeverFetched`, not 「0 个型号」), §1.5 the second shape for
`addKey.pull.result` (a pull that reached the source and found nothing gets
`addKey.pull.empty`, not 「拉到 0 个型号」).

The count-bearing keys in this file are `shell.allDirect`, `upstream.count`,
`gateway.modelCount`, `gateway.moreModels`, `addKey.pull.result`, `guard.count`,
`sourceDetail.summary`, `sourceDetail.refetch.removed` and `takeover.pill` — nine, all
under `models.hub.*`;
each appears below in its `_one` / `_other` form. This list is one side of a set equality
— the keys interpolating `{{count}}` and the keys shipping plural families are the same
set — so adding a `{{count}}` key anywhere under `models.hub.*` without adding it here
breaks that equality. §0.9's `{{count}}` row is the other side, and the two are checked
against each other mechanically rather than read: a sentence four hundred lines from the
table it restates is a sentence nobody re-reads when the table changes, which is how this
one came to name `chain.derived.hops` — retired with the chain-derived line — while
leaving out `guard.count`, added when the guard dialog got its plural family.

**Slot-bearing keys** `[derived]`. A `{{slot}}` is a promise that a value exists. Where
the value behind a slot is a contract field the schema types nullable, the key needs one
of exactly two shapes, and this file has to say which one it took:

- a **branch guarantee** — the string renders only in a state where the schema pins the
  field non-null; or
- a **second key** for the null case, because the sentence does not survive losing the
  slot.

What a slot may not have is a single total-sounding string and an unwritten hope that
the field is populated. `upstream.state.unavailableRetry` is the branch-guarantee shape
and is worth reading as the model: `state.retry_at` is nullable, but
`source.schema.json` pins it non-null on `cooldown` and null on the two healthy states
`[contract]`, and that string renders only in cooldown, so `{{time}}` cannot come up
empty where it is drawn. The two keys that needed the other shape both live on frame
06 — `sourceDetail.status.listUpdated` and `sourceDetail.summary` — and §1.6 resolves
them there, next to the fields they read.

The rule is worth stating separately from the count rule because the failure is
quieter. A missing plural form reads wrong to everyone at `count = 1`; a slot with
nothing behind it renders `型号列表更新于 undefined` to whichever users happen to own a
source in the state nobody pictured, and looks perfect in every screenshot taken by
someone whose sources all work.

**State-bearing surfaces** `[derived]`. Every state this file names owes three answers,
not one: what puts the user **in** it, what takes them **out**, and — for any state that
can outlive the surface that started it — what they find on **coming back**. The shared
state machine above is written in that shape deliberately, and the obligation binds the
per-frame flows in §1.1–§1.9 just as hard where they are prose rather than a table. A
state with an entry and no exit is a trap. A state with both and no re-entry answer is
the defect nobody reproduces, because reproducing it means leaving and returning while
something is still in flight — which is what users do and what a walkthrough does not.
Two shapes satisfy the third answer: state what the user finds, or make the state
unable to outlive its surface, which is the cheaper answer wherever it is available
(§1.4 takes it).

| Key | 中文 | English |
| --- | --- | --- |
| `shell.title` | 模型 | Models |
| `shell.running` | 网关运行中 | Gateway running |
| `shell.stopped` `[derived]` | 网关已关闭 | Gateway off |
| `shell.degraded` `[derived]` | 网关降级运行 | Gateway running degraded |
| `shell.notStarted` `[derived]` | 网关已关闭 | Gateway off |
| `shell.notInstalled` `[derived]` | 网关组件未安装 | Gateway component not installed |
| `shell.allDirect_one` `[frame]` | {{count}} 个后端都在直连 | The only backend is direct |
| `shell.allDirect_other` `[frame]` | {{count}} 个后端都在直连 | All {{count}} backends are direct |
| `shell.starting` `[derived]` | 正在启动… | Starting… |
| `shell.stopping` `[derived]` | 正在关闭… | Stopping… |
| `shell.unsupported` `[derived]` | 这个平台还没有网关组件 | No gateway component for this platform yet |
| `shell.toggle.turnOn` `[derived]` | 开启模型网关 | Turn model gateway on |
| `shell.toggle.turnOff` `[derived]` | 关闭模型网关 | Turn model gateway off |
| `shell.toggle.stopBlocked` `[derived]` | 请先将 {{names}} 切换为直连，再关闭模型网关 | Switch {{names}} to Direct before turning the model gateway off |
| `shell.toggle.stopUnavailable` `[derived]` | 暂时无法读取 Agent 路由状态，当前不能关闭模型网关 | Agent routing status is unavailable; the model gateway cannot be turned off yet |
| `shell.closed.off.title` `[frame]` | 模型网关已关闭 | Model gateway is off |
| `shell.closed.off.body` `[frame]` | 开启后才会显示和编辑来源、路由与网关模型。 | Turn it on to view and edit sources, routes, and gateway models. |
| `shell.closed.down.title` `[derived]` | 模型网关已关闭 | Model gateway is off |
| `shell.closed.down.body` `[derived]` | 上一次启动没有就绪。开启网关即可重试。 | The previous start did not become ready. Turn it on to try again. |
| `shell.closed.notInstalled.title` `[derived]` | 模型网关尚未安装 | Model gateway is not installed |
| `shell.closed.notInstalled.body` `[derived]` | 开启后会安装由 Avibe 管理的网关组件。 | Turn it on to install the managed gateway component. |
| `shell.closed.unsupported.title` `[derived]` | 模型网关不可用 | Model gateway is unavailable |
| `shell.closed.unsupported.body` `[derived]` | 当前平台没有兼容的托管网关组件。 | This platform does not have a compatible managed gateway component. |
| `shell.closed.installing.title` `[derived]` | 正在安装模型网关 | Installing model gateway |
| `shell.closed.installing.body` `[derived]` | 安装并启动完成后才会显示来源与路由。 | Sources and routing will appear after installation and startup complete. |
| `shell.closed.starting.title` `[derived]` | 正在启动模型网关 | Starting model gateway |
| `shell.closed.starting.body` `[derived]` | 网关就绪后才会显示来源与路由。 | Sources and routing will appear when the gateway is ready. |
| `shell.closed.stopping.title` `[derived]` | 正在关闭模型网关 | Turning model gateway off |
| `shell.closed.stopping.body` `[derived]` | 运行时停止期间会隐藏网关配置。 | Gateway configuration is hidden while the runtime stops. |
| `shell.closed.unread.title` `[derived]` | 暂时无法读取网关状态 | Model gateway status unavailable |
| `shell.closed.unread.body` `[derived]` | 确认运行状态前不会显示网关配置。 | Configuration stays hidden until the runtime state can be confirmed. |
| `shell.modelsInfo.label` `[derived]` | 什么是模型 | What a model is |
| `shell.modelsInfo.body` `[derived]` | 模型是 Agent 使用的型号。你可以查看每个型号现在能否运行、由谁供给,需要时为单个型号指定路由。 | A model is what an Agent uses to generate a response. You can see whether each model can run, which source supplies it, and set a route for one when needed. |
| `install.title` `[derived]` | 安装网关组件 | Install the gateway component |
| `install.subtitle` `[derived]` | 只安装组件,后端保持现在的方式 | Installs the component only; the backends keep working the way they do now |
| `install.section.effects` `[derived]` | 会发生什么 | What will happen |
| `install.effects.1` `[derived]` | 下载并安装 {{component}},约 {{duration}} | {{component}} is downloaded and installed, about {{duration}} |
| `install.effects.2` `[derived]` | 装好后网关自动启动 | The gateway starts automatically once it is installed |
| `install.effects.3` `[derived]` | 没有后端会被切换,型号菜单不变 | No backend is switched and the model menu does not change |
| `install.confirm` `[derived]` | 安装并启动 | Install and start |
| `install.cancel` `[derived]` | 取消 | Cancel |
| `install.progress` `[derived]` | 正在安装… | Installing… |
| `install.fail.title` `[derived]` | 安装没有完成 | The install did not finish |
| `install.retry` `[derived]` | 重试 | Try again |
| `shell.tab.hub` | 来源与网关 | Sources & gateway |
| `shell.tab.usage` | 用量 | Usage |
| `shell.tab.logs` | 日志 | Logs |
| `recent.title` | 最近切换 | Recent switches |
| `recent.viewAll` | 查看全部 | View all |
| `recent.collapse` | 收起 | Collapse |
| `recent.loadMore` | 加载更早的记录 | Load older entries |
| `recent.loadingMore` | 加载中… | Loading… |
| `recent.today` | 今天 | Today |
| `recent.yesterday` | 昨天 | Yesterday |
| `recent.empty` | 暂无切换记录 | No switches yet |
| `recent.deletedSource` | 已删除 | deleted |
| `upstream.heading` | 上游来源 | Upstream sources |
| `upstream.infoLabel` `[derived]` | 什么是上游来源 | What upstream sources are |
| `upstream.info` `[derived]` | 上游来源是账号或 API Key。网关从这里获取型号,再按路由供给 Agent。 | An upstream source is an account or API key. The gateway gets models from it and supplies them to Agents through routes. |
| `upstream.count_one` | {{count}} 个 | {{count}} source |
| `upstream.count_other` | {{count}} 个 | {{count}} sources |
| `upstream.group.native` | 本机原生 | Native · on this machine |
| `upstream.group.hub` | 接入网关 | Connected to gateway |
| `upstream.kind.nativeCredential` | 原生 · 本机凭据 | Native · local credential |
| `upstream.kind.subscription` | 订阅 | Subscription |
| `upstream.kind.apiKey` | API Key | API key |
| `upstream.state.supplyingNative` `[spec]` | 正在供给 {{backend}}(原生) | Supplying {{backend}} (native) |
| `upstream.state.supplying` `[spec]` | 正在供给 {{backends}} | Supplying {{backends}} |
| `upstream.state.standby` | 可用 · 当前未供给 | Available · not currently supplying |
| `upstream.state.unavailableRetry` | 暂不可用 · {{delay}} 后自动重试 | Unavailable · retrying automatically after {{delay}} |
| `upstream.state.unavailableDue` `[derived]` | 暂不可用 · 已到重试时间 | Unavailable · the retry is due |
| `upstream.state.supplyStopped` `[frame]` | · 已停止供给 | · stopped supplying |
| `upstream.empty` `[derived]` | 还没有来源。先添加一个订阅或 API Key。 | No sources yet. Add a subscription or an API key first. |
| `upstream.unread` `[derived]` | 来源列表没读到 · 网关本身正常 | Could not read the source list · the gateway itself is fine |
| `upstream.retry` `[derived]` | 重试 | Retry |
| `upstream.addSubscription` | 添加订阅 | Add subscription |
| `upstream.addApiKey` | 添加 API Key | Add API key |
| `upstream.repair.reauthorize` `[frame]` | 重新授权 | Reauthorize |
| `upstream.repair.reauthorizeToRefresh` `[derived]` `[contract]` | 重新登录以刷新订阅状态 | Sign in again to refresh subscription status |
| `upstream.repair.replaceKey` `[frame]` | 更换 Key | Replace key |
| `upstream.repair.topUp` `[derived]` | 补充额度 | Add credits |
| `upstream.repair.contactVendor` `[derived]` | 联系厂商 | Contact vendor |
| `upstream.repair.contactProvider` `[derived]` | 联系你的服务商 | Contact your provider |
| `upstream.repair.reauthConfirm.title` `[derived]` `[contract]` | 重新授权 {{source}} | Reauthorize {{source}} |
| `upstream.repair.reauthConfirm.detail.onFailure` `[derived]` `[contract]` | 只有这次登录失败时,这个来源才会需要处理,直到你重新登录;取消不会替换现有登录。 | Only if this sign-in fails will the source need attention until you sign in again; cancelling does not replace the current sign-in. |
| `upstream.repair.reauthConfirm.detail.immediate` `[derived]` `[contract]` | 开始后旧的本机登录立即失效,共用它的每个来源都会不可用。完成后只恢复这一个,其它来源需要各自重新登录。 | The old local sign-in stops working immediately, and every source sharing it becomes unavailable. Finishing restores only this source; the others each need their own sign-in. |
| `upstream.repair.reauthConfirm.confirm` `[derived]` `[contract]` | 继续登录 | Continue to sign in |
| `upstream.repair.reauthConfirm.cancel` `[derived]` | 取消 | Cancel |
| `upstream.repair.reauthorizing` `[derived]` | 正在重新授权… | Reauthorizing… |
| `upstream.repair.replacingKey` `[derived]` | 正在更换 Key… | Replacing key… |
| `upstream.repair.impact.title` `[derived]` `[contract]` | 凭据已更新,部分供给受到影响 | The credential was updated, and some supply was affected |
| `upstream.repair.impact.detail` `[derived]` `[contract]` | 以下路由或型号已被移除或中断。 | The following routes or models were removed or interrupted. |
| `upstream.repair.impact.refreshFail` `[derived]` | 凭据已更新,但模型页面暂时无法刷新。 | The credential was updated, but the model surface could not be refreshed. |
| `upstream.repair.impact.done` `[derived]` | 完成 | Done |
| `upstream.repair.unresolved` `[derived]` | 仍然不可用 | Still not working |
| `upstream.repair.fail` `[derived]` | 没能完成这次凭据修复 | The credential repair did not finish |
| `gateway.heading` | 网关路由 | Gateway routes |
| `gateway.infoLabel` `[derived]` | 什么是网关路由 | What gateway routes are |
| `gateway.info` `[derived]` | 网关路由决定每个 Agent 的型号在已有路由中的来源里先使用哪个上游来源;当额度不足、触发限流、服务端、认证或网络出错导致请求无法完成时，自动切换下一优先级。 | Gateway routes decide which upstream source each Agent's model uses first among the sources already configured in its route. When quota, rate-limit, server, authentication, or network failures prevent a request, the next priority is used automatically. |
| `gateway.sourceOrder` | 默认路由 | Default routing |
| `gateway.manageModels` `[derived]` | 管理模型 | Manage models |
| `gateway.switchToGateway` | 切换到网关 | Switch to gateway |
| `gateway.switchToDirect` | 切到直连 | Switch to direct |
| `gateway.modelCount_one` | {{count}} 个型号 | {{count}} model |
| `gateway.modelCount_other` | {{count}} 个型号 | {{count}} models |
| `gateway.selectedModelCount_one` `[derived]` | 已选 {{count}} 个模型 | {{count}} selected |
| `gateway.selectedModelCount_other` `[derived]` | 已选 {{count}} 个模型 | {{count}} selected |
| `gateway.group.subtitle.direct` `[frame]` | {{mode}} | {{mode}} |
| `gateway.group.subtitle.gateway` `[frame]` | {{mode}} · {{health}} | {{mode}} · {{health}} |
| `gateway.group.mode.direct` | 直连 | Direct |
| `gateway.group.mode.gateway` | 网关 | Gateway |
| `gateway.group.status.ok` `[contract]` | 正常 | Healthy |
| `gateway.group.status.degraded` `[contract]` | 降级 | Degraded |
| `gateway.group.status.waiting` `[contract]` | 供给暂不可用 | Supply unavailable for now |
| `gateway.group.status.interrupted` `[contract]` | 无可用来源 | No source is available |
| `gateway.group.status.unconfigured` `[derived]` | 未配置型号路由 | No model route configured |
| `gateway.group.status.unused` `[derived]` | 暂无 Agent 使用 | No Agent uses this backend |
| `gateway.group.takenOver` | 接管中 | Taken over |
| `gateway.supply.none` `[derived]` | 没有可用来源 | No usable source |
| `gateway.supply.unread` `[derived]` | 后端供给情况没读到 · 网关本身正常 | Could not read this backend's supply · the gateway itself is fine |
| `gateway.fail.switchToDirect` `[derived]` | 没能切回直连 | The switch back to direct did not go through |
| `gateway.retry` `[derived]` | 重试 | Retry |
| `gateway.group.emptySelection` `[derived]` | 尚未选择模型 | No models selected |
| `gateway.group.emptyModels` `[derived]` | 这个后端没有可用型号 | This backend has no models |
| `gateway.menu.title` `[derived]` | OpenCode 模型菜单 | OpenCode model menu |
| `gateway.menu.description` `[derived]` | 选择 OpenCode 可通过网关使用的模型；保存后，在模型行中配置各自路由。 | Choose which models OpenCode can use through the gateway, then configure each selected model's route from its row. |
| `gateway.menu.search` `[derived]` | 搜索模型或供应商 | Search models or sources |
| `gateway.menu.selected` `[derived]` | 已选 {{selected}} / {{total}} | {{selected}} of {{total}} selected |
| `gateway.menu.empty` `[derived]` | 没有可供 OpenCode 选择的模型 | No eligible source models are available |
| `gateway.menu.configured` `[derived]` | 已有配置模型 | Existing configured model |
| `gateway.menu.baselineUnavailable` `[derived]` | 模型菜单数据暂未更新，请等待刷新完成，或重试读取失败的区域后再编辑。 | Model menu data is not current. Wait for refresh, or retry the failed section before editing. |
| `gateway.menu.noMatch` `[derived]` | 没有匹配的模型 | No models match this search |
| `gateway.menu.save` `[derived]` | 保存选择 | Save selection |
| `gateway.menu.saving` `[derived]` | 正在保存… | Saving… |
| `gateway.menu.cancel` `[derived]` | 取消 | Cancel |
| `gateway.menu.saveFailed` `[derived]` | 模型选择未保存，请重试 | The model selection was not saved |
| `gateway.row.current` | {{source}} → {{model}} | {{source}} → {{model}} |
| `gateway.row.currentTakeover` | {{source}} → {{model}}（已自动切换） | {{source}} → {{model}} (Taken over) |
| `models.launch.route_unconfigured` `[contract]` | 模型 {{model}} 尚未配置路由。请前往 Models 配置。 | Model {{model}} has no configured route. Open Models to configure one. |
| `gateway.moreModels_one` | 还有 {{count}} 个型号 | {{count}} more model |
| `gateway.moreModels_other` | 还有 {{count}} 个型号 | {{count}} more models |
| `gateway.collapse` | 收起 | Collapse |
| `legend.native` `[frame]` | 原生 | Native |
| `legend.viaGateway` | 网关供给 | Gateway supply |
| `legend.connectedUnused` | 已启用 · 当前未被使用 | Enabled · not currently used |
| `legend.takeover` | 接管中 · 临时改走 | Taken over · temporarily rerouted |
| `legend.unavailable` | 供给已暂停 | Supply paused |

**The mode word is read off `AgentSupply.mode`; the health word is the closed aggregate
over `AgentSupply.named_agents`, and neither ever stands in for the other** `[contract]`.
The backend-level `supply_status` describes only the model named by
`selected_by_agent`; it becomes null whenever the global default Agent belongs to another
backend and therefore cannot truthfully describe this group. The header never reads that
field. Direct mode renders the mode alone. Gateway mode evaluates every enabled named
Agent on this backend:

| `AgentSupply.mode` `[contract]` | `named_agents[]` projection `[contract]` | Subtitle | Key |
| --- | --- | --- | --- |
| `direct` | not read — Direct arbitrates nothing, so it rolls nothing up | 直连 | `gateway.group.subtitle.direct` + `gateway.group.mode.direct` |
| `hub` | empty `named_agents` | 网关 · 暂无 Agent 使用 | `gateway.group.subtitle.gateway` + `gateway.group.status.unused` |
| `hub` | every member has `effective_model_id: null` or `route_reason: route_unconfigured` | 网关 · 未配置型号路由 | `gateway.group.subtitle.gateway` + `gateway.group.status.unconfigured` |
| `hub` | every member is `ok` | 网关 · 正常 | `gateway.group.subtitle.gateway` + `gateway.group.status.ok` |
| `hub` | at least one member is `ok` / `degraded`, but not every member is `ok` | 网关 · 降级 | `gateway.group.subtitle.gateway` + `gateway.group.status.degraded` |
| `hub` | no member is `ok` / `degraded`, and at least one is `waiting` | 网关 · 供给暂不可用 | `gateway.group.subtitle.gateway` + `gateway.group.status.waiting` |
| `hub` | the configuration-gap predicate above did not match, and every remaining `supply_status` is `interrupted` / `null` | 网关 · 无可用来源 | `gateway.group.subtitle.gateway` + `gateway.group.status.interrupted` |

**All four `[contract]` words are transcribed** `[contract]`.
正常 / 降级 / 无可用来源 remain `model-hub.md` §4.5's verbatim labels. The frozen
`waiting` label uses one umbrella statement for both Source cooldown and live
connection-backoff chains, while per-Source rows retain their distinct causes. It states
only present availability and promises neither a recovery time nor a recovery outcome.

`gateway.supply.none` is not a seventh word, because it is not the same grain `[derived]`.
The six above are the enabled-Agent aggregate rendered into the group head's status line.
`gateway.supply.none` is a body line under a group that has no source at all — a statement
about that backend's inventory, not a reading of enabled-Agent supply, which
is why it appears in neither the `{{health}}` slot's closed set (§0.9) nor the table
above. Its 没有可用来源 sitting one character from `interrupted`'s 无可用来源 is the near
miss that grain distinction has to survive: the status word answers 「这个后端现在供得上
吗」 from a field, the body line answers 「这里有没有东西可供」 from a count.

**A count is something you can only have by having looked** `[derived]`, which is why
§1.0's *Partial* renders `gateway.supply.unread` and not that body line. The state is the
per-backend supply read failing, and it used to render 没有可用来源 on the reasoning that
F2 keeps the last rendering — but F2 keeps a rendering only where there is one, and on a
first paint there is none. What the user saw was a verdict produced by a request that
never answered, pointing at the one repair the evidence does not support: add or change a
source, for a supply nobody has counted. So the state splits by whether there is anything
to keep — unread copy and 重试 on a first paint, the rollups already drawn on any later
one — which is the same split §1.0's *Sources unread* makes one read over, and the reason
those two rows are mirrors rather than one row with two moods.

**This document renders one group aggregate and no named-Agent row** `[frame]`.
`AgentSupply.named_agents[].supply_status` remains each named Agent's own rollup for its
own explicit model, while `effective_model_id` and `route_reason` expose a missing model
or Route before Source health is considered. The group consumes every member through the table above, but it does
not print an individual Agent name or assign the aggregate back to one Agent. Per-Agent
detail remains a separate surface that needs its own frame. The top-level
`selected_model_id` / `supply_status` pair remains useful for describing the next default
turn, but it is deliberately outside this group-header projection.

The four non-null values differ in *what a person can do about it*, which is why
collapsing them loses the only thing the word is for: `ok` is serving from the intended
head; `degraded` is serving through a fallback or past blocked members, so requests
still succeed; `waiting` is every member in a cooldown that is not yet retry-ready, so
nothing is being served but the process is fine and time alone fixes it; `interrupted` is
nothing being served for a reason waiting cannot resolve.
A UI that renders only 正常/降级 tells a user in `waiting` to go fix something and a user
in `interrupted` to wait.

`interrupted` covers six readings, not one, and the contract names every one of them: a
capability chain with no members at all, or nothing runnable with at least one hop held
by a non-self-healing blocker — `needs_action`, `error`, `source_missing`,
`model_unsupported`, or a native CLI unavailable in this process at any source health
`[contract]`. The word is the same for all of them because what it tells the user is the
same, that time alone will not fix this; the *repair* is not. §0.8 files them as **five**
rows rather than six because `needs_action` and `error` share one repair and one exit,
and every other reading has its own. Reading the value as CLI-only is the specific error
this list exists to prevent: it sends a user whose credential expired, whose source is
gone, or who never placed one, to check a CLI that is running fine.

**接管中 is not one of these values, and must not be rendered from this field.**
Takeover is a projection of the chain — the current hop is not the first hop, and the
first hop is unavailable for a *recoverable quota/cooldown or live connection-backoff*
reason (AC-30, D-21, C-5). 接管中 promises exactly that self-healing class. A head blocked by anything else is also
being served past — `needs_action`, `error`, a native CLI unavailable in this process, a
hop whose source or model is gone — and none of those clears on a clock, so every one of
them is **degraded without takeover** — §1.1's *Serving past a blocked head*, which is
written as that negation — and drawing the violet reroute on any of them promises a
recovery that will never arrive. A chain whose
hops are all exhausted has no runnable hop and therefore **no takeover**, and must draw
no takeover badge, connector colour or other takeover visual semantics. The two facts
can co-occur (a taken-over backend usually reads `degraded`) and are computed from
different inputs, so a surface that derives one from the other is right by accident until
the first `degraded`-without-reroute payload.

**A source's supply line renders `adopted_by`, not the chain** `[contract]`.
`upstream.state.supplying*` names which Hub-mode backends have this source
**configured into a route** — the same reading §1.6's 使用中 gets one surface over, and
for the same reason. *Stable* is the operative word in the field's own definition: the
array is unchanged by a cooldown, a revoked credential, or a takeover that routes past
this hop entirely, so a line that read it as live traffic would be false in precisely the
states a user opens this page to understand. Two things keep the rendered word honest
anyway. The line is selected by the source's **state** first — a cooling or blocked source
shows its state word instead, which is what §1.7's card delta *is* — so it appears only on
a healthy source; and what it then claims is configuration, not flow. The live question is
the chain read's (§1.2), and D-28 is the rule that keeps the two projections from standing
in for each other. Every `GET /api/models/sources` row carries the server-derived,
complete `adopted_by: [{backend, menu_model}]` projection. It is unique and sorted by
backend then menu model; the line groups it by backend and de-duplicates, so it carries
no position and no menu-model detail. Creation responses echo the same projection at top
level. This section owns that reading; §1.1 and §1.7 render it and name no source of
their own. The chain is the other projection — the stored `hops` array, read by §1.2 and
§1.3 — and D-28 is why neither may be computed from the other.

**The legend is a rendered-relation index, not a fixed asset** `[frame]` `[derived]`.
01 draws three keys, 08 draws five; the two extra keys in 08 are exactly the two
relations 08 adds (a takeover, and a source whose supply is paused). So a key renders
**iff** the page currently draws at least one element in that relation — the legend can
never explain an ink that is not on screen, and can never omit one that is. The equality
holds in both directions.

**Semantic ink** `[frame]` — five inks. Meaning is assigned **per element role**, and
the three roles below are pairwise disjoint, so every inked element has exactly one
reading:

- **Relation / status ink** — the element states a fact about where tokens come
  from: wires, rails, tint washes, status text, supply pills, legend swatches.
- **Control ink** — the element states that a control is active, selected, or
  primary: tab underline, order badges, selected option, input focus ring,
  manual-row wash, primary button.
- **Advisory ink** — the element states a consequence the reader should weigh
  before acting, and is not itself actionable: 04's ToS note, 05's state-④ and state-⑤
  result strips. It is neither a relation (it asserts nothing about where tokens come
  from) nor a control (nothing happens when you press it). The buttons those strips
  argue about are ordinary chrome — 05 ⑤'s 仍要添加 is an outline button with a
  `$--foreground` label, not an amber one, because inking the escape hatch in the
  strip's colour would make advisory ink actionable and put a hole in this partition
  for the sake of one element.

The third role exists because two roles were not enough and the gap was load-bearing,
not cosmetic: with only relation and control, the ToS note and the state-④ strip fit
neither side of a relation-versus-control partition, so any item quantifying over that
partition was undefined on exactly the two elements the frames require. A partition
with a hole is not a partition.

| Ink | As relation / status ink | As control ink | As advisory ink | Where |
| --- | --- | --- | --- | --- |
| `$--cyan` `#3FE0E5` | **原生 — this supply comes from a `native_cli` hop** | **never** | **never** | wire `@1.75`, card tint `#3FE0E50A` / border `#3FE0E54D`, tile `#3FE0E51A`, status text |
| `$--mint` `#5BFFA0` | gateway supply | active / selected / primary | success emphasis | relation: wire `@1.75`, rail (`#5BFFA01A` chip / `#5BFFA033` line), supply text. control: active tab underline `@2`, order badges, selected option card, tier-editor focus ring, manual-row wash `#5BFFA00D`, primary buttons. advisory: 05's success note (`#5BFFA014` / `#5BFFA040`) |
| `$--violet` `#7C5BFF` | **taken over — this supply is temporarily rerouted** | **never** | **never** | 08 only: wire `AEaxi` `@1.75`, module pill and group text `$--violet`, per-model current-source text `#7C5BFFCC`, legend swatch `#7C5BFF` |
| `$--gold` `#FFC857` | **supply paused — this source is temporarily unavailable** | **never** | warning emphasis | relation: 08 only — wire `gtjOy` `@1`, upstream card border `#FFC85733`, state text, legend swatch `#FFC857`. advisory: 05 state ④ strip and state ⑤ strip (`#FFC85714` / `#FFC85759`), 04 ToS note (`#FFC8571A` / `#FFC8574D`) |
| `#FF6B6B` | **needs action — this source cannot serve until a person acts** | repair / destructive action | error emphasis | advisory: 05 state ③ strip (`#FF6B6B14` / `#FF6B6B40`). relation/status: frame 12 source-card dot and text. control: frame 12 repair actions and frame 11 destructive action |
| `#FFFFFF26` | connected but not currently used | — | — | dim wire `@1` only |

**Violet and gold were previously one row, and merging them was a real error, not a
simplification** `[frame]`. Measured in 08: the takeover wire `AEaxi` is `#7C5BFF`
`@1.75` and the paused-supply wire `gtjOy` is `#FFC857` `@1`. The legend says the same
thing in its own swatches — `LmQFp` `#7C5BFF` labels 「接管中 · 临时改走」 and `oopTe`
`#FFC857` labels 「暂不可用 · 供给已暂停」 (the drawn string; the copy register now
specifies `legend.unavailable` as the shorter 「供给已暂停」, and §1.7 records why). The two
facts are opposite in valence: violet
says a request still succeeded by another path, gold says a path stopped carrying
requests. Inking both the same colour would tell the user that the thing that saved the
request and the thing that broke it look alike. Earlier revisions of this table said
「gold = takeover」; the frames say otherwise and §0.2 makes the frame right.

**Rose is the fifth ink, and it was missing from this table while a frame was already
drawing it.** 05's state ③ strip is `#FF6B6B14` / `#FF6B6B40` `[frame]` — measured, and
recorded in §1.5's metrics since the rebuild — yet the table above listed four inks, so
the role partition was undefined on a drawn element. That is the same "partition with a
hole" this section rejects one paragraph earlier, and it is worth naming rather than
quietly patching: this table drifted because it was written once and then not
re-derived, while §1.4 and §1.5 were measured again. Rose's **relation/status** meaning
is partly `[frame]`: frame 12 now draws `needs_action` at card grain, while no frame
draws an `error` source or a rose wire. D-21 assigns both states this ink, and §1.1 and
§1.6 admit them as states, so the row states the shared meaning without pretending the
new card exhibit also supplies every relation rendering the first time an implementation
has to draw one. Rose controls are narrow: they repair the rose state or confirm a
destructive mutation, and never indicate selection or ordinary forward progress.

The stroke widths carry the same fact and are not decoration: `gtjOy` is the *only*
relation wire drawn at `@1` besides the dim never-used wire, and in 01 that same
ChatGPT→Codex relation is mint `@1.75`. A paused relation thins toward the never-used
wire; a rerouted one keeps full weight because it is still carrying traffic.

Two asymmetries are deliberate and load-bearing:

- **Cyan is exclusive in both roles.** It never inks a control, so cyan anywhere on
  the page means exactly one thing: **this supply comes from a `native_cli` hop**. That
  is the single most consequential distinction on the surface — see D-6 and D-21.
  Note the noun: cyan says 原生, a property of the hop, and says nothing about 直连,
  which is a property of the backend and renders only as the subtitle's mode word (E-4).
  A page can draw a cyan wire into a backend whose mode is 网关 — that is a native source
  named by a hop in a gateway chain — and the two words must stay separable for that row
  to be readable at all.
- **Mint is dual, and that is fine, because the roles never collide on one
  element.** A tab underline is not claiming an upstream relation, and a wire is not
  claiming to be a control. Forcing mint down to one meaning would need a second
  accent hue for controls, which buys nothing and costs the brand a token.

The honest statement of the rule is therefore a partition, not a whitelist: mint
inking a relation/status element **must** mean gateway supply, and mint inking a
control element **must** mean active/selected/primary.

**Identity ink is a fourth role, and it is why hue alone never decides meaning**
`[frame]`. Each backend group header carries a 30×30 tile in a per-backend constant:
Claude Code `#3FE0E51A`, Codex `#5BFFA01A`, OpenCode `#7C5BFF24` (D-20). The tile
names *who the backend is*; it asserts nothing about where that backend's tokens come
from. OpenCode proves the role is real and not a rationalisation: in 01 its tile is
violet while its supply wire `vkveX` is mint, so on one row the same page draws a
violet element and a mint element about the same backend and means two unrelated
things by them. Read violet as relation ink there and the page says OpenCode is being
taken over, which it is not.

Identity ink is decidable from form factor without knowing the semantics: a filled
30×30 rounded tile containing a glyph, alpha `1A`–`24`. Relation ink is a stroke, a
5px dot, a status word, or a 20×2 legend swatch, at full alpha or `CC`. **No
acceptance item may put an identity tile in a relation domain**, and every item that
quantifies over a hue states this exclusion by citing this paragraph rather than
restating it.

One consequence is worth naming because a fixture depends on it. Claude Code's tile is
cyan *and* Claude Code is native-direct in every frame that draws it, so no frame
separates the two readings for cyan the way OpenCode separates them for violet. That
D-20's tile stays cyan after Claude Code moves to the gateway is therefore an assertion
of D-20, not an observation of any frame `[derived]`. An all-gateway fixture is the
first artefact that will pin it, and the cyan rule above is written so that fixture can
pass.

---

### 1.1 Frame 01 `pWQfB` — Overview (upstream + gateway)

**The question it answers:** *where do my tokens come from, and who is using
which one right now?* Read left to right it is one sentence: these sources exist →
dispatch arbitrates → each backend's models resolve to these sources today.

**Element inventory** (deltas from §1.0)

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| `heujA` upstream card | tile icon by kind, name on its own line, interface and kind pills on the next line, mono detail line, one status line | one source | yes (whole card) | Open 06 for that source |
| `uf3re` detail | account label, or `host/path · masked key` — **and every field it can draw is nullable, so the line is specified by omission, exactly as §0.9 and §1.6 rule the same hole** `[contract]`. `account_label`, `base_url` and `masked_credential` are each `["string","null"]` in `source.schema.json`, so a segment with no value is dropped, never rendered empty and never left behind a dangling `·`; `base_url: null` is the vendor's official endpoint (§0.9's `{{host}}`) and is not synthesized into a hostname (§1.6). When nothing is left the whole line is omitted rather than filled — a subscription that reports no account label is the common case, and the card still identifies itself from four required fields (icon and pill by `kind`, name by `display_name`, state by `state`). Repeating the kind pill or the card's own name here would be the only alternative, and it would say nothing the card has not already said | source | no | — |
| `YcOFo` status | which Hub-mode backends have this source configured into a route — `adopted_by` (§1.0) | the complete server-derived Source projection, never a computation over chains (D-28) | no | — |
| `wmROQ` / `Xitl7` footer buttons | Add subscription / Add API key | — | yes | 添加订阅 opens frame 13; its selected vendor opens 04. 添加 API Key opens 05 |
| `f8w6Xp` + `pnYa0` wire corridor | the relations cross between the columns without an independent axis | decorative | no | — |
| `GLylJ` backend group | backend tile, name, model count, head buttons, and one `{{mode}} · {{health}}` line. For an open menu the count is the explicit selected count, never the number of models available from Sources | per-backend mode + supply health | head: buttons only | OpenCode's **Manage models** opens its model-menu dialog; other actions are registered below |
| `ehGRK` / `bGsC7` 「默认路由」 | — | — | yes | Open 03 **for that backend** |
| OpenCode 「管理模型」 `[derived]` | — | `menu.checked` plus the server-owned eligible Source inventory | yes | Open the OpenCode model-menu dialog; the same action remains available in the empty row area |
| `IyKyp` 「切换到网关」 | — | backend in 直连 | yes | Open the 10 confirm for that backend |
| `z02Ep` / `gbrq2` 「切到直连」 | — | backend on the gateway | yes | That backend leaves the gateway immediately — **no confirm** (D-30) |
| `Exx0a` model row | model id (mono 12), a chain chip, current-source text; `legend.unavailable` occupies that text slot when the page-grain row has no runnable hop | `AgentSupply.model_supply[].has_runnable_hop`; per-model AgentChain and its `current` member | yes | Open 02 for `(backend, model)` |
| `ZM1pm` collapse row | `还有 N 个型号` | count of hidden rows | yes | Expand in place |
| `FZUYI` wire layer | one path per supply relation + endpoint dots; no central axis | derived supply set | Source and Agent cards highlight their connected paths on hover or focus | — |
| `ftWgW` legend info icon | the legend's note — **the string is measured from the frame, not specified here** (§0.2) | static | hover, focus **and** activation — the same three §1.0's title icon carries | Tooltip, the note standing as the icon's accessible description |

**The 当前 lines are a third read, and the page does not wait on it** `[contract]`. Every
element above it is drawn from the two page-level payloads — tile, name, count, the mode
and status line, the head buttons, the collapse row — but the serving hop is in neither:
`api.md` states that AgentSupply projects no backend-level serving head; `model_supply`
carries row-grain configuration/runnability facts but no current hop. Overview reads every
row's existing AgentChain shape through one `GET /api/models/agents/<backend>/chains`
per Hub backend — a 直连
backend gets the documented `direct_mode` refusal. So 「Ready」 is defined on the two
payloads on purpose: waiting on the third would hold the whole page for one row's
projection, and on a 直连 group it would wait for a read the contract refuses. That
refusal is also why a 直连 group draws no 当前 line and no takeover rather than an empty
one — there is nothing there to be pending about.

**When a collection member answers, `current` names the serving hop exactly** `[contract]`.
`agent-chain.schema.json` requires either null or an exact member of `chain`; the page
renders that member and never substitutes the first runnable hop. Takeover is true only
when `current` is not the chain head and that head is unavailable for a recoverable
quota/cooldown or live connection-backoff reason. A null `current` or a blocked head with any non-recoverable reason has
no takeover visual. Every later chain payload re-evaluates that complete predicate under
§0.8's shared rule; there is no next-turn latch. If the head is now runnable while
`current` still names the successor, that successor is rendered as the ordinary serving
hop without takeover ink or copy.

**Until that collection member answers, only facts the page payload already proved may replace `—`**
`[derived]`. Chain and takeover render `—` while the collection is outstanding, and
again when it fails, is refused or omits the member. Current source does too when
`has_runnable_hop` is true. When that page-grain field is false, however, the existing
current-text slot immediately renders a page-owned statement: `chain_length: 0` first
selects server-owned `models.launch.route_unconfigured`, while a nonempty chain selects
`legend.unavailable` in `$--gold`. The page has already proved which of those two facts
holds even though it cannot yet name a current hop or detailed cause. This reuses the
  row's measured 10.5px text slot and adds no geometry. The unresolved rendering follows
  D-3 — a surface that cannot prove a fact must say so. 「Chain unresolved」 is a row-grain
  state of its own precisely so that this one cannot be written as a transition into §1.0
  Closed, which would replace the entire internal surface with the runtime gate over a
  request about one model. A stale last-known hop is specifically excluded. AC-30 makes
  takeover a projection of the chain the surface
displays, so a takeover badge drawn from a chain no longer in hand is a projection of
nothing — the one failure that rule exists to prevent. And a failed chain read degrades
those columns and nothing else, which is §1.0's Partial rule read at row grain: only the
sub-tree that failed degrades, and the group keeps everything the other two payloads
drew.

**A read that can fail has to be re-issuable, and the collapse row is what re-issues
this one** `[derived]` D-35. 「Chain unresolved」 is the only failure state on this page
whose repair is not a control drawn beside it: the three columns render `—`, the frame
carries no per-row 重试, and a row left there would be waiting on a request nothing was
going to send again. Collapsing a group and expanding it re-reads that backend's collection
once, which costs no new control and reads as what it already means. The page's own two triggers sit
beside it — any mutation re-renders the group, and the next load re-reads everything —
so the user-available repair and the ambient ones agree. What this row must not do is
resolve on a clock: a poll cadence is a number this file has no basis to pick, and 「no
exit keys on elapsed time」 is the rule the two source rows above are written to.

**The three head buttons are mutually constrained** `[frame]`, and the constraint is
the whole model in one line: a backend is either on the gateway or not. On the gateway
it carries 默认路由 + 切到直连; in 直连 it carries 切换到网关 and **nothing else** —
Claude Code's head has no order button, because a direct backend consults no source
order and an editor there would edit a list nothing reads. D-9a states the rule, and it
is a set equality over the three groups.

**Track metrics** `[frame]` `[derived]`: `cols` is 1120 wide with `gap 16`; upstream
module is 384 wide, rail is 72, and the gateway module takes the rest. The frame's
806px column is an instance, not a height cap: both modules grow with their content,
the legend follows the taller module, and the enclosing settings route pane owns the
only page scroll. This file records the three track widths and never a derived row
width — a literal there is a number that goes stale the first time a track moves.

**Card and row metrics** `[frame]`: upstream card `fill_container`×96 minimum, auto height,
`padding [8,12]`, `gap 10`, `radius 10`; tile 34×34 `radius 9`; name 12.5/700 Inter
on its own line, interface and kind pills on the next line; detail 10.5 JetBrains Mono
`#9BA3B8CC`; status dot 5px + text 10.5/600. Backend group `$--background` fill,
`radius 12`, `$--border`; head 66 minimum with auto growth when its action row wraps,
desktop `padding [4,14]` and mobile `padding [12,14]`, `gap 7`, bottom border, backend name 14/700 Inter, tile 30×30 `radius 9`, count pill
`padding [3,8]` `radius 999`; head button `padding [9,12]` `radius 8` fill
`#FFFFFF0A` stroke `$--border-strong`, label 11.5/700; head status line dot 5px +
11/600 + 13px info `#FFFFFF40`. `rows` container `padding 8`, `gap 8`; model row 36
tall, `padding [0,12]`, `gap 10`, `radius 8`, fill `#FFFFFF05`, stroke `$--border`;
model id 12/500 JetBrains Mono; mode chip `padding [3,8]` `radius 999` fill
`#FFFFFF0A` stroke `$--border`, label 10.5/600 `$--muted`; current text 10.5 Inter
`#9BA3B8CC`; chevron 15px `#FFFFFF40`. Collapse row 24 tall with **transparent fill
and transparent stroke** — it is a row-shaped affordance, not a card. Legend 1120×34,
`gap 18`; swatch 20×2 (the dim key 20×1, see D-23), label 11/500 `$--muted`; note 11
`#FFFFFF4D` + 13px info `#FFFFFF8C`.

**States** — §0.8, rows marked §1.1.

Needs action is the one worth stating precisely `[derived]`: a
`needs_action` source **stays in the list, in place**, with its status line
replaced by the cause `state.detail_key` names `[contract]`. It is not removed, not moved to the bottom, and not
silently dropped from the chains that name it — a source you cannot see is a
source you cannot fix. Frame 12 closes that loop at the card: a repairable credential
cause adds exactly one action, 重新授权 or 更换 Key, while the card itself remains the
way into 06. §1.11 owns those controls so the cause vocabulary here and on 06 stays one
vocabulary rather than a second set of card-only status strings.

**A backend with zero model rows is a different emptiness from a backend with no usable
source, and an open menu adds one more distinction** `[derived]`. 没有可用来源 says *this
backend has selected models and nothing can serve them* — the fix is a source or route.
「这个后端没有可用型号」 is reserved for a fixed backend whose catalog is genuinely empty.
OpenCode with `menu.checked == []` instead says 「尚未选择模型」: the eligible Source
inventory may be full, but none of it belongs to OpenCode's explicit menu until the user
chooses it. A persisted Route left behind after a model is unchecked is dormant: it does
not keep a row visible or trigger a chain read, and becomes visible with its saved mapping
if that model is selected again. The group keeps its header and its `<mode> · <status>`
line in every branch; only the row area differs.

**The OpenCode empty branch offers the exact fix in place** `[derived]`. **Manage models**
is present both in the group head and beside 「尚未选择模型」. It opens a dialog populated
only from the server-owned eligible Source inventory, deduplicated by the contract's
OpenCode identifier. Saving performs `PUT /api/models/agents/opencode/menu`; a selected
model with no Route then appears as an ordinary row whose current-text slot reads
`gateway.group.status.unconfigured`, and that row opens frame 02. The dialog preserves
the saved menu view, blocks dismissal while its write is outstanding, and retains the
draft after a failed save. The dialog owns a dedicated `Source[]` → `AgentSupply` →
`Source[]` read bracket rather than composing the page's independently settled regions.
Search, selection, and save become available only when the complete Source projections on
both sides are identical and the Agent projection has no source-composition hole. Save sends
that observed menu as `baseline` beside the user's `menu` draft; the server derives additions,
removals, and any view change, then applies that intent to the latest stored menu under the
mutation lock. Concurrent unrelated selections therefore survive without another client-side
pre-write read. Existing checked identifiers remain visible and selected even when their Route
maps to a differently named upstream model. A failed or undecodable `PUT` is reconciled with
another dedicated bracket before the dialog reports failure or enables another edit; the write
is accepted when every requested addition is present and every requested removal is absent,
regardless of unrelated concurrent selections. An unread or non-convergent bracket preserves
the draft, shows `gateway.menu.baselineUnavailable`, and cannot issue the menu `PUT`; stale or
absent data is never interpreted as an empty selection.

**Extreme data**

Collapse predicate for a backend group `[frame]` for the shape, `[owner decision
2026-08-23]` for the six-row limit:

```
LIMIT = 6

# 0. ORDER — one total order over the whole group, computed before anything is hidden
key(m)    = backendMenuIndex(m)             # the backend's own menu order, and only that
sorted    = sort(models, by=key)

# 1. SELECT — a fixed prefix, which never reorders the backend menu
visible   = take(sorted, LIMIT)
collapsed = drop(sorted, LIMIT)

render collapse row  iff  |collapsed| > 0
collapse label count = |collapsed|
```

`model_supply.has_runnable_hop` still owns the row marker: `chain_length` first
partitions a false value into structural `models.launch.route_unconfigured` at zero and
`legend.unavailable` for a nonempty chain. Which hop is current and why another is
unavailable belong to the third read (「Chain unresolved」 above), and **neither consumer
may manufacture those details.** That read is per backend, asynchronous, and allowed to
fail, so visibility must not consume it. A row whose collection member is outstanding, missing, failed
or refused keeps exactly the position and visibility the backend menu gave it. Expanding
reveals any paused or unconfigured row beyond the first six without changing its
classification.

`backendMenuIndex` is unique within one backend's menu, so no two models tie and `sorted`
is one determinate sequence. Expanding removes the prefix limit instead of re-deriving an
order: rows the user could already see keep their positions, and the revealed rows appear
where they always belonged.

Consequences, each a test fixture:

| `models` | visible | collapse row |
| --- | --- | --- |
| 12 | first 6 | 「还有 6 个型号」 |
| 7 | first 6 | 「还有 1 个型号」 |
| 6 | all 6 | none |
| 2 | all 2 | none |

- The count in 「还有 N 个型号」 is `|collapsed|`.
- `|models| <= LIMIT` ⇒ **no collapse row at all**, not an empty one.
- Expanding is idempotent and does not re-rank.
- The collapsed view never exceeds six model rows, regardless of row state.

Other limits `[derived]`:

| Data | Rule |
| --- | --- |
| Long source name | Single line, ellipsis at the card's inner width — `upstream module 384 − border 2 − upContent padding 24 − card padding 24 − tile 34 − gap 10 = 290` at the frame's track width, and derived from the live track everywhere else. `title` attribute carries the full value. |
| Long base URL / masked key | Mono line truncates **from the middle**, keeping scheme+host and the last 4 key chars — the two ends are what identifies it. |
| Long model id | Mono, ellipsis at the `a` column; full value in `title`. |
| Many sources (> 6) | The upstream module grows with every source; the enclosing settings route pane scrolls, so the head, groups and footer remain in one reading flow. |
| Many backends (> 3) | The gateway module grows with every backend; the enclosing settings route pane scrolls and no nested gateway scrollbar appears. |
| Zero supply relations | The wire layer renders nothing — no placeholder path. |
| Wires | Generated from the supply-relation set, never hand-placed; the frame's four paths are an instance of that generator, not a fixed asset. Every path leaves its Source at the shared right-edge midpoint, and every relation targeting the same Agent lands at that Agent card's shared left-edge midpoint while remaining a distinct curve. One neutral marker represents each shared Source or Agent anchor, so relation ordering cannot hide or recolor either endpoint. Paths rest at low opacity; hovering or focusing a Source or Agent restores full opacity only for its connected paths. The corridor draws no independent central axis. The SVG follows the natural-height `cols` track and remains clipped before the legend and following page sections. |

---

### 1.2 Frame 02 `Q1dkS` — Route-chain editor

**The question it answers:** *for this one model, which sources will be tried, and in
what order?*

**Sparse manual intent replaces the historical stored-chain-only model.** The
server's effective projection owns Automatic, Manual, Passthrough or Unconfigured;
there is no `follow` / `custom` discriminator or client-side recommendation engine.
Inherited routes enter editing through Edit route. Manual arrays execute as saved;
Restore automatic stages null, previews the effective plan, and removes the saved
override only on Save. The routing revision above owns the approved current frames.

**The current export remains the visual authority; this section registers its behaviour.**
The manual-edit branch contains one ordered list of exact hops, a grip and ordinal on each row,
the Source display name and exact upstream model id, a per-row remove action, 添加一跳,
按来源顺序重排, the persisted-configuration hint, 取消 and 保存 `[frame]`. The title holds
the menu model, the subtitle holds the backend product name, and the opening model row is
the focus owner. Error, refusal and committed-report bodies replace the normal body in the
same modal; they do not create another Route editor `[derived]`.

**Hop-projection precedence is total** `[contract]` `[derived]`. The exact chain hop is
classified before any display join. A stale page-held `Source[]` row can still match a
Source that the later dialog-owned chain read marks missing, so join presence is never a
substitute for `AgentChain.chain[*].reason`:

| Priority | Authoritative chain classification | Page-held `Source[]` consumption | Rendered identity and action disposition |
| --- | --- | --- | --- |
| HP-1 | `reason: source_missing` | Do not consume a matching stale row and do not let a join override this classification | Render localized `route.sourceMissing`, then ` · ` and the exact raw `source_id` in mono. Keep the exact upstream `model_id` on line two; the persisted row remains removable and reorderable |
| HP-2 | The hop has no `source_missing` annotation and the exact `source_id` joins to a live/page-held Source row | The Source projection may supply only that row's display name | Render the joined display name and exact upstream `model_id`; all draft actions follow V5 |
| HP-3 | The hop has no `source_missing` annotation but the page-held projection has no exact match | Do not invent a Source name, relabel the chain reason or start another refresh | Render the exact raw `source_id` in mono and the exact upstream `model_id`. The persisted pair retains the same remove/reorder affordances as HP-1 |

Only HP-2 consumes Source display metadata. Every row remains identifiable and actionable,
while `route.sourceMissing` means the chain annotation and never the local join result.

**One held identity, one origin and one wire projection** `[contract]` `[derived]`.
Opening 02 holds `(backend, menu_model)` and reads
`GET /api/models/agents/<backend>/chain?model=<id>`. Hold `manual_override` separately
from the effective `chain`. Edit route initializes a manual draft from effective exact
pairs; Restore automatic stages null. Live `channel`, `health`, `runnable`, `reason`,
`retry_at`, `current` and `supply_state` remain read annotations and are never echoed
into a write. The reversible origin includes saved intent as well as pairs, so equal
arrays cannot settle a lost response with different manual intent. Local edits operate
on a copy; Cancel never writes. Removing the last hop stages inherited null and previews
the same plan as Restore automatic. Empty input is accepted at the API boundary but
never persists as a manual override. Preview reports normalized draft intent and cannot
replace saved authority. Undo restores the prior unsaved nonempty draft.

V5 is the sole Add-hop and draft validator. 添加一跳 opens the product's standard anchored
selection surface without assigning new frame geometry: candidates come from the current
AgentSupply eligibility plus Source inventory evidence, are grouped by Source, name the exact
upstream model id, and exclude duplicate pairs. Eligible API-key Sources additionally
accept a canonical typed upstream id absent from inventory; subscription candidates
retain known-model admission. Typed ids never create inventory or capability metadata.
The selector owns exactly one active
candidate, which is also its selected pair: ET-5a selects the first listed candidate as the
surface opens, and every ET-5d move atomically replaces both values. Which element holds DOM
focus is the standard surface's business — the filter field owns the caret and carries the
active candidate as its active descendant — so "active" here names the one owner the
confirmation reads, never a focus location. Pointer activation
first makes that exact candidate active/selected; the separate confirmation is enabled and
derived only from this one owner. Confirming appends that exact pair and focuses its grip
through ET-5c. The surface also carries the standard filter field, which narrows what is
listed and never what is legal: the active candidate is always one of the candidates
currently listed, so a filter — or a draft change — that drops it re-elects the first listed
one rather than leaving the confirmation pointing at a row nobody can see. A filter that
excludes everything renders `route.add.noMatch`, which is a statement about the typed term
and never about supply; `route.add.none` remains the only claim that no pair is available.
Identity mapping and an explicit cross-model mapping are
equally legal; the UI never invents a substitute model. If no valid pair remains, the
action is disabled with `route.add.none`. Persisted pairs that later became stale stay in
the list and may be reordered or removed, because the contract validates only new or
changed pairs. ET-17e alone owns the state edge when refreshed Hub authority invalidates a
new or changed row; this paragraph supplies the predicate, not a parallel destination.

**按来源顺序重排 is a local sort of this manual draft** `[spec]`
`[derived]` `[contract]`. `model-hub.md` §4.2/§4.6 and `api.md` Per-backend source order
make `sources.order` the shared automatic planner's default membership/order. The
per-model PUT persists the explicit `hops` without sorting them; an editor may use
its page-held default-order projection as an explicit local draft-sorting aid. The
control applies a stable total key to the current draft only: a hop whose
Source is in the backend's page-held current Source order sorts by `(0, source_order_index,
original_index)`; an unlisted Source sorts by `(1, original_index, original_index)`.
Membership, exact model mappings and within-Source relative order are preserved. The
result remains local until 保存. This editor reads an already page-held display projection
under an explicit user gesture and produces only that one local draft: it never reruns
placement or matching. The per-model write body remains only explicit `hops`; Source order
is neither sent nor interpreted by that route. This control never calls
`PUT /api/models/agents/<backend>/sources`, because that route changes backend
defaults. Frame 03 owns that backend-scoped action (G-13); this
control only sorts the one route draft held by frame 02.

**Save sequencing is total** `[contract]` `[derived]`:

| Step | Evidence held | Request / disposition |
| --- | --- | --- |
| RS-1 — local edit | opening AgentChain intent + V5-valid nonempty ordered draft or inherited null | No mutation. Last-hop removal enters Restore preview, preserving the prior unsaved draft for Undo. Save compares normalized intent as well as pairs: nonempty equal-to-automatic saves remain manual. Empty Manual is not a saveable state; inherited Save uses DELETE. Cancel never writes |
| RS-2 — first save | one immutable submitted manual override or restored null | Send the complete manual array with PUT, or DELETE for restored null, without `force` or plan echoes; transfer focus/ownership to Saving |
| RS-3 — ordinary success | R6 `{chain, removed_hops, interrupted}` | Hold all three members; cardinality is evidence consumed by ET-10/ET-11, which alone select the report or report-free M6 edge |
| RS-4 — guarded refusal | Existing HTTP 409 guard envelope and both complete refusal arrays | Nonempty manual PUT guards protected-supply interruption; inherited DELETE Restore guards actual effective removal or supply loss. Render the applicable existing guard and evidence blocks. The refusal persists nothing and retains draft intent |
| RS-5 — confirm | immutable submitted intent + both exact arrays from the currently displayed refusal | Re-send the same PUT or DELETE with `force: true`, `would_remove_hops` and `would_interrupt`; confirmation never changes the submitted intent |
| RS-6 — plan changed | a forced request returns another guarded 409 | ET-12 replaces both displayed arrays with the recomputed plan and requires another explicit confirmation; the old preview is discarded |
| RS-7 — impact disappeared | the forced request recomputes an empty guarded plan | The server returns ordinary R6 success; ET-10/ET-11 consume the actual envelope and the UI fabricates no refusal |
| RS-8 — shaped rejection | a non-guard, non-`direct_mode` error response | Settle that attempt terminal-rejected. Retry revalidates retained intent, freezes a new generation and sends one ordinary non-forced PUT or DELETE as appropriate. It never runs D-36 or reuses prior force/plan evidence |
| RS-9 — no response / no Route authority | transport no-answer or `direct_mode` without an exact Route result | ET-15/ET-18a set attempt settlement `unknown`. D-36 may read only: matching proves commit; nonmatching or failed observation never proves the old write terminal and never emits a recovery mutation. The user may abandon; any later edit begins a distinct workflow from newly read authority |

**Direct-mode evidence retires the editor; only `AgentSupply` projects the destination
page** `[contract]` `[derived]`. This is an evidence register, not a second transition
machine; the ET-18a family and the LF/AS dispatcher alone own cleanup, destination, focus and
any next read:

| ID | Evidence producer | What the evidence proves | Landing payload available here | Edge owner |
| --- | --- | --- | --- | --- |
| DM-1 | Opening `GET /api/models/agents/<backend>/chain?model=<id>` returns `direct_mode` | The Route surface is illegal at this observation point | None — the standard error envelope carries no `AgentSupply` | ET-18a starts/transfers the mounted page's existing Agents read |
| DM-2 | Route `PUT` or `DELETE` returns `direct_mode` | The editor is now illegal; no exact Route authority exists, so RO sets attempt settlement `unknown` for the submitted normalized intent even though the response is shaped | None — the error may not label the row or page Direct | ET-18a starts/transfers the same Agents read and changes only ownership/legality around that retained unknown attempt |
| DM-3 | D-36 or another post-failure chain read returns `direct_mode` | Exact Route observation is temporarily illegal; this changes RO legality to Direct-suspended and does **not** settle the held attempt | None — no second chain or Source read can manufacture one | ET-18a starts/transfers the same Agents read; RO retains every other axis until a producer-independent Hub post-Source hook makes observation legal |
| DM-4 | ET-16a's first reconciliation read returns the exact backend `AgentSupply` with `mode: direct` | Direct is the authoritative current backend mode, so no Route read or V5 validation is legal now; mode still proves neither Route commit nor non-commit | The returned AgentSupply row itself; it is installed exactly and no second Agents read is sent | AS-1 consumes this payload; RO derives attempt settlement only from held Route evidence, never from this mode producer |

For DM-1–DM-3 the handoff enters §1.0 Partial at the existing backend-supply grain while
the Agents read is pending. A failed read applies F2: keep the last-good page rendering
explicitly stale, and Retry repeats only `GET /api/models/agents`. A successful read installs
the exact returned `AgentSupply` row, whose actual `mode` alone selects LF-D or LF-H — including
a concurrent switch back to Hub. LF-D preserves every settled independent Source authority;
LF-H reuses M5 and reads Sources before calling the Hub surface current. DM-4 skips the
redundant Agents read because it already carries the exact landing payload. No branch
resurrects the Route draft or derives a page form from an error. A Route write with no exact
Route authority — either no response or DM-2's shaped `direct_mode` response — enters the
same `unknown` attempt settlement. No mode or nonmatching observation repeats or settles
that mutation.

The current guard-totality contract deliberately has **no `guard_token`, digest, version
receipt or server-held confirmation state** `[contract]`. Only an exact unchanged echo of
both refusal arrays confirms a nonempty recomputed plan. For nonempty manual `mutation.route_replace`,
submitted removals by themselves do not activate the guard: a visible noninterrupting
removal succeeds ordinarily and appears in `removed_hops`; only nonempty
`would_interrupt` refuses. For `mutation.route_restore` (DELETE or accepted empty PUT), actual effective removals also
activate the existing guard even without interruption. Every refusal carries its exact
current plan and mutates nothing. ET-13a and ET-13b own abandoning and confirming it.

**R6 and M6 own the success before the dialog may disappear** `[contract]` `[derived]`.
The returned `chain` is the post-write projection and commit evidence. Later reads may
update live projections, but never replace that held response evidence while its report
or M6 handoff is mounted. Each non-empty `removed_hops` / `interrupted` array renders once using the
shared hop and SupplyGap blocks, while an empty member skips only its block. ET-10 starts
M6 as the report mounts; ET-11 starts the same read for a two-empty R6 envelope, while
ET-17c starts it when the exact chain endpoint is the first matching authority after a lost
response. In every case the exact evidence and modal stay mounted while AR-M has an acquired
member pending; a mode-dependent deferred member is not itself pending. Each successful
acquired member is installed/held immediately; a
successful AgentSupply installs its exact page behind the modal and leaves Done focused even
if a sibling later fails. Direct mode makes Route-chain members not applicable and disowns
their reads; it does not turn them into stale failures. No settlement acknowledges, closes or
transfers the report. ET-20's Done-equivalent family is the only user-initiated
ownership-transfer exit; it may hand an in-flight M6 read to the page but never starts,
cancels or repeats it.
Route membership changes also change Source `adopted_by`, AgentSupply/model-supply and
row/current projections, so installing only the returned chain is not a complete handoff.
AR-M failure never questions the write or discards a successful member: a report stays
mounted, and a report-free commit enters Committed projection stale only for its acquired
failed subset. A failed Agents prerequisite is that exact subset; its deferred companions
are not reported stale.

**Lost-response and M6 reads use one factorized attempt-result algebra** `[contract]`
`[derived]` D-36. AR's reducer key is `(page-session workflow generation, member,
acquisition eligibility, observation epoch, member settlement, required causal-frontier set,
decisive page evidence)`;
RO's separate product key is `(mutation-attempt settlement, Route observation, owner,
observation legality)`. Page-form authority, member settlement, attempt settlement and exact
Route observation are orthogonal: a mode result may settle and install the page while the
attempt stays unknown, a nonmatching chain may not settle it, and a Source failure may not
rewrite an exact Route result. Each independently acquired member records pending,
successful authority, failure or not applicable plus the epoch at which that observation
began. A successful result is installed/held immediately and is never erased because a
sibling failed. **Installation precedes ownership transfer**: every successful Agents,
Source or AgentChain member first replaces that member in the held/page projection; only
then may an ET/AS edge close a modal, disown a workflow or move focus. Cleanup can discard
comparison metadata, never the installed authority that produced it.

**Acquisition eligibility is a separate axis** `[derived]`. Each AR-M member is exactly
`ready`, `deferred-by-prerequisite` or `acquired`. `ready` means every prerequisite is
known and the sole owner may acquire its next epoch; `acquired` owns exactly one pending or
settled generation. `deferred-by-prerequisite` means mode has not made the member eligible:
it owns no request, is not pending/failed/stale and is excluded from AR-M2/AR-M3 completion.
At M6 entry Agents is acquired first and mode-dependent Source/Route applicability is
deferred. The exact commit chain remains held while deferred; it is not a pending read. If
Agents fails, only Agents settles failed and becomes the exact Retry subset. Its successful
successor dispatches mode, then atomically promotes each still-applicable companion to
`ready`: LF/AR acquires Source once under the selected frontiers, while Hub activates the
held commit chain directly as acquired/successful and Direct drops Route as not applicable.
A member can never be both deferred and stale, and no deferred member is named in a Retry
subset.

There is no scalar “M6-current” flag. Instead, each applicable member carries the set of
causal frontiers that can invalidate it. R6 receipt or the first matching exact-chain
observation establishes a Route-commit frontier required by Source, Agents and Route; the
returned/read chain self-satisfies Route at that frontier. Every exact Hub AgentSupply
landing establishes a later Hub-landing frontier required by Source, while that payload
self-satisfies Agents. A member closes its slot only when its observation was acquired at or
after every frontier required by that member. An earlier success remains installed as
latest-known authority, but the reducer acquires exactly one successor generation; an earlier
failure likewise cannot satisfy the newer frontier. Decisive evidence recomputes the
applicable-member set and acquisition eligibility before completeness, stale or Retry is
evaluated. A member removed from that set is disowned, not failed, and only pending work is
cancelled; a success already received remains installed. At most one generation per member
owns reads for a workflow.
Starting its successor invalidates the prior member generation, and every late result from an
invalidated or disowned generation is ignored.

| Frontier | Authority event | Members invalidated / self-satisfied | Closure rule |
| --- | --- | --- | --- |
| CF-R — Route commit | R6 receipt or the first exact matching AgentChain observation | Requires successor Source and Agents observations; the returned/read AgentChain self-satisfies Route | Every still-applicable member must have an observation acquired at or after CF-R |
| CF-H — Hub landing | Every exact returned AgentSupply whose mode is `hub` | Requires a successor Source observation; the returned AgentSupply self-satisfies Agents | Sources closes only with an observation acquired at or after this latest CF-H as well as CF-R when commit is held |

Frontiers are workflow-local monotonic event ordinals, not wall-clock timestamps. Adding a
frontier never erases an installed success; it only makes an older observation insufficient
to settle the member and causes one successor acquisition.

Evidence precedence is closed and order-independent. An exact `AgentSupply.mode: direct` or
chain `direct_mode` takes the same Direct page-form edge before any sibling failure: retain
every successful independent Source/Agents member, cancel/disown only the currently illegal
Route read, and acquire one authoritative Agents landing read when needed. It never settles an
unknown Route write. That read's exact returned mode alone selects LF-D or LF-H; the decisive
error itself is no landing payload. LF-H establishes the Hub-landing frontier before it
acquires or accepts a Source member, so only a post-landing Source observation can call that
Hub projection current. In Hub,
only the exact AgentChain returned by `GET /api/models/agents/<backend>/chain?model=<id>`
compares normalized submitted intent for D-36: a nonempty manual override must match
the submitted pairs; inherited intent requires `manual_override: null`, not equality
with a prior effective chain or an empty-to-empty comparison. Matching intent
starts M6 with response-only tails unavailable and the actual current inherited plan.
A nonmatching observation is installed as the
latest Route authority but leaves the old attempt `unknown`; it authorizes only another
explicit exact-chain read or abandonment, never a PUT. The latest installed authoritative page projection becomes the
reversible exit origin, so later failure cannot reveal an older opening snapshot.

**LF is the closed landing read-set owner** `[derived]`. DM and AS classify/install exact
payloads; they do not independently decide companion projection freshness or whether an RO
attempt may resume. Landing eligibility is a property of the exact mode payload, never its
producer:

| ID | Exact landing evidence | Closed read set and installation | Failure / next owner |
| --- | --- | --- | --- |
| LF-D | Any exact returned `AgentSupply.mode: direct` | Install that AgentSupply immediately; retain every already-settled independent Source authority. Apply `legality = Direct-suspended` to a held RO product state and cancel/disown only its now-illegal exact-chain read | No companion read. A later authoritative Hub landing enters LF-H; RO eligibility is then decided only from its product axes |
| LF-H | Any exact returned `AgentSupply.mode: hub`, including ordinary M5 PATCH, DM, AR, AS and later page refresh producers | Install that AgentSupply immediately, establish a new Hub-landing frontier for Sources and require one Source observation acquired at or after it before calling the Hub landing current. Outside proven M6 this is M5's landing-owned Source read. Inside M6, a Source generation already acquired after this exact landing may satisfy it; a pre-landing Source success/failure remains latest-known only and is superseded by exactly one successor read | A frontier-satisfying Source failure keeps AgentSupply installed, marks only Sources stale and offers only the owning Source read Retry. Every frontier-satisfying Source success invokes one common post-Source hook: set `legality = Hub-observable`; if the attempt is `unknown`, page-session-owned and owns no exact-chain generation, acquire exactly one through RO-O. No observation result emits a PUT |

**RO is the attempt-settlement × Route-observation reducer** `[derived]`. It is independent
from LF's page-form settlement. Mutation-attempt settlement is exactly
`terminal-rejected | terminal-committed | unknown`. Exact Route observations are separately
recorded as `matching | nonmatching | read-failed` plus their acquisition epoch; before an
observation settles that register is absent and its in-flight generation belongs to RL.
Owner is `mounted editor | page session`, and legality is
`Hub-observable | Direct-suspended`. The immutable normalized submitted intent/stage and workflow
generation are attached only to `unknown`; no producer name is part of any axis:

| Axis | Values | Only authority that changes it | Invariant |
| --- | --- | --- | --- |
| Attempt settlement | `terminal-rejected`, `terminal-committed`, `unknown` | A shaped non-guard/non-Direct rejection → `terminal-rejected`; R6 or an exact matching chain → `terminal-committed`; no response or DM-2 without exact Route authority → `unknown` | A nonmatching/read-failed observation, mode, dismissal or sibling result cannot settle an unknown attempt |
| Route observation | absent/in-flight, then `matching@epoch`, `nonmatching@epoch` or `read-failed@epoch` | Only the exact chain GET settles its own named epoch | Every successful matching/nonmatching AgentChain is installed before any cleanup or owner change. An observation never authorizes a PUT |
| Owner | `mounted editor`, `page session` | ET-18a or AS-1 forced Direct handoff changes mounted editor → page session; explicit ET-8a/ET-8b abandonment ends the workflow rather than transferring it | Owner changes neither settlement nor observation; reload alone implicitly discards page-session evidence and never sends a mutation |
| Observation legality | `Hub-observable`, `Direct-suspended` | LF-H's successful post-Source hook → Hub-observable; any exact Direct evidence → Direct-suspended | Legality changes neither attempt settlement nor observation; Direct cancels only illegal chain work and never the held evidence |

The product's legal work and settlement are total:

| ID | Product predicate | Exact work owner | Result / visible disposition |
| --- | --- | --- | --- |
| RO-R — terminal rejection | `(terminal-rejected, mounted editor, *)` | No D-36 read is legal. CA-R1 may admit an explicit Retry only while the retained draft passes ordinary V5; activation freezes a **new** workflow generation and sends nonempty PUT or inherited DELETE according to normalized intent | The old attempt remains terminal-rejected and is never resumed. Invalid V5 disables Retry and renders its ordinary row feedback; Cancel abandons without mutation |
| RO-O — observable unknown | `(unknown, mounted editor, Hub-observable)` or `(unknown, page session, Hub-observable)` with no current exact-chain generation | Admit exactly one exact-chain observation for the held pairs. Mounted entry is acquired/settled as an AR-D member; first page-session entry is acquired by the producer-independent LF-H post-Source hook and a later successor only by D-35. A second Hub payload cannot acquire a sibling generation | Match installs the AgentChain, changes settlement to `terminal-committed` and enters M6. Nonmatch installs the AgentChain but leaves settlement `unknown`; read failure also leaves it `unknown`. Both are read-only: another admitted action may read again, never send PUT |
| RO-S — suspended unknown | `(unknown, *, Direct-suspended)` | Retain generation/pairs/stage across Direct/Hub page-form transitions; cancel/disown only an illegal exact-chain generation | Expose no mutation Retry and make no commit/non-commit claim. The next LF-H post-Source success changes only legality, then RO-O may acquire exactly one observation |
| RO-C — committed | `(terminal-committed, *, *)` | No further D-36 classification is legal; M6 owns projection reconciliation | Mode changes and dismissal cannot question or repeat the mutation; response-only impact members remain unavailable after inferred commit |

RO-O settlement is factorized by owner; these rows alone own cleanup, destination, visible
feedback, focus and next work for its exact-chain answer:

| Owner + result | Mandatory installation before cleanup | Cleanup / destination | Visible feedback / focus | Next owner |
| --- | --- | --- | --- | --- |
| Mounted editor + match | Install exact matching AgentChain | Set settlement `terminal-committed`, establish CF-R and keep the modal mounted in Route committed, reconciling | `route.refreshing`; focus `route.impact.done` | M6/AR-M; acquire every member successor required by CF-R |
| Mounted editor + nonmatch | Install exact nonmatching AgentChain as the row's newest Route authority | Keep settlement `unknown`; Source may finish independently. The modal remains read-only and may explicitly abandon through ET-8b | Existing unresolved/failure copy; Source failure adds only `route.fail.reconcileRead`. Focus the admitted read Retry or Cancel | No mutation owner. A new exact-chain read may be admitted after active members settle; user abandonment closes the workflow, and a later row activation starts a new editor workflow from installed authority |
| Mounted editor + read failure | None | Keep `unknown`, settle the exact-chain generation and retain draft/latest installed Hub authority | `route.fail.reconcileRead`; focus the admitted read Retry | A newly admitted mounted RO-O read only |
| Page session + match | Install exact matching AgentChain | Set settlement `terminal-committed`, establish CF-R and retain exact Hub page without remounting the editor | Page Chain-unresolved marker clears; preserve current FF-1-valid page target or PF-1 | M6 as page-owned reconciliation; acquire the Agents-first frontier sequence before settlement |
| Page session + nonmatch | Install exact nonmatching AgentChain | Keep settlement `unknown` and retain the page-session evidence; do not claim abandonment or non-commit | Keep the row's uncertain marker and current focus. No mutation Retry renders | D-35 re-expansion may acquire another page-owned RO-O read; reload is the sole implicit evidence drop |
| Page session + read failure | None | Keep `unknown`, settle the generation and leave the exact row Chain unresolved | Existing D-35/Chain-unresolved treatment; preserve current FF-1-valid page target or PF-1 | D-35 re-expansion may acquire one page-owned RO-O read |

The boundary is deliberate `[derived]`: this local single-user API has no Route attempt id,
terminal-status endpoint or replayable success envelope. Therefore an exact nonmatch cannot
prove that an unanswered PUT has stopped; the old PUT may still commit later. Unknown is
read-only for its whole lifetime and **no automatic or recovery PUT resend exists**. The
user may explicitly abandon the mounted workflow, inspect the installed current chain and,
if another change is needed, activate that row again to create a new editor workflow. A
later read naturally reveals a late old commit just as it reveals a concurrent editor.
Reload is the sole implicit evidence drop; no persistent reconciliation ledger is created.

**AP is the wire-authority provenance register** `[contract]`. Every consumed wire member
MUST name both its schema and the route that makes it available to this producer. A declared
but optional member cannot drive a total transition unless that producer makes its presence
total; intentionally unconsumed members remain recorded rather than being described as absent.

| ID | Member / projection | Schema and route provenance | Presence / disposition in frame 02 |
| --- | --- | --- | --- |
| AP-1 | `AgentSupply.mode` and the returned backend's page projection | `model-hub-contracts/agent-supply.schema.json`; `GET /api/models/agents` (`api.md` route table) | `mode` is required and consumed for page-form dispatch. The exact returned AgentSupply is installed as page authority |
| AP-2 | `AgentSupply.routes` | `model-hub-contracts/agent-supply.schema.json`; `GET /api/models/agents` | The member is schema-declared but optional and is deliberately not consumed by D-36. Its presence or equality cannot prove this Route write |
| AP-3 | `AgentChain.manual_override` and `chain[*].source_id/model_id` | `model-hub-contracts/agent-chain.schema.json`; exact chain GET (`api.md` route table) | Normalized manual_override and effective chain are required. This endpoint alone compares submitted intent after a lost response: exact nonempty manual pairs or inherited null. Effective pair equality alone cannot prove intent |
| AP-4 | `Source[]` | `model-hub-contracts/source.schema.json`; `GET /api/models/sources` (`api.md` route table) | Consumed for V5 eligibility and the complete Source/adoption projection; it never proves Route commit |
| AP-5 | R6 `chain`, `removed_hops`, `interrupted` | route-replace/Restore success schema and mutation matrix; exact chain PUT/DELETE (`api.md` route table) | All returned members are consumed. D-36 inference has no response envelope, so both response-only impact arrays are marked unavailable |

| ID / workflow generation | Independent reducer edge + acquisition eligibility / observation epoch | Required frontiers / Route outcome | Decisive page evidence | Immediately installed / held authority | Applicable set after decisive evidence | Awaited / deferred / stale members | Cancellation / late result | Destination / Retry subset |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AR-D1 — initial Agents failure | Agents fails before any exact landing | Attempt remains `unknown`; no Route observation or M6 frontier exists | No mode or Route evidence exists | Keep latest installed page projection and immutable draft/stage | Agents | Failed Agents read | Settle generation; no sibling exists | ET-16b; Retry Agents only through ET-16a |
| AR-D2 — Direct AgentSupply | Agents succeeds with exact backend `mode: direct` | Attempt remains `unknown`; change only legality to Direct-suspended | Mode is decisive for page form only and is already the authoritative landing read | **Install AgentSupply first**, then AS-1/LF-D may close the modal; retain Route generation/submitted pairs/stage and every settled Source | Agents for landing; exact Route observation is suspended, not settled or failed | None while Direct; a suspended attempt is visibly non-mutation-retryable | Cancel/disown only illegal chain work; ignore that generation's late result; retain the complete RO product state | Actual Direct page; no mutation Retry. Any later LF-H post-Source hook re-evaluates RO-O from product state |
| AR-D3 — Hub AgentSupply | Agents succeeds with exact `mode: hub` | Attempt remains `unknown`; the landing self-satisfies Agents and establishes a Hub-landing frontier required by Source | Hub permits exact reconciliation but proves no Route equality | **Install Hub AgentSupply first** as reversible page origin; retain immutable draft/stage | Agents, Sources and exact AgentChain | One Source generation acquired after this landing + exact AgentChain | ET-17a acquires both after installation in this workflow generation; AP-2 stays unconsumed | Originating unknown failure, pending; no Retry while active |
| AR-D4 — Hub members pending | AR-D3 plus independently settling Source and exact-chain members | Each member settles only its own slot; no sibling result changes attempt settlement or Route observation | No attempt decision until exact match or a terminal producer answer exists | Retain AS-2 and install every successful member before any edge | Agents, Sources and exact AgentChain | Pending or failed Source/chain slots | Own both reads until AR-D5–AR-D9 or RL disown | Derive the destination only from the orthogonal product table below |
| AR-D5 — chain becomes Direct | Chain settles `direct_mode`; Source may be pending, successful or failed in its own epoch | Attempt remains `unknown`; change only legality to Direct-suspended | Chain Direct takes the common Direct page-form edge but proves no mutation outcome | Install every successful Source/Agents member first; hold illegality evidence plus generation/pairs/stage. The error is no landing payload | Source + authoritative landing Agents; exact Route observation is suspended while Direct | Landing Agents read unless the decisive member itself was Agents | Preserve settled Source; disown only pending comparison work; ignore its late results; keep the complete RO product state | ET-18a family → page Partial or retained committed modal; Retry only landing reads, never the mutation. Any later LF-H post-Source hook re-evaluates RO-O |
| AR-D6 — exact chain match / commit frontier | Exact AgentChain manual_override matches normalized submitted intent, regardless of Source settlement or response order | Install `matching@epoch`, then set attempt `terminal-committed` and establish CF-R. The matching chain self-satisfies Route | Exact equality proves commit before any sibling result | **Install exact AgentChain first** as D-36 evidence; keep earlier successes installed as latest-known only | M6 Source, Agents and Route-chain projections | Acquire an Agents successor first; after its exact landing, acquire the Source successor required by CF-R and any CF-H | Supersede/disown a predecessor member only when its successor is acquired; ignore predecessor late results | ET-17c → M6; later Source success/failure cannot change commit; Retry only a frontier-satisfying failed subset |
| AR-D7 — exact chain nonmatch | Exact AgentChain manual_override differs from normalized submitted intent, regardless of Source settlement or response order | Record `nonmatching@epoch`; attempt remains `unknown` and no commit frontier exists | Exact inequality is only a Route observation and never selects page form or proves the PUT terminal | **Install exact AgentChain first** as the row's newest Route authority; preserve independently installed Source/Agents | Agents, Sources and exact chain | Only the Source slot may remain pending or failed | Settle only the chain generation; Source cannot change attempt settlement or the installed observation | Read-only product table below; another explicit Retry may issue only a newer exact-chain read, never a PUT |
| AR-D8 — exact chain read failure | Non-Direct exact-chain observation fails | Record `read-failed@epoch`; attempt remains `unknown`. No commit frontier exists | No new Route authority | Retain every successful Source/Agents member already installed | Agents, Sources and exact chain | Chain failed plus any still-pending/failed Source | Settle only chain failure; no success or axis is rolled back | After all pending members settle, ET-17b; Retry only the exact failed read subset, never an already-successful member or mutation |
| AR-D9 — Source member settles, mode-neutral | The post-Hub-landing Source generation returns success or failure before or after AR-D6/AR-D7/AR-D8 | Success or failure settles only Source at its acquisition epoch and changes no attempt/observation axis. If a later commit/Hub frontier applies, acquire a successor instead of treating this slot as current/stale | Source carries no page-form or Route-attempt evidence | **Install Source success before any transition**; mark only a frontier-satisfying failure stale | The mode-selected set is unchanged | Source alone becomes current, failed or pending for a newer frontier | Chain settlement cannot undo Source; Source settlement cannot undo Route observation | Re-evaluate the product table. A Source-only failure Retry never rereads Agents or exact chain |
| AR-M1 — acquired member settles, mode-neutral | Active M6 + an `acquired` Source, Agents or Route-chain member returns success or failure | Settle only that member. It closes its slot only if its acquisition epoch satisfies every required frontier; otherwise retain it as latest-known and make one successor `ready` for acquisition. Activating the held commit chain is an immediate successful settlement, not a request | Only an exact successful Agents mode payload may select LF/AS page form. Source success/failure, Agents failure and non-Direct Route settlement carry no page-form evidence | Install/hold a success monotonically; mark only a frontier-satisfying acquired failure stale. Successful Agents invokes LF/AS; every other result updates only its member authority/staleness | Before decisive mode, Agents is applicable/acquired and Source/Route applicability is deferred. Exact Direct activates Source and makes Route not applicable; exact Hub activates Source and the held self-satisfying Route evidence. Agents remains applicable in both | Acquired pending members; deferred companions before mode; exact acquired failures after settlement | Sibling failure cannot undo a success. A failed prerequisite leaves dependants deferred and does not make them stale; ignore predecessor-epoch results | Keep the currently installed Direct/Hub page and mounted/transferred ownership unchanged. Agents-first failure settles through AR-M3 with Agents alone retryable; successful Agents Retry activates companions. A late Direct-mode Source result settles only Source and never resurrects Hub |
| AR-MD — decisive Direct | Active M6 + exact AgentSupply `mode: direct` **or** Route-chain `direct_mode` | The Route-commit frontier remains held; Route slots are provisionally not applicable until the landing Agents member settles | Both producers take this one reducer edge; the landing AgentSupply then recomputes applicability/acquisition eligibility from its exact mode | Preserve every successful independent member. A current Direct AgentSupply self-satisfies Agents; chain Direct acquires that landing read through ET-18aM/ET-18aP | Landing Direct: Source + Agents activated, Route not applicable. Landing Hub: Source + Agents activated; Route uses the held commit chain when it is the CF-R evidence, otherwise a chain-Direct handoff acquires one newly applicable exact chain | Landing Agents when chain supplied Direct; if it returns Hub, Sources also requires its new landing frontier and Route becomes applicable. Before landing, every mode-dependent companion remains deferred | Cancel/disown only prior pending Route work and ignore its late results; never discard a successful Source/Agents member | Mounted report remains mounted or receiving page enters Partial until AS dispatch; Retry only the current acquired failed subset |
| AR-M2 — all activated frontiers satisfied | Active M6 + no acquired member pending/failed and every activated applicable member has an observation satisfying all frontiers required by that member | Complete activated surface settles reconciliation. No deferred member can coexist with a successful decisive mode; before mode, only Agents is an activated obligation | The latest exact mode-selected set controls page form | Keep every exact activated member and commit evidence | Latest mode-selected set | None; not-applicable Route members are neither current nor stale | Settle generation; ignore duplicate/disowned/predecessor results | Mounted evidence remains until ET-20 or transferred page remains; Retry absent |
| AR-M3 — acquired failed subset settles | Active M6 + no acquired member pending/unsatisfied + one or more acquired failures after their frontiers. An Agents-first failure qualifies with Source/Route still deferred | Commit frontier held; the acquired generation settles partially current without pretending deferred members were observed | Any already-successful mode/authority remains decisive; an Agents-first failure supplies no new page form | Keep every successful acquired member installed and commit evidence held | Before mode: Agents acquired, Source/Route deferred. After mode: exact activated set | Exact failed acquired subset is visibly stale; deferred members are neither visible stale nor Retryable | Settle; ignore later results from it | Mounted → ET-19a; transferred → ET-19c; Retry exactly the acquired failed subset, including Agents alone before mode |
| AR-M4 — retry acquired failed subset | Settled AR-M3 + explicit admitted Retry | All causal frontiers remain held; successor epochs for only the failed acquired subset become pending. Deferred companions stay deferred during an Agents Retry | No prior success is reread or discarded | Retain installed members/evidence | Recompute only after a successful decisive Agents result; then activate the exact mode-selected companions | Prior acquired failed subset becomes pending; no deferred member is sent | CA first hands focus off, then disables Retry; predecessor/disowned results are ignored | Same mounted/receiving phase; Retry exactly the failed subset. Successful Agents Retry dispatches LF/AS and acquires newly ready companions once |

AR-D presentation and read eligibility are derived only after its independent reducers run:

| Attempt settlement / Route observation | Source slot | Derived destination / admitted work |
| --- | --- | --- |
| `unknown`; observation in flight | Pending, successful or failed | Remain in AR-D4; no mutation Retry exists |
| `unknown`; `read-failed@epoch` | No pending sibling | ET-17b retains every installed success and retries only the failed Source/chain subset |
| `unknown`; `nonmatching@epoch` | Pending | Keep the installed exact chain and wait only for Source; Cancel may explicitly abandon through ET-8b |
| `unknown`; `nonmatching@epoch` | Successful after the current Hub-landing frontier | Admit one newer exact-chain read through CA-D7/ET-17d; never run V5 or send PUT in this workflow |
| `unknown`; `nonmatching@epoch` | Failed after the current Hub-landing frontier | ET-17f keeps the installed chain, marks only Sources stale and admits only ET-17g's Source read Retry; after Source succeeds, only a newer chain read is eligible |
| `terminal-rejected`; observation absent | Not acquired | No D-36 member is legal. CA-R1 may create a new workflow only after ordinary V5 validation; it cannot attach any observation to the terminal attempt |
| `terminal-committed`; observation absent from an R6 response | Not acquired until M6 selects mode | Enter ET-10/ET-11 M6; Route settlement is final and response members, not D-36, own evidence |
| `terminal-committed`; `matching@epoch` | Any pre-frontier Source settlement | Enter ET-17c/M6 immediately; the commit frontier requires a successor Source generation, so the earlier result cannot complete or fail M6 |

**Round-3 totality fixtures** `[derived]`. Each row is mandatory; an implementation that
cannot name the reducer/CA result for one row is incomplete.

| Fixture | Seed / event | Required invariant and owner |
| --- | --- | --- |
| RT-1 — inferred commit after early reads | AR-D Source and Hub Agents settle before the lost PUT commits; the later exact-chain read matches | Install the early successes as latest-known only, establish CF-R, acquire Agents first and then acquire Sources after that exact landing; both successors must settle before AR-M2 |
| RT-2 — M6 chain becomes Direct | AR-M has installed Hub Agents; its chain member returns `direct_mode` | AR-MD provisionally removes prior Route work, preserves independent successes and uses ET-18aM/ET-18aP to acquire landing Agents authority. Direct keeps Route not applicable; Hub acquires a new post-landing chain epoch |
| RT-3 — Direct after Source success | Source settles successfully before either AR-D5 or AR-MD | Keep that Source installed/carried; disown only pending siblings. The landing Agents result, not the Direct error, selects the page form |
| RT-4 — active Retry loses admission | Activate Retry in save reconciliation, mounted M6, transferred M6 or error-handoff page phases | CA synchronously moves focus to the destination phase's first FF-1-valid owner before disabling/inerting Retry; ET supplies no competing focus patch |
| RT-5 — local Source-order draft sort | Activate frame-02 sort, then save its explicit pairs | Follow `model-hub.md` §4.2/§4.6 and `api.md`'s negative fixture: the local gesture may reorder the draft; the per-model PUT transmits only explicit `hops`, its server path never reads `sources.order`; frame 03 separately saves default membership/order while preserving manual routes |
| RT-6 — Hub landing closes Sources | DM-1–DM-3 owns the landing Agents read; before it settles another client completes Direct → Hub and atomically creates a native Source | LF-H installs exact Hub AgentSupply, invokes M5's Source read outside M6 and calls the landing current only after that read. Failure keeps AgentSupply, marks only Sources stale and retries only Sources |
| RT-7 — CA activation modality | For every enabled CA row, activate its control once by pointer, Enter and Space; repeat for disabled/inert rows | All three enabled gestures emit that row's one `Activation`, including CA-P1 → ET-18c. Every disabled/inert gesture emits nothing; no keyboard enumeration may restate CA IDs |
| RT-8 — DM-2 unknown crosses ordinary M5 | Route PUT returns shaped `direct_mode`; LF-D installs Direct; the ordinary §1.9 mode PATCH later returns exact Hub | DM-2 creates `(unknown, page session, Direct-suspended)` before modal handoff. M5/LF-H closes Source, the common hook changes legality and RO-O sends exactly one chain GET: match enters M6; nonmatch installs current Route and remains page-owned unknown; neither branch sends PUT |
| RT-9 — explicit mounted-unknown abandonment | Lose a Route PUT response in Hub and dismiss through ET-8b before or after nonmatch/read failure | First install any successful AgentSupply/Source/AgentChain member, then ET-8b disowns every old-workflow generation and drops its attempt evidence. No page-session reconciliation is created by this voluntary exit; a later row activation opens a new workflow from the installed projection |
| RT-10 — Direct M6 settles Source independently | In M6, Agents settles exact Direct before the Source member; exercise later Source success and failure | AR-M1 installs/marks only Source at its epoch, preserves the Direct page and applicable set, and never remounts Hub. Success is monotonic authority; failure alone enters the Source stale subset |
| RT-11 — every Hub producer runs one hook | Table-drive every authoritative Hub landing producer registered now (ordinary M5 PATCH, DM, AR, AS and page refresh) plus a fixture producer added to the table | Each establishes CF-H, acquires/accepts exactly one frontier-satisfying Source observation and only after its success invokes the same product-state hook. With `(unknown, page session, Hub-observable)` exactly one RO-O chain generation starts; without that state none starts. Producer identity is neither input nor branch |
| RT-12 — Source predates M6 Hub landing | An M6 Source read succeeds, then a concurrent Direct → Hub transition creates a native Source before the exact Hub AgentSupply lands | Install the earlier Source only as latest-known authority. LF-H establishes a later Source frontier and acquires exactly one successor; only that successor may close LF-H/M6 or become its stale Source failure |
| RT-13 — Source follows M6 Hub landing | CF-R is held; the exact M6 Hub AgentSupply lands and establishes CF-H before the Source successor is acquired | That one post-landing Source generation satisfies both CF-R and CF-H. Do not start a duplicate Source read |
| RT-14 — nonmatch then Source failure | Exact chain first installs `nonmatching@epoch`; the frontier-satisfying Source read then fails | Keep attempt `unknown`, preserve the exact observation/chain authority, mark only Sources stale and admit only ET-17g's Source Retry. Agents/chain are neither reread nor used to infer termination |
| RT-15 — Source failure then nonmatch | The frontier-satisfying Source read fails first; exact chain then installs nonmatch | Reach the identical product as RT-14: `unknown + nonmatching@epoch + failed Source`, with the same retained authority, feedback and Source-only Retry. Response order changes no reducer axis |
| RT-16 — mounted Agents prerequisite failure | R6 report or report-free commit mounts M6; the Agents-first read fails before any mode is known | Settle Agents as the sole acquired failed subset, keep Source/Route applicability `deferred-by-prerequisite`, enter ET-19a and enable only CA-M2. Retry reads Agents only; its exact successful mode then activates/acquires Source once and either activates the held commit chain under Hub or drops Route under Direct |
| RT-17 — transferred Agents prerequisite failure | ET-20 transfers the same Agents-first generation before its failure arrives | Reach ET-19c on the receiving page with Agents alone stale/Retryable and Source/Route applicability deferred. CA-M4 reads Agents only; no report is remounted and no deferred member is displayed stale |
| RT-18 — CA-D positive phase partition | Table-drive initial idle/busy, generic-subset idle/busy, Source-only idle/busy, post-nonmatch exact-read idle, plus invalid and successful products | Each of the seven named families selects exactly its CA-D row; invalid/successful selects zero enabled rows. ET-17b cannot also admit CA-D1, ET-17f cannot also admit CA-D1/D3, and every busy phase emits nothing for pointer/Enter/Space. CA-R1 is exercised separately and can create only a new workflow |
| RT-19 — unknown/nonmatch dismissal | Exercise `unknown + nonmatching@epoch` while Source is pending, after failure and during Source-only Retry; invoke Cancel/title-close/Escape/outside | Every gesture takes ET-8b: first retain the installed exact AgentChain, then disown modal-only work, drop the old attempt evidence and reveal that newest authority. It sends no read/mutation. Saving remains inert, terminal rejection uses ET-8a and committed stays ET-20 |
| RT-20 — PF-1 survives removed backend | Refresh the edited backend to `cli_present: false` in (a) Hub/01 with retained Sources and zero installed groups and (b) Direct/no-Sources No backend found | Active target, model row and group head all fail FF-1; PF-1 selects the first existing registered control on the exact destination page. Focus never targets the omitted group or unregistered text, and no new UI/copy is required |
| RT-21 — selector selection owner | Open Add selector, then move by Arrow/Home/End and pointer, and narrow by filter, before confirmation | ET-5a selects the first listed candidate; every ET-5d move atomically makes the new active candidate the selected pair, and a filter that drops the active one re-elects the first still-listed candidate through the same edge. `route.add.confirm` derives enabled state only from that owner and ET-5c appends exactly it |
| RT-22 — rejected is terminal, unknown is read-only | Table-drive shaped rejection, unknown + matching, unknown + nonmatching and unknown + read failure | Rejection performs no D-36 read and explicit Retry creates a new workflow/PUT. Matching enters commit/M6. Nonmatching and failure leave the old attempt unknown and can issue only chain reads or abandon; no case emits a recovery PUT |
| RT-23 — late old PUT after nonmatch | Exact chain read returns nonmatch, then the unanswered old PUT commits after the observation | The installed nonmatching chain remains the latest observed authority and no PUT was resent. A later D-35/row-open read naturally installs the late commit; the UI never claims the earlier nonmatch proved non-commit |
| RT-24 — successful-member install before exit | In each response order, settle successful Agents, Source or nonmatching/matching AgentChain, then immediately dismiss or transfer the modal | The member is installed before cleanup/focus/owner change. ET-8a/ET-8b may discard workflow evidence but the receiving page cannot reveal an older member projection |

ET-8a owns ordinary reversible exits and terminal rejection. ET-8b owns explicit
abandonment of an `unknown` attempt, including after a nonmatching observation. Before
either exit, every successful member is already installed; neither reconstructs the opening
row after AS-2 or AR-D7 installed newer authority. ET-8b disowns all old-workflow read and
mutation rights, drops the attempt evidence, and reveals that installed projection. It sends
nothing; a later row activation is a new editor workflow, not recovery of the old attempt.

**Frame-02 transition ownership is closed** `[contract]` `[derived]`. AR alone settles a
composite-read member generation and selects its cited ET/AS handoff; RO alone settles the
Route attempt and exact observation axes, independently from LF/AS page-form landing. This table alone owns user
gestures, single producer answers and the atomic cleanup/destination/focus/read effects of
those handoffs. AS rows install only the exact projection AR selected; LF closes the selected
mode's companion read set. V5/DM only classify evidence, and CA alone admits and dispatches
its async controls. Every transition MUST
match exactly one AR or ET owner; **an unlisted transition is forbidden**. Rows are atomic by
construction: equivalent gestures may share a row only when cleanup, destination, focus and
next owner are identical.

The LF/AS rows are the one AgentSupply page-form dispatcher. Every complete-surface read that
can change the page shape enters one of them before installing a destination: Direct always
installs the exact Direct payload and preserves settled Source authority, while Hub installs
the exact Hub payload and closes M5's Source read before calling the surface current. No ET row may
repeat or bypass that mode decision.

**FF-1 is the focus-validity gate and PF-1 is the sole post-install fallback** `[derived]`.
Every Entry focus cell below is evaluated after the destination renders: a named target is
legal only when it is mounted, semantically focusable, and admitted by V5/CA/the static
control register. Disabled, inert, absent and informational-only nodes fail FF-1. A legal
active target is preserved; otherwise the edge follows its explicit fallback chain, or the
first FF-1-valid control in the destination's registered Tab order when no special chain is
listed. **Before CA makes the active control disabled, inert or absent, CA synchronously
resolves the destination phase and moves focus to that phase's first FF-1-valid owner.** The
admission change and focus handoff are one transition: ET rows do not retain the old target
or choose a competing one. In a reversible save-reconciliation phase the fallback is enabled
Cancel/title-close; in a mounted committed phase it is enabled Done/title-close; on the
receiving page it is that page's registered fallback. A registered programmatic status may
be targeted; an unregistered heading or text node may not. PF-1 resolves post-install focus
only by filtering this ordered candidate list through FF-1. It stops at the first passing
candidate; a failed candidate never becomes a terminal output:

| PF-1 order | Candidate | When it passes FF-1 | When it fails |
| --- | --- | --- | --- |
| 1 | The exact active target after install/close | It remains mounted, semantically focusable and admitted in the installed destination | Continue; background settlement cannot preserve an absent/inert target |
| 2 | Exact `(backend, menu_model)` row | Hub renders that exact row and admits its registered target | Continue; Direct, collapse or refreshed row omission cannot own focus |
| 3 | Exact backend group head | `installedAgents` still contains that backend and its group head is mounted/focusable/admitted | Continue; `cli_present: false` may remove the complete group, so its head is never assumed stable |
| 4 | Destination page's first registered FF-1-valid control in Tab order | The receiving page is mounted; select the first of its existing controls that passes FF-1 | No later candidate exists. A destination with no valid registered control is an incomplete page registration, not license to target text |

Candidate 4 covers both 01 with retained Sources but zero installed groups and §1.8's No
backend found branch. It reuses those destination pages' registered controls and adds no
heading, key or UI. Direct and Hub use the same list; mode changes which earlier candidates
can pass, not the resolver.

Modal-closing AS/ET edges cite PF-1 because their modal target unmounts. Transferred M6
AS-5b/AS-6b invokes it only when the refreshed projection removes the user's current page
target or makes it ineligible; otherwise focus stays where the user moved it. Mounted
AS-5a/AS-6a always leave admitted Done focused. The event table remains the sole destination
owner. Its **Visible feedback** cell MUST cite a key in the copy register below or name an
existing registered state/copy block; a prose-only announcement is not an implementation
contract.

The table's block citations are closed aliases, not free-form copy: “normal/owning editor
copy” means the exact Copy keys column of Ready, Dirty or Invalid after refresh; “selector
copy” means `route.add.source`, `route.add.model` and `route.add.confirm`; “originating
failure copy” means that origin's registered `route.fail.save` or
`route.fail.unconfirmed`; “F1/F2 cause” means the registered failure line for that same
state; “Partial”, “projection”, “report”, “refresh” and “stale” copy mean the exact Copy
keys column of the named §0.8 state. Shared Qp6FI, hop and SupplyGap blocks cite their
existing registers. No alias licenses a new literal.

**RL closes every read lifecycle** `[derived]`. Acquiring a read records its generation;
only the named edge may cancel or disown it. Cancellation is best-effort transport cleanup,
while disowning is the normative state-machine action: every later result from that
generation is ignored and cannot install authority, feedback or focus.

| Read owner | Generation acquisition | Cancellation / disown owner | Late-result disposition |
| --- | --- | --- | --- |
| Opening exact chain read | ET-1 acquires one dialog-opening generation; ET-4 settles the old generation before acquiring its successor | ET-8a disowns the current ET-1/ET-4 generation before modal close; ET-18a settles/disowns it on `direct_mode` | Ignore every result after successor acquisition, ET-8a or ET-18a |
| RO exact-chain observation | ET-16a acquires one mounted RO-O generation only for an `unknown` attempt; ET-17d acquires its mounted successor after a nonmatching observation. After every LF-H Source success, the common hook acquires one page-owned RO-O generation exactly when the product is `(unknown, page session, Hub-observable)`; D-35 acquires a page-owned successor after nonmatch/read failure | ET-8b explicitly abandons the mounted workflow and disowns its current generation. ET-18a/AS-1 transfer ownership to the page session, change only legality to Direct-suspended and cancel the now-illegal request. Successor acquisition disowns its predecessor; reload is the sole implicit evidence discard | Ignore results after explicit abandonment, reload or successor acquisition. Direct preserves settlement/observation/pairs/stage; a later Hub post-Source hook may acquire exactly one legal successor. A nonmatching result remains an observation of the unknown attempt and admits only another exact read, never a PUT |
| AR-D Source + exact-chain reads | ET-17a attaches both to the current unknown workflow after AR-D3's Hub landing, recording a distinct member epoch for each. ET-17g acquires a Source-only successor after AR-D9 failure; ET-17h acquires exactly the generic failed read subset; ET-17d acquires a chain-only successor after Source is current | AR-D5 settles Direct evidence and disowns only work made illegal before ET-18a. AR-D6 installs a matching observation, settles `terminal-committed` and establishes CF-R; AR-D7 installs a nonmatching observation without settling the attempt; AR-D8 settles only chain failure; AR-D9 independently settles Source. ET-8b abandons and disowns the full mounted unknown workflow | A result after member disown/supersession cannot alter any settled axis. Route match/nonmatch is never rewritten by Source; Source is never rewritten by Route. Every success installs before cleanup or owner transfer. An installed observation predating a later CF-R/CF-H remains latest-known but cannot satisfy that frontier |
| AR-M Source, Agents and Route-chain reads | ET-10/ET-11 establishes CF-R, marks Source/Route `deferred-by-prerequisite` and acquires Agents first; ET-17c transfers CF-R into the same sequence. A successful Agents mode makes applicable companions ready; every LF-H establishes CF-H before acquiring/accepting its Source successor. ET-19b/ET-19d acquires only the exact acquired failed subset | ET-20 transfers rather than cancels. Agents failure settles without acquiring deferred companions. AR-MD from either Agents Direct or chain `direct_mode` preserves successful independent members, cancels/disowns prior pending Route slots and uses ET-18aM/ET-18aP when a fresh landing Agents read is required. Landing Direct keeps Route disowned; landing Hub establishes CF-H, then acquires one Source successor and makes Route applicable. A successor disowns its predecessor member generation | Ignore disowned and predecessor-epoch results. Only an acquired member settles AR-M, and only when its epoch satisfies all CF-R/CF-H requirements; deferred members produce no late result or stale slot |
| Error-handoff Agents read | ET-18a acquires the page-owned generation; ET-18c settles the failed generation and acquires a successor | Page replacement or a successor disowns the prior generation | Ignore every prior-generation result; a current success alone enters LF-D/LF-H and its AS install. Every LF-H result then uses the same post-Source RO hook regardless of this producer |

| ID / event | Origin + held evidence | Authority dispatch order | Transient cleanup | Destination | Visible feedback | Entry focus | Next request/read owner |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ET-1 — open model row | Mounted §1.1 row + exact `(backend, menu_model)` | Acquire the RL opening generation and start the exact chain GET; infer no projection | Close the row menu; no draft/grab exists | Loading route | `route.loading` | FF-1-valid `route.cancel` | Dialog-owned chain GET |
| ET-2 — opening chain success | Loading route + returned AgentChain | Validate the envelope, then install its normalized manual_override and effective pairs as reversible origin and draft | Clear loading only | Ready | Normal frame copy | Keep `route.cancel`; payload arrival does not steal focus | None |
| ET-3 — opening non-direct failure | Loading route + held page row; DM-1 is reserved for ET-18a | Exclude `direct_mode`, then classify the shaped/transport failure | Clear loading; retain the independent page row | Route unread | `route.fail.read` | `route.retry` | None until explicit Retry |
| ET-4 — unread Retry | Route unread + exact identity/page row | Settle the old RL generation, acquire its successor and reissue only the opening chain GET | Clear the old error | Loading route | `route.loading` | FF-1-valid `route.cancel` | Dialog-owned chain GET |
| ET-5a — open Add selector | Ready, Dirty or Invalid after refresh + current V5 authorities | Materialize current V5 candidates, focus the filter field and make the first listed candidate the single active/selected pair carried as its active descendant | Clear any prior selector owner before assigning that pair | Same editor state + selector open | Selector keys; existing invalid lines remain | The active/selected first exact candidate | None |
| ET-5b — dismiss Add selector | Open selector + unchanged draft | Send/read nothing | Clear selector selection and close only the selector | Owning Ready, Dirty or Invalid state | Normal frame copy; existing invalid lines remain | `route.addHop` | None |
| ET-5c — confirm Add candidate | Open selector + selected exact pair | Revalidate the pair through V5, then append it | Clear selection and close selector | Ready if equal to origin, otherwise Dirty or existing Invalid after refresh | Normal copy plus every remaining invalid line | New row's grip | None |
| ET-5d — choose another Add candidate | Open selector + current active/selected candidate | Send/read nothing; Arrow/Home/End from the filter field, pointer activation, or a filter change that drops the active candidate atomically makes the destination candidate both active and selected | Replace the one prior active/selected owner | Same open selector | Existing selector copy | Newly active/selected candidate | None |
| ET-6a — remove one row | Ready, Dirty or Invalid after refresh + exact row/draft | If a nonempty draft survives, remove locally and re-run V5. Removing the final hop instead preserves the prior unsaved draft for Undo and enters the existing null Restore preview | Drop selector/grab state owned by that row; invalidate older previews | Nonempty draft: Ready/Dirty/Invalid by normalized intent and V5. Final hop: inherited preview loading/result/retry; never empty Manual | Existing manual or Restore/Undo copy and actual inherited target/origin | Nonempty: successor/predecessor grip then enabled Cancel. Final hop: existing Undo restore focus target | Final hop only: read-only preview null; no persistence |
| ET-6b — pointer move one row | Ready, Dirty or Invalid after refresh + exact row/draft | Apply the one pointer move, then re-run V5 | Finish the pointer operation | Ready if equal to origin; Dirty if changed and valid; existing Invalid while marked rows remain | `route.reorder.position` plus registered invalid lines | Moved row's grip | None |
| ET-6c — global draft sort | Ready, Dirty or Invalid after refresh + exact draft and page-held Source order | Apply the registered stable local sort once, then re-run V5; no-op is valid | Finish only the sort gesture | Ready if equal to origin; Dirty if changed and valid; existing Invalid while marked rows remain | Changed → `route.reorder.sorted`; no-op → `route.reorder.unchanged`; retain registered invalid lines | `route.reorder`, including after a no-op | None |
| ET-7a — ungrabbed row-focus move | Ready, Dirty or Invalid after refresh + focused grip | Send/read nothing; Arrow/Home/End changes only the roving row | Replace the prior roving focus | Same editor state | Existing row copy | Destination row's grip | None |
| ET-7b — begin keyboard grab | Ready, Dirty or Invalid after refresh + focused grip/order | Snapshot pre-grab order; send/read nothing | Set only this grip `aria-grabbed=true` | Same editor state + grabbed row | `route.reorder.grabbed` | Same grip | None |
| ET-7c — move grabbed row | Grabbed row + pre-grab order/current draft | Arrow/Home/End mutates the draft and re-runs V5 | Keep the same grab owner | Same grabbed phase over reclassified draft | `route.reorder.position` plus registered invalid lines | Same grip at its new position | None |
| ET-7d — drop grabbed row | Grabbed row + current draft | Send/read nothing; settle current order | Clear `aria-grabbed` and pre-grab snapshot | Ready, Dirty or Invalid by settled V5 result | `route.reorder.dropped` plus registered invalid lines | Dropped row's grip | None |
| ET-7e — cancel keyboard grab | Grabbed row + pre-grab order | Restore the exact snapshot; send/read nothing | Clear `aria-grabbed` and snapshot | Prior Ready, Dirty or Invalid class | `route.reorder.cancelled` plus registered prior invalid lines | Same row's restored grip | None |
| ET-7f — Tab from grabbed row | Grabbed row + current draft | Settle current order and re-run V5 before focus movement | Clear `aria-grabbed` and snapshot, then perform ordinary Tab/Shift+Tab | Ready, Dirty or Invalid by settled V5 result | `route.reorder.dropped` plus registered invalid lines | Ordinary next/previous enabled modal control | None |
| ET-8a — ordinary / terminal-rejected reversible exit | Loading route, Route unread, Ready, Dirty, Invalid after refresh or Route save rejected + latest installed authoritative page projection | Send no mutation; disown the current RL opening generation before close. A terminal rejection owns no D-36 generation | Close selector; restore/cancel any grab; discard local/submitted draft and terminal rejection evidence; ignore every late result from disowned opening reads | Reveal the latest installed page authority | Exact latest projection; retain its existing unresolved marker if any. No recovery read or false commit report follows | Preserve an FF-1-valid active page target, otherwise PF-1 after the modal target unmounts | None; an already-unresolved row retains D-35 |
| ET-8b — explicit unknown-attempt abandonment | Attempt settlement is `unknown`, owner is mounted editor, legality is Hub-observable or Direct-suspended, and the latest successful members are already installed | Send no mutation/read; explicitly abandon this old workflow and disown every mounted AR-D/RO generation before close | Close selector; restore/cancel any grab; discard submitted pairs/stage and all old-workflow rights. Ignore every later result from disowned generations | Reveal the latest installed authority, including an AgentSupply, Source or matching/nonmatching AgentChain installed before this edge | Exact latest projection with no old-attempt Retry. A later row open starts from current authority as a new workflow; it never resumes or replays this attempt | Preserve an FF-1-valid page target, otherwise PF-1 after modal close | None |
| ET-9a — first Route save | V5-valid nonempty manual draft or ready inherited preview | Freeze normalized intent and send nonempty PUT or inherited DELETE without `force` or plan echoes | Close selector; settle any grab | Saving | `route.saving` | Programmatically focusable progress status | One owned Route PUT or DELETE matching the immutable intent |
| ET-10 — R6 success with impact | Saving + exact R6 envelope with either array nonempty | Consume every R6 member, establish CF-R, mount its report and start AR-M with Agents acquired and Source/Route applicability deferred | Release mutation/refusal ownership; retain the entire envelope and held self-satisfying chain | Route impact reported with AR-M pending | Impact title/detail and each nonempty block | `route.impact.done` | Agents landing read; mode then acquires Sources under Direct or after CF-H under Hub and activates/drops the held Route evidence |
| ET-11 — two-empty R6 commit | Saving + exact R6 envelope whose two impact arrays are empty | Preserve the exact envelope, establish CF-R and start AR-M with Agents acquired and Source/Route applicability deferred | Release mutation ownership; retain the held self-satisfying chain; no origin can be restored | Route committed, reconciling | `route.refreshing` | `route.impact.done` | Agents landing read; mode then acquires Sources under Direct or after CF-H under Hub and activates/drops the held Route evidence |
| ET-12 — guarded 409 | Saving + immutable draft + complete current refusal arrays | Validate guard code and both arrays; a later 409 atomically replaces both | Release mutation ownership only after the current plan is held | Route save refused | Shared Qp6FI copy and current arrays | `guard.cancel` | None until explicit user action |
| ET-13a — abandon guard | Route save refused + immutable draft/current arrays | Send/read nothing | Drop the refusal plan only | Dirty | Normal draft | `route.save` | None |
| ET-13b — confirm guard and send | Route save refused + immutable draft/current arrays | Revalidate exact echoes, then send the same draft with `force: true` and both arrays | Drop the rendered refusal as the immutable values freeze into the request | Saving | `route.saving` | Programmatically focusable progress status | One owned Route PUT or DELETE matching the immutable intent |
| ET-14 — definitive non-guard rejection | Saving + submitted draft; DM-2 is reserved for ET-18a | Exclude guard and `direct_mode`, then settle this attempt `terminal-rejected` | Release mutation ownership; retain only the locally validated draft for an explicit new workflow | Route save rejected | `route.fail.save` | FF-1-valid `route.retry` | None; rejection never enters D-36 |
| ET-14a — explicit new attempt after rejection | Route save rejected + retained draft + CA-R1 admission | Re-run ordinary V5; when valid create a new workflow generation and send ordinary non-forced nonempty PUT or inherited DELETE. The prior terminal attempt supplies no request identity, force flag, plan echo or replay right | Clear old rejection feedback only as the new generation acquires mutation ownership | Saving | `route.saving` | Programmatically focusable progress status | One new owned Route PUT or DELETE matching normalized intent; any returned guard begins a new Qp6FI phase |
| ET-15 — no response evidence | Saving + submitted draft + initial/confirmed stage | Set attempt settlement `unknown`; infer neither commit nor refusal | Release mutation ownership; retain immutable attempt evidence | Route save outcome unresolved | `route.fail.unconfirmed` | `route.retry` | None until a read-only Retry |
| ET-16a — begin unknown reconciliation | Route save outcome unresolved in CA-D1 initial idle + held draft/stage | Start one AR-D generation with `GET /api/models/agents` | Keep error body/draft; CA-D2 owns focus transfer plus Retry busy/inert admission | Same unknown failure, reconciliation pending | Existing failure copy | CA-D2 handoff | One AR-D Agents read; no second generation is admitted |
| ET-16b — AR-D1 failure handoff | AR-D1 selected after the Agents member failed | Do not choose a page form | Clear active generation/busy; retain error body/draft and latest page authority | Originating failure | `route.fail.reconcileRead` | FF-1-valid `route.retry` | None until a newly admitted ET-16a |
| ET-17a — AR-D3 Hub comparison begins | AR-D3 selected after AS-2 installs the exact Hub AgentSupply | Under the same workflow generation read current Sources and the exact chain endpoint; record a member epoch for each and never inspect AP-2 for equality | Keep modal/draft; CA-D2 retains its completed handoff and Retry admission | Originating failure, Hub reconciliation pending over the installed Hub page; accumulator enters AR-D4 | Existing failure copy | Preserve CA-D2's FF-1-valid owner | One Source read + one exact-chain read in the current AR-D generation |
| ET-17b — settled AR-D read failure | The orthogonal unknown-attempt product has no pending member and at least one failed Source/chain slot, except the dedicated nonmatching-observation + Source failure row ET-17f | Preserve every attempt/observation axis and successful installed member; do not restart an already-authoritative read | Clear active generation/busy; retain draft and latest installed page projection | Route save outcome unresolved | `route.fail.reconcileRead` | FF-1-valid `route.retry` | CA-D3 admits a successor for exactly the failed Source/chain subset; CA-D4 owns its pending phase |
| ET-17c — AR-D6 matching chain proves commit | AR-D6 selected from an exact AgentChain whose manual_override matches normalized submitted intent | The exact AgentChain is already installed; record `matching@epoch`, settle the attempt `terminal-committed`, mark response-only tails unavailable and establish CF-R before transferring to AR-M | Clear comparison busy; discard obsolete read Retry state; retain earlier successes as latest-known but frontier-insufficient | Route committed, reconciling | `route.refreshing` | FF-1-valid `route.impact.done` | Acquire Agents first; LF-H then establishes CF-H and acquires Sources. Keep the matching chain self-satisfied |
| ET-17d — repeat exact observation after nonmatch | Attempt remains `unknown`; AR-D7 holds an installed `nonmatching@epoch`; AR-D9 holds Source success satisfying current CF-H; CA-D7 admits the read | Acquire one successor exact-chain GET under the same read-only workflow; send no PUT and do not run V5 as a mutation gate | Retain draft/evidence and every installed authority; CA-D2 owns focus transfer plus Retry busy/inert admission | Route save outcome unresolved, exact read pending | Existing unresolved copy | CA-D2 handoff | One exact-chain successor; match enters ET-17c, nonmatch updates the observation epoch, failure enters ET-17b |
| ET-17e — independent refresh invalidates local draft | Ready, Dirty or Route save rejected with no mutation owned + newly installed authoritative Source/chain projection that makes a new/changed pair fail V5 | Install the authority first and re-run V5; send no PUT | Clear selector/grab state invalidated by the refresh; when present discard terminal rejection evidence while retaining editable rows | Invalid after refresh | `route.invalidAfterRefresh` on every offender | First offending row's FF-1-valid Remove action | None |
| ET-17f — nonmatching observation + Source failure | AR-D7 holds installed `nonmatching@epoch` and AR-D9 settles a Source failure satisfying current CF-H, in either response order | Preserve attempt `unknown` and the exact observation; classify only Sources stale | Clear active generation/busy; retain draft, AgentSupply and installed exact chain authority | Route save outcome unresolved with Source stale | `route.fail.reconcileRead` | FF-1-valid `route.retry` | CA-D5 admits only ET-17g; Agents/chain and the mutation are not eligible |
| ET-17g — Source-only reconciliation Retry | ET-17f + CA-D5 admission | Reissue only `GET /api/models/sources` under the same latest CF-H; preserve attempt `unknown` and the nonmatching observation | Retain all settled authority; CA-D6 owns focus handoff plus Retry busy/inert admission | Same unresolved failure, Source reconciliation pending | Existing failure copy | CA-D6 handoff | One Source successor; success admits only CA-D7/ET-17d's newer exact read, failure returns ET-17f |
| ET-17h — generic failed-read-subset Retry | ET-17b + exact failed Source/chain subset + CA-D3 admission | Reissue only that subset; preserve every settled RO axis and successful member | Retain all authority/draft; CA-D4 owns focus handoff plus Retry busy/inert admission | Same failure, subset reconciliation pending | Existing failure copy | CA-D4 handoff | One successor per failed member; each result re-enters its independent AR-D edge |
| ET-18a — reversible decisive `direct_mode` handoff | Loading, Saving or AR-D5 + matching DM-1/DM-2/DM-3 evidence and no CF-R commit frontier | Error evidence selects no page form; retain every successful independent member and start/transfer the page's Agents read. DM-2 first sets attempt settlement `unknown`; every handoff changes owner to page session and legality to Direct-suspended without changing settlement or observation | Close modal/selector and cancel grab. Discard editable presentation; for unknown retain generation/submitted pairs/stage in the page session. Disown only pending illegal Route siblings | Page §1.0 Partial over every latest successful Source/Agents projection | Existing Partial copy; never label Direct from the error or claim a Route outcome | Stable owning backend group head | Page Agents read; result enters LF-D/LF-H. LF-H's post-Source hook re-evaluates RO-O; every late disowned sibling result is ignored |
| ET-18aM — mounted M6 chain-Direct handoff | Mounted report/committed modal + AR-MD selected from chain `direct_mode` | Keep CF-R/commit evidence and successful independent members; start the landing Agents read without deriving a page form from the error | Retain modal and enabled Done; disown only prior pending Route work | Same mounted report/committed phase over last installed page until AS-5a/AS-6a | Existing report/refresh copy | Keep FF-1-valid `route.impact.done` | Landing Agents dispatches mode; Hub establishes CF-H and acquires every required Source/Route successor, Direct does not |
| ET-18aP — transferred M6 chain-Direct handoff | Receiving page + AR-MD selected from chain `direct_mode` | Keep CF-R/commit evidence and successful independent members; start the page-owned landing Agents read | Retain installed page members; disown only prior pending Route work | Page §1.0 Partial over the last installed page | Existing Partial copy plus held committed evidence | Preserve current FF-1-valid page target, otherwise PF-1 | Landing Agents dispatches mode; Hub establishes CF-H and acquires every required Source/Route successor, Direct does not |
| ET-18b — reversible error-handoff Agents read failure | ET-18a Partial + last-good page projection + optional unknown page-session RO state | Classify only the read failure | Clear pending; keep last-good projection explicitly stale and retain all RO axes/evidence unchanged; CA-P1 admits page Retry | Page §1.0 Partial/F2 | Existing F2 cause and page Retry | FF-1-valid page Retry | None until ET-18c; M6 landing-read failure instead remains in AR-M and settles through ET-19a/ET-19c |
| ET-18c — retry error-handoff Agents read | ET-18b + stale page projection + CA-P1 admission | Reissue only `GET /api/models/agents` | Retain stale projection and all RO axes/evidence; CA-P2 owns focus transfer plus Retry busy/inert admission | Page §1.0 Partial pending | Existing F2 copy | CA-P2 handoff | Page Agents read; success enters LF-D/LF-H and its AS install/post-Source hook |
| ET-19a — mounted AR-M3 failed subset | Mounted report/committed modal + AR-M3 settled generation | Failure cannot negate commit or any successful installed member | Clear generation pending; retain modal/evidence and mark only the acquired failed member set stale. Mode-dependent deferred members remain unrendered/non-stale | Same impact report, or report-free Committed projection stale | `route.impact.refreshFail` plus prior report/refresh copy | FF-1-valid `route.retry` | None until CA-M2 admits ET-19b |
| ET-19b — mounted failed-subset Retry | ET-19a + exact acquired failed subset + CA-M2 admission | Start AR-M4 for exactly that subset; an Agents-only Retry leaves companions deferred until its success dispatches mode | Keep modal/evidence/successful members; CA-M1 owns focus transfer plus Retry busy/inert admission | Same report or report-free state, retry pending | Prior report/refresh copy | CA-M1 handoff | One AR-M successor generation for the acquired failed subset; successful Agents activates/acquires applicable companions once |
| ET-19c — transferred AR-M3 failed subset | ET-20 receiving page + AR-M3 settled generation | Failure cannot negate commit or any successful installed member | Clear generation pending; retain evidence and mark only acquired failed members stale; deferred companions remain non-stale | Receiving page with partial current authority | `route.impact.refreshFail` | Preserve current page focus only if FF-1-valid; otherwise use PF-1 | None until CA-M4 admits ET-19d |
| ET-19d — page-owned failed-subset Retry | ET-19c + exact acquired failed subset + CA-M4 admission | Start AR-M4 for exactly that subset; an Agents-only Retry leaves companions deferred until its success dispatches mode | Keep evidence/current members; CA-M3 owns focus transfer plus Retry busy/inert admission | Same receiving page, retry pending | Existing stale line | CA-M3 handoff | One AR-M successor generation for the acquired failed subset; successful Agents activates/acquires applicable companions once |
| ET-20 — committed Done-equivalent exit | Mounted report, reconciling, refreshed or stale phase + exact commit evidence | Done, title-close, Escape and outside send no mutation/read | Close modal and transfer any active AR-M generation/evidence; never restore origin | Receiving page with exact installed successes plus markers only for acquired pending/stale members; deferred companions stay unrendered until their prerequisite activates them | Held evidence plus the registered pending/stale marker as applicable | PF-1 after modal Done unmounts, over the exact installed page | Transferred AR-M if active; otherwise none, or later CA-M4/ET-19d after failure |
| AS-1 — AR-D2 Direct install | AR-D2 + exact AgentSupply `mode: direct` | LF-D installs that exact Direct row before any Source/chain read | Settle the Agents member and close modal. Preserve attempt settlement/observation, change owner to page session and legality to Direct-suspended; retain generation/pairs/stage exactly when settlement is unknown | Actual Direct page; an unknown attempt remains page-session-owned and suspended | Exact Direct projection; mode makes no Route claim | PF-1 because the modal target unmounts | No read while Direct; any later LF-H post-Source hook re-evaluates RO-O from axes, not producer |
| AS-2 — AR-D3 Hub install | AR-D3 + exact Hub AgentSupply | LF-H installs that exact Hub row as the latest reversible page authority, establishes CF-H for Sources and deliberately does not consume AP-2 for commit comparison | Clear only Agents-member pending; retain generation/modal/draft/stage and CA-D2 admission | Originating failure over actual Hub page | Existing failure copy | Preserve CA-D2's FF-1-valid owner | ET-17a acquires the post-CF-H Source generation and exact-chain read in the same workflow generation |
| AS-3 — reversible error-handoff Direct dispatch | ET-18a/ET-18c + exact AgentSupply `mode: direct` | LF-D installs that exact Direct row and keeps every successful independent Source authority already carried | Clear page-read pending and stale Hub row; retain RO settlement/observation/owner and set legality Direct-suspended | Actual Direct page | Exact Direct projection | Preserve an FF-1-valid page target or use PF-1 | No companion read; an M6 landing read dispatches through AS-5a/AS-5b instead |
| AS-4 — reversible error-handoff Hub dispatch | ET-18a/ET-18c + exact AgentSupply `mode: hub` | LF-H installs that exact Hub row, keeps successful independent authority and starts M5's Source read; never resurrect the editor | Clear Agents pending; retain the old Source list explicitly stale until M5 settles and retain all RO axes/evidence | Actual Hub page with Sources pending/stale until LF-H closes | Exact Hub projection plus `upstream.unread` only after Source failure | Preserve an FF-1-valid page target or use PF-1 | M5 Source success invokes the common LF-H post-Source hook; M6 landing uses AS-6a/AS-6b instead |
| AS-5a — mounted AR-M Direct install | Mounted evidence + current successful M6 AgentSupply `mode: direct`, whether it supplied AR-MD or answered ET-18aM | Dispatch mode immediately, install the exact current Direct page behind the modal, activate Source when it was deferred and make all Route-chain members not applicable | Retain modal/evidence and acquired Source/Agents state; cancel/disown pending Route reads and remove their failed/pending slots from stale/Retry | Same mounted report/committed phase over actual Direct page | Registered report/refresh/stale block only for acquired applicable members | Keep FF-1-valid `route.impact.done` | AR-M acquires/continues activated pending members, or CA admits only an acquired failed subset after settlement |
| AS-5b — transferred AR-M Direct install | Transferred AR-M + current successful M6 AgentSupply `mode: direct`, whether it supplied AR-MD or answered ET-18aP | Dispatch mode immediately, install the exact current Direct page, activate Source when it was deferred and make all Route-chain members not applicable | Retain evidence and acquired Source/Agents state; cancel/disown pending Route reads and remove their failed/pending slots from stale/Retry | Actual Direct page with exact acquired pending/stale markers | Exact Direct projection plus registered acquired pending/stale block | Preserve the current active page target only if FF-1-valid; otherwise PF-1 | AR-M acquires/continues activated pending members, or CA admits only an acquired failed subset after settlement |
| AS-6a — mounted AR-M Hub install | Mounted evidence + successful M6 AgentSupply `mode: hub`, including the landing read after chain Direct | Dispatch mode immediately, install the exact Hub page behind the modal, activate Source/Route companions and establish CF-H for Sources; the payload self-satisfies Agents | Retain modal/evidence and independent authority. Acquire one Source successor unless its generation was acquired after this exact landing. Activate the held self-satisfying commit chain without a read; only a chain-Direct handoff with no exact Hub chain authority acquires one newly applicable chain epoch | Same mounted report/committed phase over actual Hub page | Registered report/refresh/stale block for acquired AR-M members | Keep FF-1-valid `route.impact.done` | AR-M continues every acquired member missing its required frontier, including Sources and any newly acquired chain, or CA admits an acquired failed subset after settlement |
| AS-6b — transferred AR-M Hub install | Transferred AR-M + successful M6 AgentSupply `mode: hub`, including the landing read after chain Direct | Dispatch mode immediately, install the exact Hub page, activate Source/Route companions and establish CF-H for Sources; the payload self-satisfies Agents | Retain evidence and independent authority. Acquire one Source successor unless its generation was acquired after this exact landing. Activate the held self-satisfying commit chain without a read; only a chain-Direct handoff with no exact Hub chain authority acquires one newly applicable chain epoch | Actual Hub page with exact acquired pending/stale markers | Exact Hub projection plus registered acquired pending/stale block | Preserve the current active page target only if FF-1-valid; otherwise PF-1 | AR-M continues every acquired member missing its required frontier, including Sources and any newly acquired chain, or CA admits an acquired failed subset after settlement |

**Frame-02 control × state totality** `[frame]` `[derived]`. This is the exhaustive static
rendering register for the Route editor. A control not listed for a state is absent or inert;
each listed control supplies its copy key, enable predicate and keyboard reachability. It
does **not** own state edges: activation emits the cited ET event, whose row alone owns
cleanup, destination and entry focus.

**CA is the sole async control-admission and activation matrix** `[derived]`. It covers controls whose
admission changes while AR owns a generation; the static table remains authoritative for
controls with no async phase delta. `aria-busy` communicates progress and never substitutes
for native/semantic disabled or an inert activation handler. There is at most one active
reconciliation or M6 generation for the workflow. Pointer, Enter and Space on an enabled CA
row all emit that row's one `Activation`; those `Activation` cases MUST NOT be enumerated
again in a static or keyboard register. While disabled/inert, all three gestures send nothing. Before
an admission transition disables, removes or makes the active control inert, CA applies the
FF-1 handoff above synchronously. An unlisted async subphase for a CA-covered control is
absent or inert. CA-D predicates are a positive exhaustive and mutually exclusive partition:
one exact product phase selects one row, never a negated liveness test and never two rows.
The seven named CA-D phase families below cover initial idle, initial busy, generic subset
idle, generic subset busy, Source-only idle, Source-only busy and post-nonmatch exact-read
idle. CA-R1 is the separate terminal-rejection/new-workflow admission; invalid or successful
products select no enabled CA-D row:

| ID | Control | Async subphase | Rendered / admitted | Synchronous focus handoff | Activation |
| --- | --- | --- | --- | --- | --- |
| CA-R1 | Rejected-write Retry | **Terminal rejection idle**: ET-14 holds `terminal-rejected`, the retained draft is V5-valid and no new workflow owns a PUT | Rendered, enabled | On activation mount and focus the programmatically focusable `route.saving` status before Retry becomes inert | Emit ET-14a; create one new non-forced workflow/PUT and confer no identity, force/plan echo, replay or reconciliation right from the terminal attempt |
| CA-D1 | Save-failure Retry | **Initial idle**: ET-15 origin or ET-16b, attempt settlement is `unknown`, no AR-D AgentSupply landing, no settled Source/chain subset and no generation active | Rendered, enabled | None while admitted | Emit ET-16a and atomically acquire the sole initial Agents generation |
| CA-D2 | Save-failure Retry | **Initial busy**: ET-16a Agents, then AS-2/ET-17a or ET-17d Source/chain generation active before an ET-17b/ET-17f/ET-17c settlement | Rendered busy, disabled/inert | If Retry is active, move first to enabled Cancel, otherwise enabled title-close, before changing admission | None; cannot overlap AR-D or reach any PUT |
| CA-D3 | Failed-read subset Retry | **Generic subset idle**: ET-17b holds an exact failed Source/chain subset, excluding the dedicated nonmatching-observation + Source-failed product, and no member is pending | Rendered, enabled | None while admitted | Emit ET-17h for exactly the failed read subset; never reread a settled success or send a mutation |
| CA-D4 | Failed-read subset Retry | **Generic subset busy**: ET-17h's exact generic failed-subset successor is active | Rendered busy, disabled/inert | If Retry is active, move first to enabled Cancel, otherwise enabled title-close, before changing admission | None; cannot overlap the successor generation |
| CA-D5 | Source-only reconciliation Retry | **Source-only idle**: attempt `unknown` + `nonmatching@epoch` + failed Source and no member is pending | Rendered, enabled | None while admitted | Emit ET-17g for Sources only |
| CA-D6 | Source-only reconciliation Retry | **Source-only busy**: ET-17g's Source successor is active while attempt remains `unknown` and the nonmatching observation stays installed | Rendered busy, disabled/inert | If Retry is active, move first to enabled Cancel, otherwise enabled title-close, before changing admission | None; cannot reread Agents/chain or overlap a Source generation |
| CA-D7 | Post-nonmatch exact-read Retry | **Post-nonmatch read idle**: attempt `unknown` + installed `nonmatching@epoch` + frontier-satisfying Source success, with no read pending | Rendered, enabled | None while admitted | Emit ET-17d and acquire one newer exact-chain GET; no activation in this workflow can send PUT |
| CA-M1 | Mounted M6 Retry | An acquired AR-M member is pending, AR-M4 Retry is pending, or AR-M2 succeeded; deferred companions alone never make Retry busy | Absent; if layout reserves it, disabled/inert | If Retry is active, move first to enabled `route.impact.done`, otherwise its enabled title-close, before changing admission | None |
| CA-M2 | Mounted M6 Retry | Settled ET-19a / AR-M3 exact acquired failed subset, including Agents-only with companions deferred | Rendered, enabled | None while admitted | Emit ET-19b for exactly the held acquired failed subset |
| CA-M3 | Page M6 Retry | An acquired transferred AR-M member is pending, AR-M4 Retry is pending, or AR-M2 succeeded; deferred companions alone never make Retry busy | Absent; if layout reserves it, disabled/inert | If Retry is active, move first to the receiving page's registered FF-1-valid fallback before changing admission | None |
| CA-M4 | Page M6 Retry | Settled ET-19c / AR-M3 exact acquired failed subset, including Agents-only with companions deferred | Rendered, enabled | None while admitted | Emit ET-19d for exactly the held acquired failed subset |
| CA-P1 | Error-handoff page Retry | ET-18b settled landing-Agents read failure | Rendered, enabled | None while admitted | Emit ET-18c and acquire one landing-Agents successor |
| CA-P2 | Error-handoff page Retry | Initial or ET-18c landing-Agents read pending | Rendered busy, disabled/inert | If Retry is active, move first to the page's registered FF-1-valid fallback before changing admission | None |

| State / surface | Control/content | Copy key | Rendered / enabled condition | Static keyboard reachability / emitted event |
| --- | --- | --- | --- | --- |
| Loading route | 取消 button | `route.cancel` | Enabled while the opening GET owns no mutation | Initial focus and first Tab stop; activation emits ET-8a |
| Loading route | Title-bar close | `route.cancel` | Enabled while the opening GET owns no mutation | Independent second Tab stop; activation emits ET-8a |
| Route unread | 重试 | `route.retry` | Enabled | First command in Tab order; activation emits ET-4 |
| Route unread | 取消 button | `route.cancel` | Enabled | Normal Tab order; activation emits ET-8a |
| Route unread | Title-bar close | `route.cancel` | Enabled | Independent Tab stop; activation emits ET-8a |
| Ready / Dirty / Invalid after refresh | 添加一跳 | `route.addHop`; disabled explanation `route.add.none` | Enabled exactly when V5 exposes a candidate | Normal Tab order; activation emits ET-5a |
| Ready / Dirty / Invalid after refresh + Add selector | Candidate filter field | `route.add.search`; empty-result explanation `route.add.noMatch` | Enabled while open; receives focus as the selector opens | Typing narrows the listed candidates and re-elects the active one through ET-5d; Escape emits ET-5b; Tab reaches the candidate region |
| Ready / Dirty / Invalid after refresh + Add selector | Source/model candidate region | `route.add.source`, `route.add.model` | Labels render while open; exactly one currently listed valid pair is the active/selected candidate | Arrow/Home/End or pointer activation emits ET-5d; Escape emits ET-5b; Tab reaches confirmation |
| Ready / Dirty / Invalid after refresh + Add selector | 添加 confirmation | `route.add.confirm` | Enabled only for the selected exact V5-valid pair | Activation emits ET-5c |
| Ready / Dirty / Invalid after refresh | 按来源顺序重排 | `route.reorder` | Enabled; stable-sort no-op remains valid | Normal Tab order; activation emits ET-6c and focus stays here |
| Ready / Dirty / Invalid after refresh | Each hop grip | `route.grip` | Rendered for every hop; exactly one grip is the roving list tab stop | Pointer drag emits ET-6b; Space/Arrow/Home/End/Tab/Escape emit ET-7a–ET-7f by phase |
| Ready / Dirty / Invalid after refresh | Each hop remove action | `route.removeHop` | Rendered for every hop; the current roving row's action joins Tab order | Activation emits ET-6a; ET-7f guarantees no grabbed state reaches it |
| Invalid after refresh | Per-offending-row feedback | `route.invalidAfterRefresh` | Rendered beneath the exact model id for every refreshed-invalid new/changed pair; unchanged stale pairs are excluded | Informational text; each row's Remove remains reachable |
| Ready / Dirty / Invalid after refresh | 取消 button | `route.cancel` | Enabled | Normal Tab order; activation emits ET-8a |
| Ready / Dirty / Invalid after refresh | Title-bar close | `route.cancel` | Enabled | Independent Tab stop; activation emits ET-8a |
| Ready / Dirty / Invalid after refresh | 保存 | `route.save` | Ready and Invalid: disabled. Dirty: enabled only when changed and V5-valid. Invalid Save uses all rendered invalid lines as `aria-describedby` | Activation when enabled emits ET-9a |
| Saving | Owned progress status; no action control | `route.saving` | Stable programmatic focus target; every command/dismissal is inert while PUT is owned | No actionable tab stop; response emits ET-10, ET-12, ET-14, ET-15 or ET-18a |
| Route save refused | 仍要保存 | `guard.confirm.saveRoute` | Enabled for exact current arrays | Shared Qp6FI Tab order; activation emits ET-13b |
| Route save refused | 取消 | `guard.cancel` | Enabled | Initial focus; activation emits ET-13a |
| Route save refused | Title-bar close | `guard.cancel` | Enabled | Independent Tab stop; activation emits ET-13a |
| Route save rejected | 重试 | `route.retry` | CA-R1 alone renders/enables it for a V5-valid retained draft; it creates a new workflow/PUT. Invalid draft renders it disabled | First command in Tab order only when enabled; activation delegates to CA-R1 |
| Route save rejected | 取消 button / title-bar close | `route.cancel` | Enabled whenever no new PUT is owned | Distinct Tab stops; activation emits ET-8a |
| Route save outcome unresolved | 重试 | `route.retry` | CA-D1–D7's positive phase partition is the sole owner. Each enabled row issues only its registered read; busy phases are disabled/inert and no phase emits PUT | First command in Tab order only when enabled; activation delegates to the current CA-D row |
| Route save outcome unresolved with attempt `unknown` | 取消 button / title-bar close | `route.cancel` | Enabled whenever no read response is being synchronously installed; an owned read may be disowned | Distinct Tab stops; activation emits ET-8b and abandons the old workflow |
| Route committed, reconciling / refreshed / Route impact reported / mounted AR-M stale | 完成 | `route.impact.done` | Enabled before, during and after AR-M settlement | First command in Tab order; activation emits ET-20 |
| Route committed, reconciling / refreshed / Route impact reported / mounted AR-M stale | Title-bar close | `route.impact.done` | Enabled before, during and after AR-M settlement | Independent Tab stop; activation emits ET-20 |
| Route impact reported / report-free stale | 重试 | `route.retry` | CA-M2 renders/enables it only after ET-19a settles an acquired failed subset, including Agents alone while companions are deferred; CA-M1 makes it absent/inert during acquired pending/retry and after success | Normal Tab order only when enabled; activation delegates to the current CA row |
| Receiving page with transferred stale subset | 重试 | `route.retry` | CA-M4 renders/enables it only after ET-19c settles an acquired failed subset, including Agents alone while companions are deferred; CA-M3 makes it absent/inert during acquired pending/retry and after success | Page Tab order only when enabled; activation delegates to the current CA row |

**Dismissal is closed by `commit class × modal owner × busy ownership`** `[derived]`.
The four dismissal affordances are distinct inputs but share an ET edge only when cleanup,
destination, focus and next owner are identical. Reconciliation reads are disownable and do
not make a noncommitted modal mutation-busy; Saving alone owns the non-cancellable mutation
response. An entry absent from this matrix is forbidden; selector/grab Escape takes its
more-specific ET-5b/ET-7e edge before this matrix.

| Commit class / exact mounted phase | Modal owner | Busy owner | Cancel button | Title close | Escape | Outside press |
| --- | --- | --- | --- | --- | --- | --- |
| No mutation submitted: Loading / unread / Ready / Dirty / Invalid | Mounted editor | Optional disownable opening/read generation | ET-8a | ET-8a | ET-8a | ET-8a |
| `terminal-rejected` | Mounted editor | None; rejection never owns D-36 | ET-8a | ET-8a | ET-8a | ET-8a |
| `unknown`, including matching read pending, nonmatching/read-failed observation, Source pending/failed/retrying | Mounted editor | None or disownable AR-D read generation | ET-8b | ET-8b | ET-8b | ET-8b |
| Refused guarded plan | Mounted guard | No request | ET-13a | ET-13a | ET-13a | ET-13a |
| Mutation request in flight: Saving | Mounted editor | Non-cancellable PUT response | Inert / absent | Inert / absent | Inert | Inert |
| `committed`: reconciling / report / mounted stale | Mounted report/committed modal | None or transferable AR-M read generation | ET-20 Done-equivalent | ET-20 | ET-20 | ET-20 |

**Keyboard event register** `[derived]`. Focus remains trapped while the modal is mounted;
the static table above defines which controls participate. This table defines emitted key
events only. The cited atomic ET row remains the sole owner of cleanup, destination and entry
focus:

| Key | Emitted event |
| --- | --- |
| `Tab` / `Shift+Tab` | Ungrabbed: move through each enabled control, including Cancel and title-close as distinct stops, and wrap in the modal. Grabbed: ET-7f first drops and clears `aria-grabbed`, then performs that move |
| `Space` on a grip | Ungrabbed → ET-7b start; grabbed → ET-7d drop |
| `↑` / `↓`, `Home` / `End` on a grip | Ungrabbed → ET-7a roving focus; grabbed → ET-7c draft movement |
| Candidate Arrow / `Home` / `End` from the selector's filter field, or pointer activation | ET-5d atomically moves the selector's one active/selected owner |
| `Escape` | Grabbed → ET-7e cancel/restore; selector open → ET-5b; otherwise emit the exact phase edge in the dismissal matrix (including guard → ET-13a); Saving emits nothing |
| `Enter` / `Space` on a remove action | ET-6a removes that row |
| `Enter` / `Space` on 按来源顺序重排 | ET-6c sorts and retains `route.reorder` focus |
| `Enter` / `Space` on 添加一跳 / selector confirmation | ET-5a opens / ET-5c appends |
| `Enter` / `Space` on 保存 | ET-9a; Enter never submits implicitly from a row/selector |
| `Enter` / `Space` on 重试 | Route unread (not CA-covered) → ET-4. Every CA-covered Retry delegates to its one row: when enabled, pointer/Enter/Space all emit that row's `Activation`; when disabled/inert, all emit nothing |
| `Enter` / `Space` on guard primary / 完成 | ET-13b / ET-20 |

Ordinals renumber contiguously after ET-6a/ET-6b/ET-7c. Grab state and every new position use the
same `aria-grabbed` + live-region treatment as §1.3. No prose in this register may add a
state edge: the ET matrix is exhaustive, and the destination/focus of an emitted event is
whatever its one ET row states.

**Copy** — namespace `models.hub.route.*`; the guarded operation delta stays under the
shared `models.hub.guard.*` owner.

| Key under `models.hub.route.*` | 中文 | English |
| --- | --- | --- |
| `title` `[frame]` | {{menuModel}} · 路由链 | {{menuModel}} · Route chain |
| `section` `[frame]` | 这个型号的路由链 | Route chain for this model |
| `addHop` `[frame]` | 添加一跳 | Add a hop |
| `add.source` `[derived]` | 来源 | Source |
| `add.model` `[derived]` | 型号 | Model |
| `add.confirm` `[derived]` | 添加 | Add |
| `add.none` `[derived]` | 没有可添加的来源型号 | No source/model pair is available to add |
| `sourceMissing` `[derived]` | 来源不存在 | Source missing |
| `invalidAfterRefresh` `[derived]` | 这个已编辑的跳在刷新后的来源中不可用。移除它后再保存。 | This edited hop is unavailable after the refresh. Remove it before saving. |
| `reorder` `[frame]` | 按来源顺序重排 | Reorder by Source order |
| `reorder.grabbed` `[derived]` | 已抓取第 {{position}} 跳。使用方向键移动。 | Hop {{position}} grabbed. Use arrow keys to move. |
| `reorder.position` `[derived]` | 已移到第 {{position}} 跳。 | Moved to hop {{position}}. |
| `reorder.dropped` `[derived]` | 已放到第 {{position}} 跳。 | Dropped at hop {{position}}. |
| `reorder.cancelled` `[derived]` | 已取消重排,恢复到第 {{position}} 跳。 | Reorder cancelled. Restored to hop {{position}}. |
| `reorder.sorted` `[derived]` | 已按来源顺序重排。 | Reordered by Source order. |
| `reorder.unchanged` `[derived]` | 路由链已经符合来源顺序。 | The route chain already matches Source order. |
| `hint` `[frame]` | 这条链是写下来的配置。以后调整来源顺序时,其中的来源会按新的顺序重排。 | This chain is stored configuration. Later Source order changes reorder its hops to match. |
| `empty` `[derived]` | 没有可继承的路由。请配置默认路由。 | No inherited route is available. Configure default routing. |
| `removeHop` `[derived]` | 移除这一跳 | Remove this hop |
| `grip` `[derived]` | 调整这一跳的顺序 | Reorder this hop |
| `loading` `[derived]` | 正在读取路由链… | Loading route chain… |
| `cancel` `[frame]` | 取消 | Cancel |
| `save` `[frame]` | 保存 | Save |
| `saving` `[derived]` | 正在保存路由链… | Saving route chain… |
| `refreshing` `[derived]` | 路由链已保存,正在刷新模型页面… | Route chain saved. Refreshing the model surface… |
| `fail.read` `[derived]` | 路由链没读到 | The route chain could not be read |
| `fail.save` `[derived]` | 路由链没保存上 | The route chain was not saved |
| `fail.unconfirmed` `[derived]` | 保存结果还没确认 | The save outcome is not confirmed |
| `fail.reconcileRead` `[derived]` | 当前模型页面暂时没读到 | The current model surface could not be read |
| `retry` `[derived]` | 重试 | Retry |
| `impact.title` `[derived]` `[contract]` | 路由链已保存 | The route chain was saved |
| `impact.detail` `[derived]` `[contract]` | 以下是这次保存实际移除或中断的项目。 | These are the items this save actually removed or interrupted. |
| `impact.refreshFail` `[derived]` | 路由链已保存,但模型页面暂时无法刷新。 | The route chain was saved, but the model surface could not be refreshed. |
| `impact.done` `[derived]` | 完成 | Done |

| Key under `models.hub.guard.*` | 中文 | English |
| --- | --- | --- |
| `title.saveRoute` `[derived]` `[contract]` | 保存 {{menuModel}} 的路由链 | Save the route chain for {{menuModel}} |
| `subtitle.saveRoute` `[derived]` `[contract]` | 这次保存会让已配置的型号没有可用来源 | This save leaves a configured model with no usable source |
| `confirm.saveRoute` `[derived]` | 仍要保存 | Save anyway |

---

### 1.3 Default routing - per backend (approved frame `ztAos`)

The backend action is Default routing / 默认路由. It edits that backend's default Source
membership and priority. Inherited Automatic and Passthrough routes follow it; Manual
overrides retain their exact arrays. Show affected inherited/manual model counts from
server projections without claiming historical human authorship or current health.

Read `AgentSupply.sources.order` and eligibility; save the complete subset with
`PUT /api/models/agents/<backend>/sources`. Optional `force`, `would_remove_hops`
and `would_interrupt` follow the existing exact-plan guard. Pure reordering needs no
guard; membership removal can remove effective hops or supply and must display the
actual refusal, echoing both arrays unchanged on confirmation. Success returns the
authoritative `{agent: AgentSupply}`; reconcile chains and adoption through existing
reads. A failed or ambiguous save keeps the draft and reads canonical defaults before
another write. Switching backends cannot reuse another backend's draft.

Both include and exclude actions exist. Sources outside defaults remain available for
manual routes. Empty defaults are valid and leave inherited routes Unconfigured; existing
manual arrays survive. Zero eligible Sources shows the empty state. Blocked Sources keep
their rank. Rank is configured priority only, never the currently serving Source.

Keyboard/touch operate the same list: include/exclude buttons are focusable; Space
grabs/drops; arrows move a grabbed row or focus; Escape cancels the grab before closing.
Announce new positions and retain moved-row focus. Long lists scroll with header/footer
visible; labels and controls fit desktop/mobile and both color schemes.

Retain the approved helper text: inherited routes first use matching Sources in default
order; without a match eligible Hub API-key Sources forward the original id in that order.
Subscriptions are not speculative candidates. Manual routes remain
independent. Helpers must respect existing fallback and streaming boundaries.

| UI label | English | Chinese |
| --- | --- | --- |
| Group action/title | Default routing | 默认路由 |
| Route origin | Automatic / Manual / Passthrough | 自动 / 手动 / 透传 |
| Empty plan | Unconfigured | 未配置 |
| Empty inherited action | Configure default routing | 配置默认路由 |
| Dialog footer action | Restore automatic | 恢复自动 |
| Draft recovery | Undo restore | 撤销恢复 |

Reuse existing localized Save/Cancel/Retry and guard-impact copy. Origin-help behavior
follows the routing revision above. Compatibility chains/reorder is not the new UI save
path; when called it uses the same default guards and preserves manual arrays.

### 1.4 Frame 04 `XvCC4` — Add subscription

**The question it answers:** *I have a paid subscription — do I let the CLI use it
directly, or hand it to the gateway?* Two dialogs, because the recommended answer
is **different per vendor** and for a reason that is legal, not technical.

**Frame 13 supplies the vendor before this dialog opens** `[frame]` `[contract]`. Every
line below remains per vendor — the head, both option cards, which one carries 推荐, the
ToS note and the hint — while the producer is now explicit: Claude 订阅 selects
`vendor: anthropic`, ChatGPT 订阅 selects `vendor: openai`, and §1.12 owns that mapping. The
radio group here still produces only `channel`; `POST /api/models/oauth/start` combines
the vendor already held with the channel chosen here.

**Static subscription-vendor register**. These are client-owned brand destinations, the
same class of declaration as the vendor name, plan subtitle and ToS note above; they are
not OAuth-flow fields and do not travel through the server. Frame 12 consumes the top-up
destination for `balance_exhausted` and the support destination for `account_banned`:

| Subscription vendor | `Source.vendor` | Top-up destination | Support / appeal destination | Implementation consumer |
| --- | --- | --- | --- | --- |
| Claude | `anthropic` | `https://claude.ai/settings/usage` | `https://claude.ai/restricted` | I4 extends `ui/src/components/settings/models/vendorMeta.ts` with both static destinations |
| ChatGPT | `openai` | `https://chatgpt.com/codex/settings/usage` | `https://openai.com/form/appeal/` | I4 extends `ui/src/components/settings/models/vendorMeta.ts` with both static destinations |

This register is total only for the two subscription vendors the frame-13 producer can
emit. An `api_key` Source does not consume it: even when its compatibility preset leaves a
known `vendor` string on the Source, that string does not identify who operates the
account or where that operator takes payment and support requests. Frame 12 renders the
non-linked 联系你的服务商 fallback for that population. Adding a provider is therefore a
static-register change with a matching I4 consumer, not a new wire member.

**Element inventory**

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| head | `添加 {vendor} 订阅` + `host / plan` | vendor selected in frame 13 | close, named by `addSub.cancel` (§1.0) | Dismiss |
| `gI9r5` / `gBZ4W` (Claude), `mUvIf` / `Fs6bj` (ChatGPT) option | radio mark, name, recommendation badge, one-line consequence | static per vendor | yes | Select **this** option and deselect the other |
| `uzelE` ToS note | why gateway-supplying a Claude subscription is out of scope | static, Claude only | no | — |
| `iF4LZ` / `O0o5X` hint | how to get the other channel too (Claude) / what the non-recommended choice costs (ChatGPT) | static per vendor | no | — |
| foot | 取消 / 去登录 | selection | yes | Dismiss / start OAuth `[spec §4.5]` |

**Metrics** `[frame]`: dialog 620 wide, `radius 14`, `$--surface`,
`border-strong`; Claude 424 tall, ChatGPT 365. Option card 580 wide `padding 14`
`gap 12` `radius 10`; **selected** = `#5BFFA00F` fill + `#5BFFA059` border, unselected
= `#FFFFFF05` fill; the mark is a 16×16 `radius 999` ring, `stroke $--mint @1.5` when
selected with a `$--mint` dot inside it, `stroke #FFFFFF33` and empty when not. Badge
`padding [3,8]` `radius 999` `#5BFFA01A` / `#5BFFA04D`. ToS note 524 wide
`padding [9,11]` `radius 8`, gold (`#FFC8571A` / `#FFC8574D`), `triangle-alert` icon.
Foot 61 tall, mint 去登录 with `arrow-right`.

**The paste-back exhibit `nOgMQ` registers the step after start** `[frame]`
`[contract]`. It is the third exhibit on frame 04's authoring sheet; the crop contains
only the dialog, and its authoring caption is not product copy. The drawn variant is
`presentation.expects: paste_code`. `paste_callback_url` uses the same geometry and
selects the callback-URL copy below `[derived]`, because the frozen enum changes the
value accepted by the one contracted submit route and does not define a second form.

| Element | Displays | Interactive | On activate |
| --- | --- | --- | --- |
| paste head | The selected vendor and the flow's expected return | close | Dismissing |
| authorization link `[derived]` | The exact non-null `presentation.auth_url` | yes | Open the same provider URL without starting another flow (PD-2) |
| paste input | Code or complete callback URL, selected by `presentation.expects` | yes | Keeps the value locally; empty means 提交 is disabled |
| helper | PD-4's resolved server-declared string; null or lookup failure uses the expects-specific generic hint | no | — |
| 取消 | Leave the flow | yes | Dismissing |
| 提交 | Send the held value | yes when non-empty | `POST /api/models/oauth/submit` with `{flow_id, value}` |

The provider handoff belongs to the start transition, while the visible row is its
same-flow recovery path `[contract]` `[derived]`. PD-1 preallocates the browser context
inside the 去登录 gesture; RR-1 transfers it to the **non-terminal** accepted flow, and PD-2
navigates when that flow first carries `presentation.auth_url` and keeps the exact URL
actionable in the dialog. RR-2 routes an
already-terminal acquisition straight to status and PD-3 closes the unused context. That
is how the user obtains the code or callback URL even when browser popup policy refused the
automatic handoff. The URL is server-declared; the client keeps no vendor-to-**OAuth**-URL
table, and the static top-up/support register above is never consulted for this transition.
Polling, re-rendering or reconciling a submit never **auto-opens** the authorization URL
again; the user may still activate the held link. A submit response is not assumed terminal,
but C11 forbids treating every non-terminal state as the same milestone. E3a `starting` /
`awaiting_action` says the provider still needs the paste value and returns to the form with
that value retained; only the user's next explicit 提交 resends it. E3b `verifying` says the
value was accepted and transfers ownership to the 2s completion poll without resubmission.

The helper is declaration-first as well `[contract]`: PD-4 sends
`presentation.instructions_key` through the existing missing-key-safe resolver. Forms A,
B and C render it directly only when lookup resolves. A null **or unresolved** key falls
back to the generic helper selected by `expects`; the client owns neither a
vendor-to-instruction table nor a second paraphrase of resolved server copy.

**Paste-back geometry** `[frame]`

| Element | Metric |
| --- | --- |
| Dialog | 620 wide, height hugs content, `#0E0E18`, `#FFFFFF24` border, radius 14, shadow `0 16 40 #00000099` |
| Head | `padding [16,20]`, `gap 4`; title 15 / 700; subtitle JetBrains Mono 10.5 / 400 `#9BA3B8B3`; close 15px |
| Body | `padding 20`, `gap 14` |
| Field | label 11.5 / 600 `#FFFFFF8C`; input 36 high, `padding [0,12]`, `#FFFFFF08`, `#FFFFFF14` border, radius 8; value and placeholder JetBrains Mono 11.5 / 400 |
| Helper | `gap 7`; info icon 12px; text 520 wide, Inter 11 / 400, line 17, `#FFFFFF8C` |
| Foot | `padding [14,20]`, `gap 8`, `#FFFFFF05`; buttons `padding [8,14]`, radius 7, labels 12 / 600; submit icon 12px |

The recommendation flips per vendor `[frame]`: Claude = 原生使用 **推荐** /
登录为网关来源 **次选**; ChatGPT = 登录为网关来源 **推荐** / 原生使用
**支持,不推荐**. Frame order follows the recommendation — the recommended option
is first in both dialogs, and it is the one pre-selected.

**The options are a radio group, and one 去登录 produces exactly one effect**
`[frame]`. The frame draws it unambiguously: the mark nodes are named `radio`, the
selected one carries a single `dot` child, the other is an empty ring, and exactly one
is filled per dialog. The copy agrees — 「两条都要也可以,**分两次添加**。」 Two channels
are reachable, sequentially, not in one pass. So:

- implement as `role="radiogroup"` labelled by the dialog title, with the recommended
  option as the initial selection; roving tab-stop, arrow keys move the selection;
- there is no zero-selected state to design for, because the dialog never opens without
  a selection and a radio group cannot be emptied by clicking. 去登录 is therefore
  enabled from the moment the dialog opens;
- on a second pass the already-added channel stays in place and reads as already added
  rather than disappearing `[derived]` — a dialog that silently drops an option looks
  like a different dialog, and the user was told to come back here;
- **and the row that is not choosable is the native one, decided by the slot and not by
  the account** `[contract]`. Before a sign-in this dialog knows which sources exist; it
  does not know who is about to sign in. The one exclusivity the contract states is the
  slot — 「Each backend has at most one `native_cli` Source because its official CLI
  exposes one current login」, said four times in `model-hub.md` — so the native row is
  **present but not choosable** exactly when that backend already holds its one native
  source, whichever account that source holds. The hub row is never disabled: a second
  hub-held account of the same vendor is something §4.1 permits, and disabling it on the
  strength of an existing vendor source would refuse an addition the contract allows. And
  it stays addable all the way through: nothing in the contract refuses a second hub
  account, so no state waits at the end of that path for one. **What *Already bound*
  answers is the native row's condition, not the hub row's** — the slot is a singleton the
  API itself enforces, and this dialog's reading of it is taken when the dialog opens, so
  a source created anywhere else between that read and 去登录 makes the start call the
  first thing that knows;
- the not-choosable row renders with `aria-disabled="true"`, no radio mark of either
  kind, and the arrow keys skip it, so the roving tab-stop has exactly one stop. Whenever
  it is disabled the initial selection is therefore **the remaining option, whatever the
  recommendation says** — pre-selecting a disabled row is the one way to open this dialog
  with 去登录 disabled, and the bullet above depends on that never happening.

This is the item most likely to be implemented from the pixels alone, and getting it
backwards in either direction costs something real: checkboxes would promise a
one-press both-channels action the engine never receives, and a radio group whose
second pass hides the taken option would make the hint's instruction unfollowable.
The disabled-row rule is the same defect one level down — an option that is visible,
carries a mark, and refuses to take it reads as a bug rather than as a record.

**The subtitle is per-vendor copy, not two slots** `[frame]` `[contract]`. The frame draws
claude.ai / Max · Pro and chatgpt.com / Plus · Pro, and this section registered that line
as `{{host}} / {{plans}}` — two promises nothing here can keep. A subscription carries no
`base_url` at all, so there is no entered host to interpolate; and no contract in this set
publishes which plans a channel accepts, so there is no list to join either. Synthesizing
the vendor's **API endpoint** from the remediation register is ruled out: a billing or
support destination says nothing about the endpoint a Source calls, and the two facts may
not substitute for each other. Every other string in this dialog is already static per
vendor — which is what the element inventory says this head is — so
the subtitle is two rows beside `tos.claude` and `hint.chatgpt` rather than a template with
nothing behind it. `{{plans}}` had no second consumer and its §0.9 row goes with it.

**States** — §0.8, rows marked §1.4. It has no Loading state: nothing is fetched
before the dialog opens.

`[derived]`: choosing 登录为网关来源 while the engine is down must fail **before**
the browser hand-off, with 「网关没有响应,请重试」 — sending someone through an
OAuth flow that has nowhere to land is the most expensive possible way to report
that the engine is down.

**去登录 does not mean 「open a browser and wait」; it means 「start the flow and render
what the flow declares」** `[contract]` `[contract-gap]` G-33.
`POST /api/models/oauth/start` answers with an `OAuthFlow`, and `oauth-flow.schema.json`
puts the whole step-2 shape in `presentation`: `auth_url`, `device_code`,
`instructions_key`, and `expects`, which runs `none · paste_code · paste_callback_url`.
The declaration is what the dialog renders — never a vendor→form table on the client,
which is the thing the schema's own description rules out. That description also names
the three compositions it expects, and the middle one is why this reading had to be
split: 「A = auth_url + expects paste_code; **B = auth_url + device_code + expects
none**; C = auth_url + expects paste_callback_url」.

**Presentation-delivery totality** `[contract]` `[derived]`. Declaring a URL or copy key
is not yet delivering it to a person. These rows are shared by create and reauth and apply
to Forms A, B and C; a form consumer that skips one leaves a valid server declaration
unusable:

| ID | Evidence / moment | Browser-context ownership | In-dialog fallback / copy disposition |
| --- | --- | --- | --- |
| PD-1 | A create or reauth flow-producing control is activated while the browser still owns the user gesture | Synchronously open one blank external context **before** awaiting the start/reauth request. The request still runs when popup policy returns no context | Nothing is rendered yet. A refused/failed acquisition closes an unused blank context. A non-terminal flow owns it even while `auth_url` is null, because a later status read may supply the URL |
| PD-2 | Acquisition or a later read of the active non-terminal flow first carries non-null `presentation.auth_url` | Navigate the one PD-1 context to that exact URL. Never derive or rewrite it, and never auto-open another context during polling, submit or reconciliation | Keep the exact URL as a visible actionable link for the lifetime of the active form. It is the manual path when PD-1 was blocked or the external context was closed; activating it opens the same URL without restarting the flow |
| PD-3 | RR-2 receives a terminal flow, or an active flow terminates before PD-2 navigates | Close the unused PD-1 context and status-read or settle immediately; a terminal flow owns no provider handoff | Render no form and no stale authorization link |
| PD-4 | An active form carries `presentation.instructions_key` and `expects` | N/A — this is localized copy, not navigation | Resolve the open-vocabulary key through the existing `serverText` missing-key guard. A resolved key renders directly. A null **or unresolved** key renders the generic helper selected by `expects`: device-code, authorization-code paste or callback-URL paste. Never print the machine key and never use a vendor→instruction table |
| PD-5 | An active paste form in E3a carries `presentation.auth_url: null` | The one held flow and its one PD-1 context own a 2s status read under the existing dialog bound. Keep reading while the required URL is null; never start a second flow or browser context. E2 stays inconclusive, and the evidence-class matrix owns every other answer | Keep the held form and draft. The first read with a non-null URL invokes PD-2 on the original context and pauses this delivery-only poll while E3a still waits for explicit submit |

**`expects` says what comes back, not whether anything is shown** `[contract]`
`[contract-gap]` G-33. `expects: none` means the user
hands no value back to Avibe. It does not mean the flow declared nothing to display:
Form B is `expects: none` carrying a `device_code`, the pattern where the provider's own
page asks for a code that this dialog is the only holder of. So 「the branch that
finishes by itself」 and 「the branch with nothing to render」 are two different sets,
and 04 draws the second: a head, two option cards, a ToS note, a hint and a foot
of 取消 / 去登录, with no code block or copy control. Its non-null
`presentation.instructions_key` still renders in the helper under the declaration-first
rule above, but instructions cannot substitute for the `device_code` value the user must
type. That missing read-only value and copy control are G-33.

**G-33 stays open after the paste-back registration** `[derived]` `[contract-gap]`
G-33. Forms A and C now render the `nOgMQ` field and submit `{flow_id, value}` to
`POST /api/models/oauth/submit`, and every form renders a non-null
`presentation.instructions_key`. Form B still needs a read-only `device_code` the user
copies out, with nothing submitted anywhere; the new input cannot substitute for that
output block. *Awaiting sign-in* is therefore the `expects: none` branch **whose
`presentation` carries no `device_code`**, stated as that branch rather than as all of
OAuth. The schema's own Claude example uses `paste_code`, so the registered paste branch
is not hypothetical while the separate device-code debt remains explicit.

**「Finishes by itself」 is not 「tells us by itself」** `[contract]`. `expects: none` means
the user hands nothing back, not that anything notifies the client — so the completion
this dialog waits for is **read**, not received: `GET /api/models/oauth/status/<flow_id>`
is polled while the flow is non-terminal, at the 2s cadence and under the bound below,
and a terminal `OAuthFlow.state` is what
ends the wait. Without the poll *Awaiting sign-in* is a state with an entry and no exit:
the flow finishes in the browser, the source exists, and this dialog is still sitting on
a button the user already pressed.

**Status-read evidence-class matrix** `[contract]` `[derived]`. This is the one matrix used
by Awaiting sign-in, Awaiting paste-back completion, Paste-back failed reconciliation and
§1.11 reauth. A machine code is classified by what it can prove about this flow, never by
the fact that the server named it:

| Evidence class | Acquisition / status / submit outcome | What it proves | Poll ownership | Intent-specific projection |
| --- | --- | --- | --- | --- |
| E1 — acquisition terminal, tail not yet materialized | Acquisition returns a terminal `flow` without a terminal tail | The producer found a finished flow, but has not materialized or reported its create/repair result | Status-read immediately under RR-2; PD-3 opens no form or `auth_url` | None until the status route returns the complete terminal envelope |
| E2 — inconclusive read | Transport failure, no answer or `engine_down` | Nothing about whether this flow is still pending or has completed; `_oauth_status` can emit `engine_down` for an ordinary adapter outage while pending | Keep the held state; the next 2s tick retries under the same bound | None; neither source list nor source card is changed by evidence about no flow state |
| E3a — action still required | Non-terminal `{flow}` in `starting` or `awaiting_action` | The flow exists and is not done. For a paste presentation, these states prove that the provider still needs the held paste value; they do **not** prove that submit was accepted | An ordinary sign-in keeps its owned form/poll. A paste form with null `auth_url` keeps PD-5's delivery poll; once the URL is delivered it waits without a completion poll. A direct submit or D-36 reconciliation returns to Awaiting paste-back with the value retained; only a later explicit 提交 resends it | None |
| E3b — submitted, verifying | Non-terminal `{flow}` in `verifying` | The flow exists, the provider accepted the paste value when that presentation owns one, and terminal materialization is still pending | Enter or continue Awaiting paste-back completion under the same bounded poll; never resubmit the held value | None |
| E4 — create terminal flow failure | Terminal `failed` / `cancelled`, `flow_expired`, or `flow_not_found` with held `intent: create` | This flow cannot produce a later success terminal; create owns no pre-existing Source subject to reconcile | Stop → OAuth failed | Retry preserves the held create intent |
| E5 — terminal success | Terminal `success` envelope | Authorization and materialization completed | Stop | `flow` supplies terminal state and intent; `create` consumes `source`, `added_to`, `adopted_by`; reauth consumes R3's `source`, `recovered`, `interrupted_pairs` |
| E6 — materialization-only failure | Exactly `discovery_failed` or `migration_item_conflict` in the standard error envelope | This code can arise only after terminal authorization entered materialization, which may already have changed Source state and dependent supply. Conditional `interrupted_pairs` is present and nonempty only when acquisition-stage mutation already produced that exact impact; otherwise it is absent | Stop immediately → OAuth materialization failed; render a present report, then RR-5 reads the attempt scope | R5 consumes every error-envelope member. `create` refreshes the Source list; `reauth` refreshes M3's complete model surface, including same-backend native siblings, before M0 Source-gone precedence. A later read never replaces or reconstructs `interrupted_pairs` |
| E7 — reauth Source absence | `source_not_found` while the held intent is `reauth` | The exact repair subject no longer exists | Stop → M0 / §1.6 Source gone | Drop the repair overlay, retain exact absence, reconcile the complete model surface and never select a lookalike Source |
| E8 — forgotten reauth flow | `flow_not_found` while an acquired flow holds `intent: reauth` | Only that the flow binding is gone; a completed binding can be forgotten after its exact Source is deleted, so this proves neither repair failure nor Source presence | Before failure handoff, run RR-5's M3 complete model-surface read for Hub or native; only a pre-flow acquisition failure outside E8 keeps the narrower RR-6–RR-9 selected-Source read | Source absent → E7 / M0; Source present → OAuth failed in front of the complete reconciled projection; read failure → OAuth failed with read-only RR-5 Retry before any resend. No retry is offered against known absence |
| E9 — reauth terminal failure | `flow_expired` while the held intent is `reauth`, or terminal `failed` / `cancelled` after that reauth producer | The held flow cannot produce a success tail, but that fact does not settle the producer attempt's Source or dependent projections | Run the same RR-5 attempt-scope read as E8 before visible failure handoff | Source absent → E7 / M0; Source present → OAuth failed in front of the reconciled projection; read failure → OAuth failed with RR-5 read-only Retry. No branch treats an empty or missing impact tail as a clean repair |

E2 is bounded by the dialog clock, not by the first outage. E3a/E3b are deliberately
separate progress evidence: `non-terminal` alone can never authorize completion polling
for a paste value the provider still awaits; PD-5 is a presentation-delivery read, not
that completion poll. E6 is the deliberately closed
exception: treating one of its two members as silence can keep polling after
materialization already cleared a reauth Source's discovered supply. No open-ended
「server-named」 bucket is allowed to claim that transition; adding a third E6 member
requires evidence that the code is exclusive to terminal materialization and a change to
this table, while adding any other named failure requires its own evidence-class row.
The intent-specific failure copy says which journey failed, and the refreshed projection
carries what the server now says about the affected source. R5 closes the former G-34
exception: a present `interrupted_pairs` report is rendered exactly once and retained;
an absent member stays absent. No implementation may infer or rebuild the historical
report from the later projection read.

**The clock that bounds it is this dialog's, not the flow's** `[derived]`. What ends a
flow no terminal reading ever comes back for is `OAuthFlow.expires_at` when the flow
carries one, and 15 minutes from `POST /api/models/oauth/start` when it does not.
`oauth-flow.schema.json` admits `expires_at: null` `[contract]`, so a stop condition read
only off that field is missing in precisely the case that needs it: a provider that goes
quiet on a flow with no expiry leaves a dialog polling every 2 seconds with no exit at
all, which is the one shape §0.8 exists to make impossible. A client-side bound is the
smaller of the two available fixes — the other is to contract create flows as always
carrying an expiry, which is a change to `oauth-flow.schema.json` this document has no
authority to make and would still leave every existing null unhandled. Whichever bound
arrives first lands on *OAuth failed*: the same state and the same sentence as a provider
refusal, because 重试 is the same one thing to do and a second message would promise a
second remedy that does not exist.

**A bounded wait ends the waiting, not the flow, so 重试 asks the flow what happened
before it acts** `[derived]` D-32. The two provider terminals are finished when they
arrive — `failed` and `cancelled` leave nothing behind — and this is the first entry into
*OAuth failed* that does not. The bound is this dialog's clock, not a reading: all it
establishes is that no answer arrived inside a deadline this file picked, which is a fact
about the wait and not about the flow. So on this entry 重试 re-reads
`GET /api/models/oauth/status/<flow_id>` once for the flow it is about to abandon, and
the answer decides. `success` is what the extra request is for — the user did sign in,
late, and the `create` terminal is holding the source, so the dialog closes into 06 for
the source that terminal names, D-11's landing, with nothing cancelled and no second flow
started. An unsuccessful terminal reads like the other two entries and leaves nothing to
cancel. Only a still-non-terminal reading, or a read that fails, puts the dialog back
where the bound left it, and that is the branch the cleanup below belongs to.

**The new flow does not wait for cleanup, but cleanup's own read does wait for cancel**
`[derived]`. This paragraph begins only after the read owner has settled: an unread
RR-5 result, including E6 with a mounted `interrupted_pairs` report, exposes only its
read-only Retry and cannot arrive here. Once reconciliation permits a fresh retry, it
launches the acquisition the user asked for and gives the old flow to an F4 background
owner. Within that owner the order is fixed: settle the
authorized `POST /api/models/oauth/cancel` attempt, then re-read the affected projection.
Starting the new flow need not block on that work; reading before the cancel settles is
forbidden because cancel can itself materialize the old flow. If the fresh journey has
already adopted a reusable reauth flow, the ownership check withholds the stale cancel
and still performs the read.

A cleanup failure has no error surface and does not replace 重试 with bookkeeping. The
post-cleanup read still runs, which narrows D-16 from 「the next load eventually tells」 to
「the cleanup owner refreshes after its last possible write」. Create refreshes the list;
reauth refreshes the held Source. A failed cancel may still leave a later completion, but
the spec neither calls that cleanup success nor suppresses the read that can observe the
latest projection.

**This state is read off the flow, and there is no source to read it off** `[contract]`.
It said 「classified `needs_action`」 until this round, which named a real status and put
it in the one place it cannot be: `api.md` states outright that a failed create flow has
no prior `Source` to mark `needs_action`, and its *OAuth completion* rules give a flow
that is non-terminal, failed or canceled the flow object and nothing else — so there is
no source in hand to carry a verdict, and nowhere for the reader to see one if there
were. The entry condition is therefore the flow's own reading, which is exactly the three
unsuccessful exits *Awaiting sign-in* already lists. `needs_action` keeps the meaning it
has everywhere else in this document: a source that exists and needs the user to act.
That is the re-auth path, which §1.1's per-source row already renders
(`sourceDetail.status.needsAction.oauthExpired`) — a grant that lapsed on a source that
was created, not a source that never was.

**And the one sentence must not depend on the cause** `[derived]`. `error_key` is
declared nullable, and `cancelled` — the terminal a user reaches by abandoning the
provider's page — is the one of the three most likely to carry nothing. Per-key copy
would need a fallback for the most ordinary failure here, so the dialog renders one
sentence for all three and offers 重试. That is the reasoning the expiry exit uses one
paragraph above, on the other axis: a second message promises a second remedy, and there
is only one.

**All four failure rows keep the same foot** `[derived]`. The dialog's foot is 取消 /
去登录 (`[frame]`), and a failure replaces the message, not the buttons: 去登录 becomes
重试 and 取消 stays exactly where it was. So each of the four has a way out that binds
nothing — a property worth stating here rather than four times, because the reason is
one reason. The three that are not OAuth failures are the easy ones to forget: an engine
that is down, an account that is already taken, and a start call that came back without
a flow are all conditions the *dialog* cannot fix, and leaving them with only a forward
exit would trap a user behind someone else's state. **That third one is also the reason
*OAuth failed* cannot be stretched to cover it**: its entry condition is a reading of a
flow, its 重试 branches on a `flow_id`, and neither exists when the start call is what
failed — so a fourth row is cheaper than a state that has to check whether its own
subject is there.

**Dismissing while a sign-in is in flight is visually immediate and evidentially
ordered** `[derived]` `[contract]`. 取消, the close icon and a press outside are the one
DP-3 exit: the dialog leaves at once, while RR-10's background owner decides whether it
still owns `POST /api/models/oauth/cancel`. That owner always waits for its cancel attempt
to settle before it re-reads the affected projection. This order is load-bearing: cancel
may itself materialize a terminal create/repair, and a read issued beside or before it
can return the state that existed before that write.

**The cancel call's own failure still does not hold the user** `[derived]`. A non-2xx or
dropped connection renders no cleanup error and offers no retry on a surface that is gone,
but it also does not suppress the later read. Create rereads the Source list; reauth
rereads M3's complete model surface, including every same-backend native sibling an
accepted attempt may already have invalidated. An ownership handoff skips an unauthorized
cancel and performs the same read. The result is the only re-entry rule required: a later
open starts from the projection the model surface now reports, while no late cleanup
response reopens the departed dialog. This does not promise that no flow can outlive a
failed call; it promises that the
latest available authority is read after cleanup has had its chance to write.

**There is no partial-completion state here, and that is a property of the rebuild, not
an omission.** An earlier draft of this section carried a `[contract-gap]` (G-5) for the
outcome when one of two simultaneously-selected channels landed and the other failed.
Single-select removes the case: 去登录 produces one native binding *or* one gateway
source, and a failure is the ordinary OAuth-failed row above. The sequential path the
hint describes is two separate dialogs with two separate outcomes, each atomic on its
own, which is why it needs no transaction model. G-5 is struck in §0.5 and its number is
not reused.

**Copy** — `models.hub.addSub.*`

| Key | 中文 | English |
| --- | --- | --- |
| `title` | 添加 {{vendor}} 订阅 | Add {{vendor}} subscription |
| `subtitle.claude` `[frame]` | claude.ai / Max · Pro | claude.ai / Max · Pro |
| `subtitle.chatgpt` `[frame]` | chatgpt.com / Plus · Pro | chatgpt.com / Plus · Pro |
| `opt.native` | 原生使用 | Use natively |
| `opt.native.desc.claude` `[frame]` | Claude Code 直接用这个订阅,凭据只留在本机。 | Claude Code uses this subscription directly; the credential stays on this machine. |
| `opt.native.desc.chatgpt` `[frame]` | Codex 直接用这个 ChatGPT 账号登录。 | Codex signs in with this ChatGPT account directly. |
| `opt.hub` | 登录为网关来源 | Sign in as a gateway source |
| `opt.hub.desc.claude` | 把这个订阅交给网关,供给 Codex、OpenCode 等其他 Agent。 | Hand this subscription to the gateway so it can supply Codex, OpenCode and other Agents. |
| `opt.hub.desc.chatgpt` | 网关把它供给 Codex 和其他 Agent,用量、额度、接管都能看到。 | The gateway supplies it to Codex and other Agents, with usage, quota and takeover all visible. |
| `badge.recommended` | 推荐 | Recommended |
| `badge.secondary` | 次选 | Second choice |
| `badge.supportedNotRecommended` | 支持,不推荐 | Supported, not recommended |
| `tos.claude` | 订阅条款只授权你本人在 Claude 官方客户端里使用。转供其他 Agent 属于超范围使用,账号可能被限制。 | The subscription terms authorize only you, inside Claude's official clients. Supplying it to other Agents is out-of-scope use and the account may be restricted. |
| `hint.claude` | 两条都要也可以,分两次添加。 | You can have both — add them one at a time. |
| `hint.chatgpt` `[frame]` | 额度用完不会自动接管,也看不到用量。 | Nothing takes over when the quota runs out, and usage stays invisible. |
| `opt.added` `[derived]` | 已添加 | Already added |
| `signIn` | 去登录 | Sign in |
| `cancel` | 取消 | Cancel |
| `retry` `[derived]` | 重试 | Retry |
| `paste.title.code` `[frame]` | 回填授权码 · {{vendor}} 订阅 | Paste authorization code · {{vendor}} subscription |
| `paste.title.callbackUrl` `[derived]` `[contract]` | 回填回跳地址 · {{vendor}} 订阅 | Paste callback URL · {{vendor}} subscription |
| `paste.subtitle` `[frame]` | 登录进行中 | Sign-in in progress |
| `paste.label.code` `[frame]` | 授权码 | Authorization code |
| `paste.label.callbackUrl` `[derived]` `[contract]` | 回跳地址 | Callback URL |
| `paste.placeholder.code` `[frame]` | 粘贴浏览器给你的授权码 | Paste the authorization code from the browser |
| `paste.placeholder.callbackUrl` `[derived]` `[contract]` | 粘贴浏览器回跳后的完整地址 | Paste the complete URL after the browser redirects |
| `paste.hint.code` `[frame]` | 在浏览器完成登录后,页面会给你一个授权码;贴回这里继续。没拿到码就回浏览器重试,或取消后重新发起。 | After signing in in the browser, the page gives you an authorization code. Paste it here to continue. If there is no code, retry in the browser or cancel and start again. |
| `paste.hint.callbackUrl` `[derived]` `[contract]` | 在浏览器完成登录后,复制地址栏里的完整地址;贴回这里继续。没拿到地址就回浏览器重试,或取消后重新发起。 | After signing in in the browser, copy the complete address from the address bar and paste it here. If there is no address, retry in the browser or cancel and start again. |
| `paste.submit` `[frame]` | 提交 | Submit |
| `paste.submitting` `[derived]` | 正在提交… | Submitting… |
| `paste.fail` `[derived]` | 没能确认这次回填已经提交 | Could not confirm the pasted value was submitted |
| `error.oauthFailed` `[derived]` | 登录没有完成。可以重试。 | Sign-in did not complete. You can retry. |
| `error.finalize` `[derived]` | 已完成授权,但创建来源失败。请重试。 | Authorization completed, but the source could not be created. Try again. |
| `error.finalizeReauth` `[derived]` | 已重新登录,但这个来源仍不可用。请重试。 | Sign-in completed, but this source is still unavailable. Try again. |
| `error.startFailed` `[derived]` | 没能确认登录已经开始,可以重试 | Could not confirm the sign-in started — you can retry |
| `error.engineDown` `[derived]` | 网关没有响应,请重试 | The gateway is not responding. Please retry. |
| `error.alreadyBound` `[derived]` | 这个后端的原生登录位已经被占用了。改用网关托管添加。 | This backend's native login slot is already taken. Add it as a gateway-held source instead. |

These error strings are `[derived]`, not `[spec]`: the failure *classes* are the
spec's (§4.5), but no `models.oauth.*` namespace exists in either locale file today,
so this file is where these strings are decided.

**Three of these rows changed when E-4 was ruled, and the change is subtractive.**
`opt.native.desc.claude`, `opt.native.desc.chatgpt` and `hint.chatgpt` each ended in
「不经网关」 as drawn before 2026-08-09. AC-23 forbids a 「『not through Gateway』
explanation」 and the i18n landing row bans mechanism-copy keys; the ruling deleted the
clause from all three and from the frame, and kept every consequence the user actually
needs. What survived is the point: 「额度用完不会自动接管,也看不到用量。」 states the two
things the reader loses without naming the mechanism they lose them to, which is why the
ban costs nothing here. The recommendation still reads as a recommendation. §0.6's E-4
records the reasoning; the rows above are now the strings of record, and the frame
matches them.

**Extreme data** `[derived]`: `{{vendor}}` is this frame's one interpolation, so both
locales must survive a long vendor name without wrapping the head into the body. The
subtitle is not a second one, and reading it as one is what the paragraph above withdrew:
there is no plan list to interpolate, so the extreme case here is a **fixed** localized
string per vendor that must wrap or truncate legibly — never a list to be generated, and
never a revival of the retired `{{plans}}`. Any layout rule stated for it is stated about
a constant. The ToS note is per-vendor content, not a shared component — a second vendor with
a different restriction gets its own string, never a parameterized one, because
paraphrasing somebody's terms of service is not a translation problem.

---

### 1.5 Frame 05 `GDErR` — Add API key

**The question it answers:** *how do I connect any OpenAI/Anthropic-compatible
endpoint — a relay, an aggregator, something I host — with the smallest number of
things I have to know?* Answer: a URL and a key. **Everything else the product
works out for itself, and says so when it cannot.**

Five states are drawn: the left column is the happy path (① default / ready → ②
detecting → ①″ identified → ②″ saving → success destination), the right column is
the three failures (③ unreachable / unauthenticated / wrong address, ④ connected but
the interface is undetermined, ⑤ identified but the inventory did not come back). The
right column is ordered by how far the attempt got before it stopped, which is also the
order in which the product stops refusing: ③ and ④ cannot save at all, ⑤ can.

**2026-09-04 owner ruling — vendor preset + declared protocol** `[derived]`.
This dated block sits on top of detect-then-confirm. Authority:
`docs/plans/model-hub-vendor-preset-protocol.md`. V4 06r already drew the
vendor dropdown; 模型网关 05 is the current-implementation frame and now
carries that field. Do not invent a new dialog.

- Field order is **服务商** → 名称(可选) → Base URL → API Key → 接口类型.
  服务商 is a select: 自定义 · 兼容端点, then the first-wave catalog
  (`deepseek`, `qwen`, `kimi`, `zhipuai`, `openai`, `anthropic`,
  `openrouter`, `groq`, `mistral`, `xai`, `together`, `fireworks`).
- A catalog vendor pins `vendor`, prefills the official Base URL (still
  editable), and **locks** 接口类型 to the catalog protocol with badge
  「内置目录」. The manual disclosure is hidden. 检测 authenticates and
  fetches models; it does not have to prove the interface by shape.
- 自定义 keeps detect-then-confirm. Auto detect still requires matching
  response proof. A concrete disclosure choice is a **declaration**:
  authentication on that path is enough to persist. ④ copy says so.
  Retry stays disabled while Auto is selected.
- Identified mint strip carries 「内置目录」 on a catalog pin and
  「手动指定」 on a declaration. Auto detect has no badge.
- Changing 服务商 resets URL, protocol lock, and any observation.
  Editing Base URL on a preset does not drop the pin.

**2026-09-03 owner ruling — detect-then-confirm** `[derived]`. This dated block
supersedes the origin-axis / 拉取型号 / one-shot 添加 material later in this section.
The dialog is now a single add flow:

- After 服务商, field order is 名称(可选) → Base URL → API Key → 接口类型. Interface type is a
  **detection-result area**, not a required input, except when a catalog
  vendor has locked it. Idle is a dimmed row naming
  whatever 检测 is about to send — 自动探测 with `addKey.protocol.idleHint`, or the
  chosen interface with its glyph and no hint, because only Auto identifies
  automatically; Ready is the same row undimmed; Detecting is a spinner with
  `addKey.protocol.detecting`; Identified is a mint strip naming the established protocol
  (protocol-family glyph before the label) and the model count — never the model
  names.
- Primary is two-step: **检测 / Detect** runs the non-persisting observe; **确认添加 /
  Confirm & add** persists with the established protocol. There is no optional 拉取型号
  button, so the pull/add origin axis has no remaining work and its primed twins are
  retired. ⑤'s 仍要添加 stays, gated on an inventory outcome with an established protocol
  rather than on origin.
- On 自定义, manual override is a collapsed disclosure `addKey.protocol.manual`. A concrete
  selection is a declaration, not a probe constraint. In ④ the selector is expanded
  in place; retry stays disabled while Auto is selected. The collapsed row and the
  expanded selector are one selection rendered two ways, so exactly one of them is on
  screen: expanding replaces the row, and the pressed segment is then the statement.
- Cancel while detecting aborts the probe and returns to the form with values intact.
  Cancel during persist stays blocked.
- Every surface that names a **concrete** interface type carries that protocol
  family's brand glyph (Anthropic mark for `anthropic`; OpenAI mark for both
  `openai_responses` and `openai_chat`). Auto detect has no glyph. Glyphs live in
  `protocolGlyph.tsx` and do not touch `vendorMeta.ts`.
- The persisting invariant is: every persisting exit requires a protocol
  established on the 2026-09-04 ladder (catalog pin, user declaration, or
  matching response proof). ③ never persists. ④ never persists from Auto;
  a concrete declaration that authenticates leaves ④ through ② into ①″.
- Evidence retires with the inputs it was derived from. Editing Base URL, the API
  key or the protocol selection returns **any** state whose primary would persist
  without observing again to ① Ready — ①″'s report, ⑤'s waived inventory, and the
  protocol a refused or unanswered create still holds. That is the property, stated
  over how a state exits rather than over the list of states that exist today. A
  state whose primary re-observes (③, ④, ⑤'s 重试, ⑥) keeps its line instead: that
  retry reads the fields as they now stand. The display name is in no observation, so
  ①″ survives a rename; it is in the create request, so a server-named refusal of
  that request does not survive one. Two of the covered states offer no edit to fire
  the rule — ⑤ renders no form and ⑦ locks every field — and that is why the rule is
  written over exits: whether a state happens to expose an editable field today
  decides only whether it fires, never whether it applies.

**2026-09-04 owner ruling — no text in this dialog inherits the document's default
type scale** `[derived]`. Manual verification found the idle/ready row's
active-interface statement rendering at 16px, larger than the 15px dialog title,
because the element carrying that statement declared only layout. Its scale is
12 / 600 — `--model-hub-add-key-strip-title-size`, already the detecting row's and
the identified strip's scale — while the 自动探测 hint beside it keeps the 10.5
field-hint scale. Stated as the property rather than as that one row: **every text
node in this dialog declares its own size.** The element that owns a statement is
the element that sizes it, which is why the two-scale idle row sizes each child
instead of the row, and why a row added later is measured by the rule rather than by
whether it appears in the Metrics list below.

**2026-09-04 owner ruling — wherever this dialog states the interface, it states why
it is settled** `[derived]`. ① Default above registers 「内置目录」
(`addKey.protocol.catalogPinned`) as the badge on a catalog pin. Two keys extend that
rule to the cases ① does not cover, and neither introduces a new state:

- `addKey.protocol.declared` — 「手动指定」, the same badge shape on the other answer
  that is not "the response proved it": a concrete interface chosen from the 自定义
  disclosure. It rides the ready row and both result strips, exactly where
  `catalogPinned` does. 自动探测 carries no badge, because there the response shape
  IS the evidence and the identified strip already names what it found.
- `addKey.protocol.catalogPinned.hint` — what 检测 still establishes under a pin
  (authenticate the key, fetch the model list), replacing `addKey.protocol.idleHint`
  on the ready row. The two cannot share one string: `idleHint` promises the
  interface will be identified automatically, which under a pin is neither true nor
  still in question.

Stated as the property rather than as three call sites: **an interface this dialog did
not detect says where it came from, and the row that says it also says what detection
is still for.**

**Element inventory**

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| head sub-line | that Add detects the connection and interface first, then confirms | static | no | — |
| 服务商 + hint | 自定义 · 兼容端点 first, then one option per shipped catalog row in file order, by its label; the hint says a catalog pick prefills the official address and pins the interface | `vibe/data/api_key_vendors.json`, read by this dialog and the server from the one tracked file | yes | Select a catalog vendor → Base URL becomes its `official_base_url` (still editable), 接口类型 locks to its protocol and the disclosure is gone, any observation is retired. Select 自定义 → Base URL clears and the disclosure returns |
| `f7Ao1U` 名称(可选) | free text | user | yes | — |
| `cXsiv` Base URL + hint | free text; the hint names an API root, and says a bare host uses the standard `/v1` path | user | yes | — |
| `mZBBw` API Key | masked value, reveal icon | user | yes | Toggle reveal |
| Interface type | result area last: idle/ready row naming the interface 检测 will send, detecting spinner, identified mint strip, catalog-locked row, or ④'s gold strip; protocol-family glyphs on every concrete protocol name | observation result; catalog pin; disclosure selection is a declaration on 自定义 | disclosure yes on 自定义; ④ selector yes | Open `addKey.protocol.manual`, which replaces the row with the selector; a concrete choice declares the next 检测 / ④ 重试 |
| `S0pOY2` 检测 / 确认添加 | 检测 while there is no fresh result; 确认添加 after ①″ | form validity | yes | 检测 → ②; 确认添加 → ②″ with the established protocol |
| `OT0Xf` state ② | spinner, 识别接口中… | in-flight observe | 取消 only | Abort; return to ① Ready with the form intact |
| persist strip state ②″ | spinner, 保存中…, 正在保存这个供应商 | in-flight persist | none — cancel blocked | — |
| `C72yS` state ③ strip | classified outcome copy | observation result | no | — |
| `EJrDH` ③ foot | 取消 / 重试 | — | yes | Dismiss / re-run the explicit observation |
| `vKiIo` state ④ strip | connected, interface undetermined | observation result | no | — |
| `WZyA8` selector | the same four interface choices, expanded in place, glyphs on concrete options | selection | yes, Auto detect remains selected until changed | Select one concrete interface; enables 重试 |
| `Nak7y` ④ foot | 取消 / 重试 (dimmed until a concrete interface is picked) | selection | yes | — |
| `d6bFlX` state ⑤ strip | the interface *was* identified, and the model list did not come back | observation result | no | — |
| `x0Gzg` ⑤ foot | 取消 / 仍要添加 / 重试 — **three** buttons, the only foot in the product with three | — | yes | Dismiss / save the source without an inventory / rerun the entire observation |
| `sqZa9` success note | that the dialog closes straight into 06 | static | no | — |

**Metrics** `[frame]`: dialog 560 wide, height auto in all five states — the frame
sets no fixed height, so a build that pins one is deviating, not matching. Head
`padding [16,20]` `gap 4`; body `padding 20` `gap 14`; field `gap 6`; input 520×36
`radius 8` fill `#FFFFFF08`; field hint 10.5 JetBrains Mono `#9BA3B8B3`. Protocol-family
glyphs are ~14px `currentColor`. Result strip 520 wide `padding [11,13]` `gap 10`
`radius 9`: red `#FF6B6B14`/`#FF6B6B40` for ③, gold `#FFC85714`/`#FFC85759` for ④
**and for ⑤** (`AFl3g`), mint `#5BFFA014`/`#5BFFA040` for the success note. The
form-level selector uses `padding 3` `gap 3` on `#FFFFFF0A`/`$--border`; unselected
segments fill `#00000000`, while the selected Auto or concrete segment uses the mint
success fill and ink. Foot `padding [14,20]` `gap 8` on `#FFFFFF05`, top border; buttons
`padding [8,14]` `gap 6`. State ⑤ (`d6bFlX`, 560×148) is the same dialog shell with
one strip (`uKZuq` 560×87 → `AFl3g` 520×59 → `EbcxN` `triangle-alert` + `LePtp`
title/detail) and a three-button foot (`x0Gzg` 560×61 → `SvK44` 取消, `wouXZ`
仍要添加, `o8K7m` 重试); it carries no field, because ⑤ asks the user for nothing.

**Three of those fills carry state policy, not styling.** Auto detect is selected on the
initial form. In ④, 重试 stays dimmed and disabled only while the selector remains Auto;
a concrete interface selected before or after the ambiguous attempt is shown as selected
and enables the full-mint retry. State ②'s in-flight primary and ⑤'s whole-observation
retry retain their drawn dimmed mint, while ③'s retry is full `$--mint`. A build that
defaults the initial form to a concrete interface, enables ④ while it still reads Auto,
or hides a concrete selection that produced the latest attempt has implemented a
different decision rather than a cosmetic variation.

**States** — §0.8, rows marked §1.5. Two of them are absences worth stating: this
dialog has no Loading state, because nothing is fetched before it opens, and no Empty
state, because a form has none.

**Every state past the form consumes the one non-persisting observation route**
`[contract]`. `POST /api/models/sources/observe` returns `SourceObservation` and no
credential reference or persisted Source. The dialog consumes only its contracted
outcome, reachability, authentication, protocol, discovery and models facts. It does not
reconstruct the removed request/status/reason evidence slots from adapter logs or HTTP
details.

**Detect observes before Confirm persists, and that ordering is what makes ③, ④ and ⑤ offers
rather than notifications** `[contract]`. Each of them asks the user for a
decision — retry, pick a concrete protocol, add anyway — and a decision offered after the row is
already stored is not a decision; ⑤ is the clearest case, because 仍要添加 is only an
offer if nothing was added when the inventory came back unusable. So 检测 is the mandatory
non-persisting observation, and `POST /api/models/sources` goes out on one of three paths,
all of them past a consent: 确认添加 from ①″, ④'s retry once a concrete selection identified
the protocol and the user then confirmed, and ⑤'s 仍要添加. An earlier version had the
creation call go first and the diagnosis come back from it, which needed the persisting
route to answer non-terminally — a response no contract gives it — and left every 重试 in
this dialog re-adding a source that already existed. The seam this draws is the one G-19
registers: before the POST the transient credential is revoked on every settlement AC-26
names, and after it the source exists and its questions belong to 06.

**The origin axis is retired** `[derived]`. Until 2026-09-03 this dialog had two producers
of the same probe — 添加 and optional 拉取型号 — and every outcome was primed as an
add/pull twin so that a pull could never persist. Detect is now mandatory and Confirm is
the only persisting primary, so there is no optional pull branch and no remaining work for
those twins. ①′ / ②′ / ③′ / ④′ / ⑤′ / ⑥′ are tombstones. ⑤'s 仍要添加 is gated on an
established protocol owner with unavailable inventory, not on origin.

Cancel is no longer an origin-dependent fork. ②'s 取消 aborts the in-flight observe and
returns to ① Ready with the form intact — a real abort, not a dismissal — and a second
detect cannot start while one is in flight. ②″ blocks cancel for the G-19 persist
boundary. Outcome-state 取消 dismisses. A cancelled in-flight observe revokes the
independently provisioned transient ref on every way AC-26 names (success, failure,
adapter error, timeout, cancellation), including the durable pending-revocation record
when the revoke itself fails. What a cancel yields once Confirm has crossed into
persistence is the contracted boundary: before durable commit, cleanup completes before
cancellation would have settled, except this dialog does not offer that cancel; after
commit, cancellation ends only this caller's wait and the next Source/Agent read claims
the committed result.

**② occupies the protocol result area rather than replacing the form** `[derived]`. The
period while Detect is pending renders `addKey.protocol.detecting` with a spinner in that
area, and the primary is the same detecting label, disabled. Every word of the older
`addKey.adding` / `addKey.adding.detail` pair described a one-shot add that observed and
persisted in one press; those keys remain in the locale files and are not this state's
copy.

**A successful Detect lands in ①″, not back in ① and not in a persist** `[derived]`. ①″ is
the form exactly as ① Ready renders it, plus the mint identified strip: protocol-family
glyph, established protocol label, and `addKey.pull.result` or `addKey.pull.empty`. It is also
where ④ lands when the manual selection identifies the interface with discovery succeeded,
and where ⑤'s 重试 lands when the repeated observation comes back usable. Nothing was
persisted, so 确认添加 from here still runs Source-create's repeated observation and reuses
none of the client report as proof. Editing Base URL, API Key, or the protocol selection
drops the report, because a count is a fact about the address it was fetched from. And 取消
dismisses with nothing to abort — the in-flight abort belongs to ②, which is where the
request still is.

**The form-level protocol selector on 自定义 is a declaration, not a probe constraint**
`[frame]` `[contract]`. The 2026-09-04 owner ruling supersedes the 2026-08-26
shape-only sentence and the 2026-09-03 "probe constraint, never proof" sentence.
Auto detect remains the default on 自定义 and still requires matching response
proof. A concrete choice on the next 检测 or ④ 重试 is persisted after
authentication on that path; shape proof is not required. A catalog vendor
hides this selector and pins protocol from the shipped table.

State ④ keeps the same selector visible — expanded in place, not behind the disclosure —
and requires a concrete choice before 重试. The primary is still a retry, not a save, and
the strip always describes the latest attempt. A successful retry with discovery succeeded
lands ①″ and still persists nothing. The stored Source records the established protocol
but no manual/automatic provenance marker: the distinction matters during preflight, not
during later invocation. Saved protocol changes still require a new Source.

**State ⑤ is the one place in the product that saves something it could not fully
verify, and the rule that makes it safe is a property, not a permission** `[frame]`
`[contract]`. E-5 was raised because no frame drew this state; it is now drawn
(`d6bFlX`), and the ruling that closed it states the invariant the whole dialog
enforces:

> 已保存的来源恒有一个已建立且有归属的协议;凡拿不到这个归属的路径,产物都是「没有添加成功」。

Read that as a partition of the four ways an add can end, and every button on this
screen falls out of it. ③ and ④ are the paths where the protocol owner was **not**
established, so both refuse — that is E-3's gate, and ⑤ does not reopen it, because
⑤ is downstream of the gate rather than around it. ⑤ is the path where the protocol
*was* established by one rung of the ladder — catalog pin, concrete `custom`
declaration, or matching response proof — and a *different* result, the model
inventory, did not arrive. AC-27 at `ca45aeb6` puts the same thing from the
contract's side: 「『Add anyway』 is available only after the protocol was
established and a different result, such as model inventory, remains unavailable;
that uncertainty is a health fact.」 An unknown inventory is a fact a saved source
can carry; an unknown protocol owner is not, because every later request depends on
it.

That is why 仍要添加 exists here and nowhere else, and the frame says so in its own
caption: 「全产品唯一一处「仍要添加」」. It is also why the state carries **no field**.
④ asks the user for a concrete interface because the product is missing something the user might
know; ⑤ asks for nothing, because the user cannot supply a model list. The only
question ⑤ puts to a person is whether to keep the connection they just established.

**The three-button foot is the shape of that question, and the ink partition holds
through it** `[frame]`. `x0Gzg` carries 取消 (`SvK44`), 仍要添加 (`wouXZ`) and 重试
(`o8K7m`) — the only three-button foot in the product. 重试 is the nominal continue
and wears the dimmed mint for the reason given above. 仍要添加 is the deliberate,
less-common choice, and it is drawn as **outline chrome** — `#FFFFFF0A` fill,
`$--border` stroke, label `$--foreground` — not in an advisory ink. An earlier draft
of this frame tinted that label gold to mark it as the cautious path. That would have
made gold a *control* ink, which §1.0's partition forbids outright, and it would have
done it one paragraph after this file asserts the partition has no holes. The
distinction ⑤ needs is between *primary* and *secondary*, which is chrome's job;
advisory inks describe the state of the world, and the world's state here is already
said once, by the gold strip above.

**The property this state produces is an equality, not a pixel**: the set of dialog exits
that persist a source equals the set whose protocol has a named established owner. A
build that lets ③ or ④ save, and a build that refuses ⑤, break the same equality from
opposite sides — which is why it is worth stating as one.

**The persisting body is the complete `SourceCreate` schema, including
`accept_unavailable_inventory`** `[contract]`. The frozen schema
carries required `vendor` and write-only `key`,
optional `display_name`, normalized `base_url`, the selected `protocol` when
present, and the generated `client_nonce`. It
carries no protocol conclusion or discovered inventory: the server repeats the same
observation inside the create attempt and owns the stored protocol,
models and health. Thus ④'s Auto path still cannot save, a concrete ④ declaration
is authority once authenticated, and ⑤'s
仍要添加 is the only producer that sets `accept_unavailable_inventory: true`;
the clean path sends false or omits it and may not commit when that server-side
observation again establishes the protocol owner while discovery remains unavailable. The
flag never supplies or overrides observation evidence, is inert on successful discovery,
and cannot bypass reachability, authentication or protocol establishment. The response is
`{source, added_to, adopted_by}`;
the plaintext key and nonce reservation never become routing inputs.

**The reveal control is named by its action, and the value's state is the input's**
`[derived]`. `mZBBw` draws a masked value with a reveal icon, and an icon-only control
needs a name in both locales rather than one a screen reader infers from a glyph. Two
keys carry it — `field.apiKey.reveal` and `field.apiKey.conceal` — and the name swaps
with the state, so the control always says what pressing it does. It is deliberately
**not** an `aria-pressed` toggle: 「显示 API Key,已按下」 leaves the listener to work out
whether *pressed* means the key is showing or the showing is armed, and those two
readings differ by exactly the fact being asked about. The value's state is already
carried losslessly by the field itself, which stops being a password input while the key
is revealed; the button carries the verb.

**Copy** — `models.hub.addKey.*`

| Key | 中文 | English |
| --- | --- | --- |
| `title` | 添加 API Key | Add API key |
| `subtitle` | 先检测连接与接口，确认后添加 | Detect the connection and interface first, then confirm to add |
| `field.name` | 名称(可选) | Name (optional) |
| `field.protocol` | 接口类型 | Interface type |
| `field.protocol.hint` | 不确定时用自动探测；已知类型时可直接声明所选接口。该路径鉴权成功即可添加；选错了运行时可能失败，而且保存后不可更改 | Use Auto detect when unsure; choose a type to declare that interface. If authentication succeeds it can be added, a wrong choice can fail at runtime, and the saved type cannot be changed |
| `field.baseUrl` | Base URL | Base URL |
| `field.baseUrl.hint` | 填写 API 根地址，例如 https://api.example.com/v1；只填域名时使用标准 /v1 路径 | API root, such as https://api.example.com/v1; a bare host uses the standard /v1 path |
| `field.apiKey` | API Key | API key |
| `field.apiKey.reveal` | 显示 API Key | Show API key |
| `field.apiKey.conceal` | 隐藏 API Key | Hide API key |
| `pull.result_one` | 拉到 {{count}} 个模型 | Fetched {{count}} model |
| `pull.result_other` | 拉到 {{count}} 个模型 | Fetched {{count}} models |
| `pull.empty` | 已经连接，但这个供应商没有可用模型 | Connected, but this source lists no models |
| `detect` | 检测 | Detect |
| `confirm` | 确认添加 | Confirm & add |
| `saving` | 保存中… | Saving… |
| `saving.detail` | 正在保存这个供应商 | Saving this source |
| `fail.subtitle` | 认出接口是「添加」的前置条件 · 先按下面这条修,再重试 | Identifying the interface is a precondition of Add · fix what the line below names, then retry |
| `fail.auth` | 上游拒绝了这个凭据 | The upstream rejected this credential |
| `fail.auth.detail` | 检查 API Key、接口类型和 Base URL 的 API 根路径 | Check the API key, interface type, and the Base URL's API root path |
| `fail.address` `[derived]` | 无法连接这个地址 | Could not reach this address |
| `fail.network` `[derived]` | 连接超时 | The connection timed out |
| `fail.unclassified` `[derived]` | 响应无法归类 | The response could not be classified |
| `fail.engineDown` `[derived]` | 网关没有响应,请重试 | The gateway is not responding — try again |
| `fail.save` `[derived]` | 没能确认这个来源已经保存 | The source is not confirmed saved |
| `fail.inProgress` `[derived]` | 创建仍在进行,可稍后重试 | Creation is still in progress. Try again later. |
| `retry` | 重试 | Retry |
| `undetermined.title` | 连上了 —— 但认不出它说哪种接口 | Connected — but we cannot tell which interface it speaks |
| `undetermined.detail` | 返回结构对不上任何一种已知接口。请选择一个明确类型后重试：只要该路径鉴权成功即可添加；但选错了运行时可能失败，而且保存后不可更改 | The response shape matches no interface we know. Choose a concrete type and retry: if authentication succeeds on that path it can be added, but a wrong choice can fail at runtime and the saved type cannot be changed |
| `protocol.auto` | 自动探测 | Auto detect |
| `protocol.anthropicMessages` | Anthropic Messages | Anthropic Messages |
| `protocol.openaiResponses` | OpenAI Responses | OpenAI Responses |
| `protocol.openaiChatCompletions` | OpenAI Chat Completions | OpenAI Chat Completions |
| `protocol.idleHint` | 填好 Base URL 和 API Key 后自动识别 | Identified automatically once Base URL and API key are filled |
| `protocol.detecting` | 识别接口中… | Identifying the interface… |
| `protocol.manual` | 手动指定接口类型 | Manually specify interface type |
| `inventory.title` | 接口已经认出来了 —— 但这次没拿到它的型号清单 | The interface is identified — but its model list did not come back this time |
| `inventory.detail` | 接口已经确认;这次型号清单读取失败 | The interface is confirmed; the model list could not be read this time |
| `addAnyway` | 仍要添加 | Add anyway |
| `success.title` | 成功 → 弹窗关闭,直接进入「来源详情 · 型号管理」 | Done → the dialog closes and you land on Source details · Models |
| `success.detail` | 型号列表、重新拉取、推理强度档位都在那里维护 | The model list, refetch and reasoning tiers are all maintained there |
| `cancel` | 取消 | Cancel |

**Observation evidence slots are retired, not left nullable** `[contract]` AC-49.
`SourceObservation` carries only outcome, reachability, authentication, protocol,
discovery and models; it has no request, status or reason producer. Therefore
`addKey.undetermined.detail` and `addKey.inventory.detail` are static failure statements,
and the former `addKey.inventory.reason.*` family is a tombstone with no wire producer.
No separator or placeholder is conditionally reconstructed from transport details.

**`fail.subtitle` is shared by every cause, which is why it names none of them**
`[derived]`. State ③ renders exactly one of `fail.auth` / `fail.address` /
`fail.network` / `fail.unclassified` underneath it, selected only by
`authentication_failed` / `unreachable` / `timeout` / `adapter_error`. Only the first
is about a credential. A subtitle that names the
credential is right one time in four and actively misleading the rest: it sends a user
with a typo in the base URL off to regenerate a key that was never the problem. It points
at whichever outcome-safe line the observation produced instead, which is the same thing
`fail.auth.detail` does one level down — the specific advice lives with the specific
cause.

**`fail.unclassified` is the fourth line that keeps the failure set total** `[derived]`.
`SourceObservation` has six terminal outcomes. `observed` and `ambiguous` leave ③ for
their own states; the four remaining outcomes map one-for-one here. `adapter_error` is
the contract's own name for an upstream result the adapter could not classify, and it is
neither authentication rejection, unreachable nor timeout; without the residual line a
build either files it under one of those,
which reports a cause that was never observed, or renders ③ with an empty cause slot on a
frame whose subtitle promises one. 响应无法归类 says only what the evidence proves and
remains true for both contracted forms: a shaped server/rate-limit response with
`reachable: true`, and a local adapter failure with `reachable: null`. The exit is
unchanged, because 重试 re-runs the whole observation and never resumes from a
classification.

The three protocol strings above are the **only** protocol names anywhere in the
product surface. They are identifiers, identical in both locales, and they
are exactly the three transports the protocol enum admits `[contract]` AC-28 — the
label 「OpenAI Chat Completions」 maps to `openai_chat`.

`inventory.detail` keeps the one fact that distinguishes ⑤ from ④ — and therefore the
one fact that licenses 仍要添加: the interface is known and only discovery failed.
It deliberately names no request, status or cause, because `discovery: failed` is a bare
enum and cannot support finer evidence. The sentence is static in both locales.

**`undetermined.hint` used to contradict AC-27, then stated the 2026-08-26
shape-only rule, and now states the 2026-09-04 ladder.** The string the
frame drew originally — 「选一种才会保存 · 之后可在来源详情里改」 — promised the choice
was changeable later. At `ca45aeb6`, AC-27 said the opposite: after Save the stored
protocol is preserved byte-for-byte through retest, discovery, refresh, credential and
Base-URL replacement and restart, and 「changing protocol requires a new Source」; FC-12
`PATCH /api/models/sources/<source_id>` `[contract]` carries metadata only — frame 11
now registers that editor in §1.10 — and has no protocol field at all, so there is no request that could
change one. The 2026-09-03 string ended 「保存后不可更改」 and still called the
choice a probe constraint. The 2026-09-04 string keeps immutability after Save
and names the declaration: authentication on the chosen path is enough; a wrong
declaration fails on a later real call. **E-2 remains closed; this string is still
its whole surface.** The other
half of that conflict was an instruction to put a protocol-edit entry point on frame
06's interface badge; the ruling deleted the badge instead, so there is no longer a
place in the product where a stored protocol is displayed as changeable, and §1.6
specifies neither the badge nor its tooltip (see §0.6). One sentence on this screen now
carries the entire rule, which is the right number of places for it: the moment a user
can act on the protocol is the only moment the constraint is worth reading.

**Extreme data** `[derived]`: the evidence line is mono and truncates from the
middle, keeping method+host and the status code; a base URL with no scheme is
normalized to `https://` before the probe and the normalized form is what the
field shows afterwards (so the user can see what was actually tried).

**An omitted 名称 is filled at submit time, not at render time** `[derived]`. If the
field is blank when 确认添加 is pressed, the client sets `display_name` to the URL host
before the request goes out, so the persisted value *is* the host. The rejected
alternative was a render-time fallback, and it is worth naming why it is wrong:
`display_name` is a required non-null string `[contract]` FC-03 and creation fills an
omitted name from the vendor, so a details page receiving `custom` cannot tell
whether the user typed it or left the field empty. A fallback that depends on
information the payload does not carry is a fallback that will eventually lie — the
same principle as D-3. The user sees the host in the name field as soon as it is
filled, so the value they get is the value they were shown.

A host can be longer than `display_name` accepts — `source.schema.json` bounds the
field and owns the number — so the fill is **truncated from the left with a leading
ellipsis, keeping the tail**, and the truncated value is what lands in the field before
the request goes out. Keeping the tail keeps the registrable domain, which is the part
that says whose source this is; a long host is nearly always long in its subdomains. The
alternative — letting the client send the untruncated host and the server reject it —
would turn a name the user never typed into a validation error they cannot act on. The
rule holds for the user-typed case too: the field is bounded at input, so no path can
compose a name the contract will not take.

---

### 1.6 Frame 06 `wItw4` — Source detail · model management

**The question it answers:** *which models does this one source have, which of
them do I actually want, and what reasoning tiers does each accept?* Nothing else
— chain membership is set on 01/02, ordering on 03.

**Element inventory**

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| `iGcAi` back icon | icon only; `sourceDetail.back` is its accessible name in both locales `[derived]` | route | yes | Return to 01 |
| `sugad` source bar | 36×36 identity tile, source name, `provider or endpoint host · established protocol` identity pill, **state dot + state label** (使用中 in the drawn state) + 型号列表更新于 {{time}}, mono `host · N 个型号` | source state `[spec §4.5]` | capability-gated 重新拉取 / 添加模型 / source overflow `[frame 11]` | Refetch / append an editable row / open 编辑来源 · 移除来源 |
| 重新拉取 action capability `[derived]` | `sourceDetail.action.refetch` | `Source.supply_channel` | render for `hub`; do not render for `native_cli` | A Hub Source → Refetching. A native Source has no stored credential for this route to validate, so it exposes no activation |
| `myA8k` header | 型号 ID (250) · 录入 (84) · 推理强度 (470, with info) · fill spacer | static | no | — |
| `OM5PH` row | model id, entry-kind pill, tier chips, overflow icon | one model | tiers, overflow | Edit tiers / row menu |
| Retired discovered-row delta `[derived]` | same `OM5PH` chrome, muted ink, existing tag component with `sourceDetail.entry.retired`; never a new row component | `origin: discovered`, `retired: true` `[contract]` | no delete or reactivation action | Remains readable and non-supplying; refetch preserves it |
| `p2JwTz` tiers | chips, or 未设置档位; `+ 添加档位` is drawn only while a pointer is on the row, in the box it reserves either way, and the row answers that hover with `--model-hub-wash-0a` fill and nothing else — no border, no height, no reflow; a write this row rolled back keeps its 重试 here `[derived]` | `reasoning_efforts[]` `[contract]` FC-03 | the whole cell, by pointer, focus or tap — the cell's box is the row band rather than the chips it happens to hold, so a device with no hover draws no pill and still has the same target | Enter edit mode; retry the rolled-back write |
| `eVavA` tiers (editing) | removable chips + text input + 回车添加 · 任意文本 + ghost suggestion chips derived from the source's established protocol (the OpenAI protocols offer `low` `medium` `high` `xhigh`; Anthropic Messages names no tier vocabulary and offers none) + one muted line stating that tiers are sent as typed and that a model with none is still routable `[derived]` | local edit → `PATCH /api/models/sources/<source_id>/models/<model_id>` `[contract]`; suggestions are presentation only — never persisted, never pre-filled at discovery, never sent | yes; one row is in this state at a time, and the state ends when focus leaves the editor — Escape (which hands focus back to the row) or focus moving anywhere outside it, including another row's cell — so every control inside it is reachable by keyboard and by pointer alike; a rolled-back write is not held here, it stays with its row `p2JwTz` | Add / remove a tier; a suggestion adds through the same path typing it would take; retry a rolled-back write |
| `nN4TZ` manual row | editable id input, 手动添加 pill, tier affordance, 取消 / 添加 | local draft → `POST /api/models/sources/<source_id>/models` `[contract]` | yes | Commit or discard |
| `Q83BF` add row | 添加模型 + when to use it | — | yes | Append a manual draft row |
| `tF3Bh` footnote | scope of this page; that tiers are yours to type; that the interface type is confirmed at add time, shown, and not editable here | static | no | — |

**The back icon is named rather than inferred** `[derived]`. `iGcAi` is this page's only
route back to 01 and draws no text, so `sourceDetail.back` carries its accessible name in
both locales — the same treatment `field.apiKey.reveal` gets one frame over and
`shell.modelsInfo.label` gets on 01, for the same reason: a glyph is not a name, and an
icon-only control whose destination cannot be heard is a control a keyboard or
screen-reader user cannot use. It names the destination and not the gesture —
返回来源列表 rather than 返回 — because *back* on a page reached from several places says
only that something precedes it, and this control goes to 01 whichever way 06 was opened.

**There is no per-model on/off on this page, and that is the design** `[frame]`. An
earlier version of this section described an 接入 column with a toggle per row; the
frame has neither — three columns and a fill spacer, no switch anywhere. The reason
is D-9: a model's participation is decided by the routing chain that resolves it, so a second
per-model boolean here would be a second owner of the same fact, and the two would
disagree the first time a chain changed. What the page owns is the *inventory* —
which models this source has, and what tiers each accepts.

**What a row's overflow menu offers is measured, and the contract is now broader than
that drawing** `[frame]` `[contract-gap]` G-3. Frame 06 carries
`sourceDetail.row.remove` only on manual rows and no removal item on discovered rows.
The contracted guarded DELETE now accepts either: manual deletes the row; discovered
persists `retired: true`, remains readable and never supplies. Therefore `origin` cannot
be used as a capability predicate for the route, and the missing discovered-row producer
cannot be explained as server impossibility. This specification preserves the measured
manual-only menu and registers the absent producer; it does not add an unframed control
or advertise the existing route as unreachable. It does register the producer's durable
projection: a retired discovered row keeps `OM5PH`'s chrome, uses muted ink, and appends
the existing tag component with `sourceDetail.entry.retired`. It has no discovered-row
delete or reactivation action, remains readable but never supplies, and no refetch may
clear the tombstone `[contract]`.

**G-3's route and tombstone are closed; its frame affordance is not.** Refresh never
revives a retired row, and the shared guards run before either delete outcome
`[contract]`. A future frame can register the discovered-row consumer against that
complete route without another contract change.

**Omitting the discovered-retirement control from this frame still requires one reach
property** `[derived]`. A source may broadcast a large catalogue, and this frame draws no
retirement consumer for those rows. What has to hold is about reach, not about controls:

> However many models a source broadcasts, the number of actions a user needs to reach a
> particular model does not grow linearly with the size of the set.

How that is satisfied is an implementation choice — search, grouping, recent use, or
something else. This file deliberately names no control for it: naming one would turn a
property into a fixture and freeze the weakest implementation that happens to pass.

**The removal confirmation is one surface, and every guarded change uses it** `[frame]`.
`Qp6FI` is 520 wide, `$--surface`, `radius 14`, `$--border-strong`, with the standard
outer shadow. Head `kCVJB` `padding [16,20]` `gap 4` with a bottom border: title `R4NNdG`
Inter 15 / 700 naming the exact operation (「从 aihub 移除 ernie-5.0」), subtitle `I8g2k`
JetBrains Mono 10.5 `$--muted` naming the scope, close `eT9Sn` 15px `#FFFFFF59`. Body
`UnP1t` `padding 20` `gap 14` carries the three parts of the guard envelope in order: a
label `flV8I` (Inter 10.5 / 700 `#FFFFFF73`, letter-spacing 1.1) with a count pill
`ZnpYd` (`#FFFFFF0A`, `radius 999`, `$--border`, `padding [3,8]`, Inter 10 / 600
`$--muted`); the affected hops `smTsO` (`$--background`, `radius 10`, `$--border`,
`gap 6`, `padding 8`), one 52-high row each (`#FFFFFF03`, `radius 8`, `$--border`,
`padding [0,10]`, `gap 10`) showing the hop's model line at Inter 12 / 600 `#F5F1E8B3`,
its consequence at JetBrains Mono 10.5 `#9BA3B8B3`, and its position as a pill; and a
hint `uAs5V` (13px `info` `#FFFFFF59` + Inter 11.5 `#FFFFFF73`, width 420, line-height
1.5) stating whether anything is left without supply. Foot `P9Sxv8` `#FFFFFF05` with a
top border, `padding [14,20]` `gap 8`, right-aligned: 取消 `FijDU` in the neutral
treatment, and the forcing action `QRZGe` in the destructive one (`#FF6B6B1A`, stroke
`#FF6B6B59`, label `$--destructive`).

**The position pill consumes `RouteHopRef.position` directly** `[contract]`.
`guard.hop.position` renders 顺序 #{{n}} and frame 11's
`guard.hop.position.removeSource` renders 第 {{n}} 跳 from the refusal row's required
one-based pre-mutation position. The UI neither treats array order as chain position nor
issues a per-chain lookup. `{{n}}` is therefore always present whenever the hop row is
rendered.

That structure is not specific to removing a model, and the surface is not either. Frame
11 proves the same shell at source grain for Base-URL edits and source removal. It is the
guarded-change refusal rendered once `[spec §4.5]`: the count pill and the row list
are the hops the change would remove, the hint line is `would_interrupt`, and the
destructive button is the `force` re-send. What opens it are requests that persist, not
the gestures that precede them. The two inventory mutations this page already sent are
`POST /api/models/sources/<source_id>/refresh` and
`DELETE /api/models/sources/<source_id>/models/<model_id>`; frame 11 adds guarded
`PATCH /api/models/sources/<source_id>` and `DELETE /api/models/sources/<source_id>`
and frame 12 adds `PUT /api/models/sources/<source_id>/credential` `[contract]` — with
the title naming the operation and the rows naming the hops. Which operations are guarded at all is
§4.5's matrix, and this page does not restate it: an enumeration written twice is two
things to keep in step, and the copy that fell behind is exactly how a caller withdrawn
from the matrix went on being listed here. A second surface for the same refusal would be
the same failure one layer down — a second reading of what `would_interrupt` means, and
the two would disagree the first time it grew.

The 移出 in §1.3's drawer is not a caller, because it is not yet a request. It moves a row
between two sections of an open drawer and sends nothing; 取消 discards it, and only
保存顺序 persists the order and applies the reorder operation in one request. That request
has no guard, so no confirmation is needed. A guard hung on the click would fire
on an edit the user can still take back, and would not fire on the edit that actually
lands. The two unguarded model operations do not come here either: adding an entry and
editing an entry's tiers touch neither an entry's identity nor any chain that references
it, so there is nothing for a guard to refuse and a confirm would be ceremony (§0.5).

The matrix in §4.5 names the removed-hops field `would_remove_hops`, and at `ceace07f`
`model-hub-contracts/api.md` carries exactly that name in the shared refusal envelope.
The matrix is also **exhaustive**, which is what settles the question for any request this
document does not draw: a mutation the matrix does not mark guarded cannot be refused this
way, and one it does gets this surface wherever it is eventually drawn. What this surface
depends on is not the spelling: it is that the refusal carries the hops and a supply-loss
flag, and the shape of this dialog follows from those two regardless of what they end up
being called.

**Copy** — `models.hub.guard.*`. The surface is shared, so its strings live in their own
namespace rather than in either caller's. The parts that describe the refusal are the
same in every direction — they render the same envelope — and only the strings that name
the operation vary by caller. That split is the copy-level form of the same rule the
geometry follows: **one surface, one reading of `would_interrupt`; each operation names
only what it is about to do.**

The old frame-only `hint.removeSource` statement about retaining emptied manual
chains is superseded by Empty Route Inheritance. It is not a production locale key
and requires no new consumer. Current guard hints use the existing
`settings.models.guard.hint.safe` / `settings.models.guard.hint.interrupt` keys and
the recomputed inherited impact; a final-hop removal never forces an empty Manual state.

| Key | 中文 | English |
| --- | --- | --- |
| `label` | 会被移除的跳 | Hops that will be removed |
| `label.removeSource` `[frame]` | 移除后会一起消失的跳 | Hops that disappear with this source |
| `count_one` | {{count}} 跳 | {{count}} hop |
| `count_other` | {{count}} 跳 | {{count}} hops |
| `hop.position` | 顺序 #{{n}} | Order #{{n}} |
| `hop.position.removeSource` `[frame]` | 第 {{n}} 跳 | Hop {{n}} |
| `hint.safe` | 这些型号还有别的来源可用。 | These models still have another source available. |
| `hint.interrupt` | 有型号会因此没有可用来源。 | Some models will be left with no usable source. |
| `gap.label` `[derived]` | 会失去可用来源的型号 | Models that will be left with no source |
| `gap.subject` `[derived]` | {{backend}} · {{menuModel}} | {{backend}} · {{menuModel}} |
| `gap.agents` `[derived]` | 已指定它的 Agent:{{agents}} | Agents pinned to it: {{agents}} |
| `cancel` | 取消 | Cancel |
| `title.removeModel` | 从 {{source}} 移除 {{model}} | Remove {{model}} from {{source}} |
| `subtitle.removeModel` | 这个型号会从这个来源的清单里消失 | This model disappears from this source's inventory |
| `confirm.removeModel` | 仍要移除 | Remove anyway |
| `title.refetch` | 重新拉取 {{source}} 的型号 | Refetch models from {{source}} |
| `subtitle.refetch` | 这次拉取会让部分型号从这个来源的清单里消失 | This refetch drops models from this source's inventory |
| `confirm.refetch` | 仍要拉取 | Refetch anyway |
| `title.editSource` `[derived]` | 保存 {{source}} 的来源修改 | Save source changes for {{source}} |
| `subtitle.editSource` `[derived]` | 新的 Base URL 会让部分型号从这个来源的清单里消失 | The new Base URL drops models from this source's inventory |
| `confirm.editSource` `[derived]` | 仍要保存 | Save anyway |
| `title.replaceKey` `[derived]` `[contract]` | 更换 {{source}} 的 Key | Replace key for {{source}} |
| `subtitle.replaceKey` `[derived]` `[contract]` | 新 Key 会让部分型号从这个来源的清单里消失 | The new key drops models from this source's inventory |
| `confirm.replaceKey` `[derived]` | 仍要更换 | Replace anyway |
| `title.removeSource` `[frame]` | 移除来源 {{source}} | Remove source {{source}} |
| `confirm.removeSource` `[frame]` | 移除来源 | Remove source |

**The hint line answers 有没有 and the refusal also carries 哪些** `[derived]`
`[contract]` `[contract-gap]` **G-23**. `would_interrupt` is not a flag. It is a
`SupplyGap[]`, each entry naming `backend`, `model_id` and `agents`, where `model_id` is
the **menu** model — 「the protected identifier is always the menu model, never a hop's upstream
`model_id`」 — and `agents` is the enabled named Vibe Agents whose explicit model is that
one, 「present and may be empty」 `[contract]`. `model-hub.md` then requires the confirm to
use it: 「the confirm copy names affected Agents when any exist」. A single sentence saying
some models will be left without a source satisfies none of that — it tells a user that
something they configured is about to stop working and withholds which thing, on the one
surface whose whole purpose is to let them decide.

So the strings are specified here and the element that would hold them is not.
`guard.gap.label` heads the list, `guard.gap.subject` is one entry, `guard.gap.agents` is
the line under an entry whose `agents` is non-empty — and the count pill beside the label
is `gateway.modelCount`, reused verbatim rather than given a second spelling in this
namespace, the same reuse §1.6 makes of it. What no frame draws is the block: `Qp6FI` as
measured has one label, one count pill, one row list and one hint line, all of them the
hop side. Specifying the copy is this document's own register and the authority requires
it; specifying a second body block would be this document drawing (§0.2), so the drawn
element is a gap and the number carries it.

**Which block renders is decided by which array is non-empty, not by which error leads**
`[contract]`. Both callers on this page run both guards — `model-hub.md` lists 「explicit
refresh/recovery, and manual-model deletion」 among the mutations that stage the inventory
and 「run **both** guards」 — so `source_model_in_route_chain` and `source_last_supplier`
are both readings this dialog can receive. The refusal envelope carries both arrays either
way, and 「when both apply, the exact-hop error leads and the response still carries both
complete arrays」. A body that keyed off the error code would therefore drop half of a
double refusal, and would render an empty hop list under `source_last_supplier`, whose own
example in `api.md` has `would_remove_hops: []` beside a populated `would_interrupt`. Each
block renders when its array has entries and is absent when it does not; a refusal
populates at least one of them, because an empty pair is a refusal with nothing to refuse.
The hint line is unchanged and stays a summary: `hint.safe` when `would_interrupt` is
empty, `hint.interrupt` when it is not — the same fact the list states in detail, kept
because a user scanning the dialog reads the sentence before the rows.

**The source-level callers do not revive the deleted order-save caller.** An earlier
version of this table carried a `*.saveOrder`
triple whose subtitle read 「移出的来源会从这个后端的所有路由链里消失」. §1.3 no longer
starts this surface, because the order save is not a guarded mutation (§1.3, and
`model-hub.md` §4.5's matrix, which is exhaustive and does not list that route), and the
three strings went with the state that cited them. The sentence is worth recording as
deleted rather than quietly dropped: it was the most specific claim anywhere in this file
about what saving an order does, it was false, and it was phrased as a *warning*, which
is the register a reader trusts most.

The per-hop line under each model id states which chain the hop sits in — the row's data,
not a string this table owns. **It does not state a mode.** The earlier drawing put a
consequence like 「…仍是「自定义」」 there, which was a sentence about the follow/custom
pair S-1 deleted; a hop belongs to a configured chain and there is no second kind.

**Metrics** `[frame]`: source bar `fill_container` `padding [14,18]` `gap 14`
`radius 12` `$--surface` / `$--border`, identity tile 36×36 `radius 9`, status row =
5px dot + 12/500 (`$--mint` as drawn — the ink is the state's, see below) + 11
`#FFFFFF8C`, mono line 10.5 JetBrains Mono
`#9BA3B8B3`, both labelled actions 95 wide (添加模型 `$--mint`); source overflow `b4_more`
is 33 wide with `padding [8,9]`, `#FFFFFF0A`, radius 7 and a 14px ellipsis. Table `fill_container`
`radius 12` `$--surface`; header `padding [10,18]` `gap 16` fill `#FFFFFF05`, labels
11/600 `#FFFFFF73`; row `padding [11,18]` `gap 16` with a bottom border; column
widths **250 / 84 / 470 / fill** and they must match the header exactly. Tier chip
`padding [4,9]` `radius 999` fill `#FFFFFF0F` stroke `$--border`, 10.5/500
`$--foreground`; the `+ 档位` chip is the same shell with a transparent fill,
`#FFFFFF24` stroke and `$--muted` ink; empty label 11 `#FFFFFF59`; editing input
`radius 999` fill `#5BFFA00F` stroke `$--mint`, removable chips `padding [4,8]`
`gap 5` with a 10px ✕. Manual draft row is mint-washed (`#5BFFA00D`, `#5BFFA033` top
and bottom) with a 250×32 `radius 7` input. Overflow icon 16px `#FFFFFF59`. Footnote
11.5 `#FFFFFF8C`.

**The bar's dot and label are the source's state, not a constant** — `[frame]` for the
state the frame draws, `[derived]` for the rest. The frame draws a supplying source, so
it shows a `$--mint` dot and 使用中. But a source can be on standby, cooling or
`needs_action` when its detail page is opened — `needs_action` most of all, because a
dead credential is *why* someone comes here — and a bar hard-coded to 使用中 tells that
user their key is fine on the one screen they opened to fix it. So the dot and the label
both derive from the source's state, in the vocabulary D-21 already fixes for state
text:

| `Source.state.status` `[contract]` | Ink | Key |
| --- | --- | --- |
| `active` or `standby`, and `adopted_by` is non-empty `[contract]` | `$--mint` | `sourceDetail.status.inUse` (detail) / `upstream.state.supplying` (card) |
| `active` or `standby`, and `adopted_by` is empty `[contract]` | `$--muted` | `upstream.state.standby` |
| `cooldown` | `$--gold` | `upstream.state.unavailableRetry`, or `upstream.state.unavailableDue` once `retry_at` has passed `[derived]` |
| `needs_action` | `#FF6B6B` | the `sourceDetail.status.needsAction.*` row `state.detail_key` selects `[derived]` `[contract]` |
| `error` | `#FF6B6B` | `sourceDetail.status.error` `[derived]` |

The split of the two healthy statuses is the one place this bar reads a second field: 使用中
claims a Hub-mode backend has this source configured into a route, which is `adopted_by` (§1.0), not a source state. A
source can be perfectly healthy and be in nobody's chain, and saying 使用中 there would be
the same lie as saying it about a dead credential, in the flattering direction.

What `adopted_by` does **not** answer is whether traffic is flowing through it at this
instant. `api.md` calls it 「the stable Source-card projection of persisted references」,
and stable is the operative word: a reference persists across a cooldown, a revoked
credential and a takeover that routes past this hop entirely. So 使用中 is a statement
about configuration, and the copy is written to be true of a standby hop that a chain
still points at. A card that promised live supply would need the per-chain runnability
read, which is a different projection D-28 keeps separate — and it would go stale the
moment a hop it named entered cooldown. Unadopted `standby` and unadopted `active`
land on one word deliberately — they differ in *why* nothing is drawing from the source,
and this bar is not where that difference is actionable.

**Every Source read carries that second field** `[contract]`. The bar consumes the
complete server-derived `adopted_by` array from `GET /api/models/sources`, exactly as
§1.0's card does. It never reads chains backwards into attribution (D-28) or relies on a
creation response retained in client state. The fact is registered once and cited twice
so both consumers select the same active/in-use split.

**`needs_action` is four causes, not one, and the row above resolves to whichever of them
the payload carries** `[contract]`. `state.detail_key` is required on this state and its
enum runs `models.source.needs_action.oauth_expired`,
`models.source.needs_action.balance_exhausted`,
`models.source.needs_action.credential_revoked`,
`models.source.needs_action.account_banned` — four causes with four different remedies, of
which only one is about a credential being wrong. One label reading 凭据失效 was therefore
false on three of them, and false in the expensive direction: it sends a user whose balance
ran out to re-enter a key that is fine. So the four have four strings, selected by the key
the payload already carries. What this does **not** do is enumerate the ways a *request*
can fail — that set is closed at F1–F5 and stays closed. `detail_key` is a field on the
source, persisted and read back, and rendering a field the contract requires is not the
same act as inventing a taxonomy for a transport error.

The states this bar shares with the upstream card reuse the card's keys unchanged, and
the two rose states are worded here because no frame words them anywhere; the card
renders **these** keys rather than wording them a second time. Two vocabularies for one
state is how they drift apart — and until this round the rule was broken in the place it
was written: the `upstream.state.*` family carried its own needs-action and error rows,
word for word, which is exactly the second vocabulary this paragraph forbids. Those two
rows are gone and §0.8's §1.1 rows cite the keys declared here, so 凭据失效 could not
survive in one table after being retired in the other. There is no key for *paused supply*: `legend.unavailable`
belongs to frame 08's relation legend, where gold says a wire stopped carrying, and a
source's own state is what §4.5 enumerates — a bar that borrowed the relation's word
would report a fact about a link as a fact about the source. The
`sourceDetail.status.listUpdated` suffix survives every state untouched: it reports the
inventory's age, not the source's health, and a dead credential does not make an
already-fetched list any older.

It is keyed on its field rather than on the state, and that is also what decides the one
case where it is **not** drawn. `last_discovered_at` is required and nullable
`[contract]`, and null carries a specific meaning: no successful discovery has ever
completed for this source. A key that failed on its very first fetch arrives here with
an empty table and no timestamp. The suffix is then omitted entirely and the line ends
at the state label — not filled with 未知, and not with `created_at`, which is a
different fact wearing this one's clothes. What that user needs is an explanation of the
empty table, and the empty table is where this file puts it.

**The mapping is total over `state.status`, and `error` is the row that makes it so**
`[spec §4.5]`. §1.1's state table already names a per-source `error` — an unclassified
failure, the residual class the four named ones do not cover — and a source in it can be
opened here like any other. A table that stopped at `needs_action` would leave the bar
undefined on a reachable state, and the drawn state is 使用中, so the undefined case
falls through to *this source is fine* on the one screen the user opened because it is
not. That is the same failure the paragraph above rejects for `needs_action`, one class
further out. `error` therefore takes rose like `needs_action` — both mean *a person has
to act* — but a **different word**, because they need different acts: a `needs_action`
source names its cause and the act that clears it, while 异常 deliberately claims no
cause. Naming a cause the product
did not classify would be worse than admitting it has none; what the user can still do is
drawn either way. A Hub Source keeps 重新拉取 in every state; a `native_cli` Source uses
its credential repair action and never renders the refetch action registered above.

**Credential repair has one owner, and it is frame 12's card** `[frame]` `[contract]`.
A second upstream-contacting action on this page would be exactly the duplication the
重新拉取 rule below forbids: the bar states the cause, the table stays visible, and
`iGcAi` returns to the card whose 重新授权 or 更换 Key action §1.11 registers. This keeps
refresh/recovery and credential replacement separate without leaving either route ownerless.

**The 录入 pill is a second witness for D-19's neutral pair** `[frame]`. 自动拉取
renders `#FFFFFF0A` / `$--border` / `$--muted`; 手动添加 renders `#FFFFFF14` /
`$--border` / `$--foreground`. Same shell, one step of contrast, and the brighter one
is the one the user put there. Neither pill
takes an accent hue, because 录入 records *where a row came from*, not whether
anything is wrong with it. D-19's pair is one rule with two renderings, stated once
rather than per frame: one shared meaning, two independent instances, and a mismatch in
either one is a defect in both.

**Deviation, not yet resolved** `[frame]`. The model-id text in `wzfF1` renders at
`$--foreground` on four rows and `#FFFFFF59` on nine, interleaved. It matches no row
property — not entry kind (the manually added row is dim), not tier count, not
position — so it is authoring drift, and the rule this spec states is the uniform
one: **model ids render at a single ink across all rows**. It is recorded here rather
than silently normalized because the frame is owner-approved and this lane does not
edit approved frames to make a document true; §0.2's authority order settles numbers,
not unexplained variance. Same handling as D-23's legend-swatch alpha. Raised in the
PR description for the owner's call.

**States** — §0.8, rows marked §1.6. The two commit states that send a request
(`Tier commit`, `Manual commit`), the two that send a guarded one (`Refetching`,
`Removing a manual entry`) and the two refusals those come back to (`Refetch refused`,
`Guard refused`) are separate rows there from the editing states that precede them,
because an edit that has not been sent cannot fail and an edit that has been sent can.

Five rules:

- **Refetch preserves what the user authored, and a model it stops advertising is
  marked where the user chooses models — never silently skipped** `[derived]`
  `[contract]`. Manually added models survive a refetch, and AC-26 requires rediscovery
  to preserve the user-edited `reasoning_efforts` list plus `display_name` and
  `discovered_at` `[contract]` — all stored fields, so all survive. The harder case is a
  model a chain still *references* that a **successful** refetch no longer returns. This
  table is the discovered set, so its row goes; what must not happen is that the chain
  quietly stops working. **On this page it cannot, and the reason is that 重新拉取 is
  guarded** — §0.8 has said so in three rows all along. `model-hub.md` §4.5 runs the
  staged inventory past both guards before committing it: if an exact configured hop would
  cease to be callable the non-forced call is **refused** with `source_model_in_route_chain`
  and ordered `would_remove_hops`, and 「another Source supplying the same menu model does
  not make that exact reference disposable」; if no exact hop is lost but a protected route
  loses its last supplier it is refused with `source_last_supplier`; when both apply the
  exact-hop error leads and both arrays are still carried. That refusal is *Refetch
  refused*, drawn in `Qp6FI` with `guard.hint.safe` / `guard.hint.interrupt` naming which
  of the two the user is looking at — and, once the frame grows the block G-23 registers,
  with `guard.gap.*` naming the models and Agents behind that sentence. 仍要拉取 re-sends
  with `force`, and the contract is equally explicit about what that does: it 「applies the inventory change and removes only
  those invalidated hops in one transaction, preserving the identity and relative order of
  all surviving nonempty manual arrays」. Removing a final hop drops its override key
  and evaluates inheritance before reporting every effective removal and resulting gap —
  `force` is confirmation, not a claim that the change is interruption-free.

  **The guards protect exact effective impact, not an empty-disable state.** Source
  mutations use the shared exact plan, while nonempty manual PUT retains its visible
  noninterrupting-removal exception. Empty PUT/DELETE use Restore guards. §4.6 says the
  same thing from the storage side: a hop's live reason stays visible 「until the Source
  recovers, the user removes or changes the pair, **or a guarded cascade removes it**」.
  Until this round that sentence was quoted here without its last clause, and the missing
  clause was exactly this case — which is how a page whose own register already routed
  重新拉取 through the guard could also promise that a refetch retains the hop. The
  retention promise is real but it belongs to the other caller: **automatic background
  discovery** never performs the cascade, records the model `model_unsupported`, keeps the
  hop visible and non-runnable, and waits for an explicit refresh or edit `[contract]`.

  What survives unchanged is the rule the earlier text was written to protect, and it was
  never about hops: **一条链引用的型号,若在所有来源上都不再供应,必须在型号菜单上可见地
  标出,不得静默跳过。** The row remains in frame 01's ordered model list and the explicit
  disclosure keeps it reachable beyond the six-row prefix. §4.4's
  `model_supply.has_runnable_hop: false` selects its marker rather than its collapse
  position. A nonempty chain renders `legend.unavailable` in its existing
  current-text slot; `chain_length: 0` branches first to the existing
  `models.launch.route_unconfigured` family. The projection
  follows the actual inherited plan after a cascade removes the last manual hop. Its
  catalog row survives normalization; an empty inherited plan is Unconfigured, while a
  nonempty inherited plan shows its automatic/passthrough origin and live status.
  **No stale row is invented on frame 06.** Frame 01 is the registered model-row
  consumer; the Source inventory remains only the Source's current inventory.

  *What that costs, said here too because this is the page where it is felt.* The source a
  model came from is exactly what this table used to tell you, and after the refetch it no
  longer can: the row is gone with no trace that it was ever here. On the forced path the
  hop goes with it, so the confirm is the **only** place the user ever sees the list of
  references they are giving up — which is why this page adds no second notice afterwards.
  The success envelope carries `removed_hops` and `interrupted`, and both re-state what
  `Qp6FI` just showed; announcing them again would report a surprise where the user had
  already been asked. On the unforced paths the trail is longer instead of shorter: a user
  diagnosing 「无来源可供」 reads the marker on the menu, opens the model's chain, and finds
  a retained hop naming a source whose inventory no longer lists that id — three surfaces
  to answer a question one page could have answered if the inventory remembered its own
  past. The trade is deliberate: nobody gets silently unconfigured by a vendor changing
  `/models`, and the price is that the explanation is assembled rather than read. The
  separate discovered-model retirement route persists its own tombstone; this refetch
  rule neither substitutes for nor closes G-3's still-undrawn discovered-row producer.
- **Empty state** `[derived]` keeps the header row and the add row and shows whichever
  of `sourceDetail.empty` / `sourceDetail.emptyNeverFetched` the source's
  `last_discovered_at` selects. An empty table with a live add row is the shortest path
  out, which is why both strings end on an action.

  *The table decides the state; the stamp only picks which of the two empty strings it
  shows.* Both are empty-table sentences — 这个来源没有返回型号 and 还没有成功拉取过这个来源
  的型号列表…可以…手动添加一个型号 — and 添加模型 writes a row without touching
  `last_discovered_at`, which is a discovery timestamp and not a table timestamp. So a
  source saved through §1.5's 仍要添加 and then populated by hand holds a null stamp above
  a table with models in it. Selecting on the stamp alone reads that source as Never
  fetched and tells the user to add a model, above the model they added; selecting on the
  table first cannot, because a non-empty table is Ready however its rows got there. What
  such a source loses is only the age line, and §0.9's absence rule already drops it.
- **Needs action** `[derived]` keeps the whole table visible and
  read-only-ish: you can still see what you had configured. Hiding the inventory
  because the credential stopped working destroys the only copy of the user's intent.
- **Every source-bound mutation on this page or in its frame-11/frame-12 overlays can
  answer that its subject is gone, and one state absorbs all of them** `[contract]`
  `[derived]`. `source_not_found` is in `api.md`'s minimum error set and every route these
  surfaces send takes the source id in its path, so a tier edit, manual add/removal,
  refetch, source edit/removal, reauthorization and key replacement can each come back with it —
  the source having been deleted from another tab, another API client, or a guarded
  cascade while 06 sat open. Four F1 rows each inventing a sentence for it would be four
  ways of saying the same thing, and each would leave the user on a page whose subject
  does not exist: the retry those rows offer cannot succeed, which is the dead control
  D-9a rules out. So the reading is promoted out of the treatments — any of them
  answering `source_not_found` lands *Source gone*, whose only affordance is the way
  back to a list that is still true. §0.8 declares that dispatch once, with precedence
  over F1/F3, for §1.6, §1.10 and §1.11; their caller rows do not restate it.
- **重新拉取 is the only action here that contacts the upstream, and the contract has
  only one to draw** `[frame]` `[contract]` AC-26. A Hub Source draws 重新拉取 beside
  添加模型; a `native_cli` Source draws only 添加模型. No source draws a connectivity test,
  and of the Hub Source's two actions only 重新拉取 leaves the
  machine; 添加模型 writes a row the user typed. 05's 拉取型号 and 06's 重新拉取 are the
  same operation at two moments, which is why they share a verb and neither is called a
  test.
  AC-26 was amended on 2026-08-09 to **one saved mutation**, and `api.md` makes that
  mutation `POST /api/models/sources/<id>/refresh` — the only refresh/recovery route,
  runnable against a source in `needs_action` or `error` precisely so that it can recover
  one `[contract]`. Recovery is therefore not a control this page is missing; it is what
  pressing 重新拉取 on a blocked Hub Source already does, which is also why the action stays
  enabled in every Hub state above. A `native_cli` Source has no stored credential for this
  route to validate and therefore does not render it. The only probe the contract exposes
  is backend-scoped and walks a chain, so it is not a thing a source page can offer at all. A 测试连接 control
  here would re-create the second upstream-contacting operation the amendment deleted, and
  would immediately owe an answer for what its verdict means when the refresh that follows
  disagrees.
- **The tier control form is this file's call** `[contract]`. AC-26 fixes the data
  (`reasoning_efforts: string[]`, editable for discovered and manual models alike, no
  default item, no prefill, no selected state) and then explicitly defers the control
  form to `design.pen`. So the chips-plus-freetext-input treatment in the metrics
  above is normative, and 「未设置档位」 is the real empty state rather than a
  synthesized default — see D-5.

  **Adding a tier and removing one are the same mutation** `[contract]` D-31. The route
  「replaces the complete capability list」, so the request body is the whole
  `reasoning_efforts` array the row should end up with, never a delta and never a
  per-chip operation — pressing Enter sends the list plus the typed tier, pressing a
  chip's × sends the list minus that chip, and removing the last one sends `[]`, which
  is how a row gets back to 「未设置档位」. That is why §0.8 gives the two one `Tier
  commit` row instead of two: one route, one body shape, one failure treatment, one
  string. Both directions are optimistic in the same way — the chip appears or
  disappears on Enter or ×, and F1 restores the pre-request list on rejection rather
  than leaving the row showing a list the server never accepted. The only asymmetry is
  where the undone edit is put back: an add returns its text to the input, where the
  user was already typing, and a removal returns its chip to the row, in its original
  position. The list is ordered and this route replaces it wholesale, so a rejected
  removal that re-appended the chip would silently reorder the tiers as the price of a
  failed request.

**Copy** — `models.hub.sourceDetail.*`

| Key | 中文 | English |
| --- | --- | --- |
| `back` `[derived]` | 返回来源列表 | Back to sources |
| `status.inUse` | 使用中 | In use |
| `status.needsAction.oauthExpired` `[derived]` `[contract]` | 需要重新登录 | Sign in again |
| `status.needsAction.balanceExhausted` `[derived]` `[contract]` | 余额用完 | Out of balance |
| `status.needsAction.credentialRevoked` `[derived]` `[contract]` | 凭据被吊销 | Credential revoked |
| `status.needsAction.accountBanned` `[derived]` `[contract]` | 账号被限制 | Account restricted |
| `status.error` `[derived]` | 异常 | Error |
| `status.listUpdated` | · 型号列表更新于 {{time}} | · model list updated {{time}} |
| `summary_one` | {{host}} · {{count}} 个型号 | {{host}} · {{count}} model |
| `summary_other` | {{host}} · {{count}} 个型号 | {{host}} · {{count}} models |
| `action.refetch` | 重新拉取 | Refetch |
| `action.editSource` `[frame]` | 编辑来源 | Edit source |
| `action.removeSource` `[frame]` | 移除来源 | Remove source |
| `edit.title` `[frame]` | 编辑来源 {{source}} | Edit source {{source}} |
| `edit.name` `[frame]` | 显示名称 | Display name |
| `edit.baseUrl` `[frame]` | Base URL | Base URL |
| `edit.hint` `[derived]` `[contract]` | 协议已在添加时建立,不在这里改;改显示名称不会影响型号与路由链。改地址时会先检查供给影响。 | The protocol was established during add and cannot be changed here. Renaming does not affect models or routes; changing the address first checks its supply impact. |
| `edit.cancel` `[frame]` | 取消 | Cancel |
| `edit.save` `[frame]` | 保存 | Save |
| `edit.saving` `[derived]` | 正在保存… | Saving… |
| `edit.fail` `[derived]` | 没能确认来源修改已经保存 | Could not confirm the source changes were saved |
| `remove.checking` `[derived]` | 正在检查移除来源的影响… | Checking the impact of removing the source… |
| `remove.fail` `[derived]` | 没能确认这个来源已经移除 | Could not confirm the source was removed |
| `refetch.added` `[derived]` | 新增 | New |
| `refetch.removed_one` `[derived]` | 本次拉取移除了 {{count}} 个型号:{{models}} | This fetch removed {{count}} model: {{models}} |
| `refetch.removed_other` `[derived]` | 本次拉取移除了 {{count}} 个型号:{{models}} | This fetch removed {{count}} models: {{models}} |
| `refetch.unchangedOnly` `[derived]` | 本次拉取没有变化 | Nothing changed in this fetch |
| `action.addModel` | 添加模型 | Add model |
| `row.remove` `[derived]` | 移除 | Remove |
| `fail.refetch` `[derived]` | 拉取没有回来,下面是这一页最后一次真的读到的列表 | The fetch did not come back. The list below is the last one this page actually read |
| `fail.tier` `[derived]` | 档位没保存上 | The tier was not saved |
| `fail.addModel` `[derived]` | 这个型号没添加上 | The model was not added |
| `fail.removeModel` `[derived]` | 这个型号没移除掉 | The model was not removed |
| `gone` `[derived]` | 这个来源已经不在了 | This source no longer exists |
| `retry` `[derived]` | 重试 | Try again |
| `col.id` | 型号 ID | Model ID |
| `col.entry` | 录入 | Entry |
| `col.tiers` | 推理强度 | Reasoning tiers |
| `entry.auto` | 自动拉取 | Auto-fetched |
| `entry.manual` | 手动添加 | Added manually |
| `entry.retired` `[derived]` | 已退役 | Retired |
| `tiers.empty` | 未设置档位 | No tiers set |
| `tiers.addFirst` | + 添加档位 | + Add tier |
| `tiers.add` | + 档位 | + Tier |
| `tiers.inputHint` | 回车添加 · 任意文本 | Enter to add · any text |
| `addRow.hint` | 拉取不到、或只想接入其中一个时用 | Use this when a model is not discoverable, or when you only want one of them |
| `empty` `[derived]` | 这个来源没有返回型号。可以手动添加,或重新拉取。 | This source returned no models. Add one by hand, or refetch. |
| `emptyNeverFetched` `[derived]` | 还没有成功拉取过这个来源的型号列表。可以按上面状态里写的那条先处理,再拉一次,或者手动添加一个型号。 | This source's model list has never come back. Deal with whatever the status above reports, fetch again, or add a model by hand. |
| `footnote` | 这里只管「这个来源有哪些型号」。型号走哪条路由链,在网关模块里改。档位自己填,两种录入方式都一样。接口类型在添加时确认并固定，页面会显示、但不能修改。 | This page answers only "which models does this source have". Which routing chain a model takes is set in the gateway module. Tiers are yours to type, the same for both entry kinds. The interface type is confirmed when the source is added, shown here, and not editable. |

**The four discovery results are rendered at two different grains, and one of them has
no row to land on** `[derived]` `[spec §4.2]`. `model-hub.md` requires discovery to render
「added, removed, unchanged, and failed results」, and this page is the only saved-Source
surface where discovery runs. *Failed* is not part of this state at all: a classified
failure preserves the previous list and returns the normal safe error `[contract]`, which
is *Refetch failed* and its own copy. The other three are one fetch's diff, and
the diff is a client-side comparison — the refresh answers with the complete updated
source and this page still holds the list it was rendering, so nothing has to be
reconstructed from the discovered count, which `api.md` forbids anyway. *Added* is
row-grain and takes a tag on the new rows, `refetch.added`. *Unchanged* is the residue and
is rendered by saying nothing about those rows — a tag on every untouched row is noise the
user has to read past to find the two that moved — except in the one case where the residue
is the whole answer, which `refetch.unchangedOnly` states outright so a fetch that did
nothing does not look like a fetch that did not happen. *Removed* is the one with nowhere
to be: the rows are gone, so the report is a line under the status bar naming them,
`refetch.removed`. That line is why `{{models}}` exists as a slot — a count alone
(「移除了 2 个型号」) reports a loss and withholds what was lost, on the page whose whole
job is which models this source has.

**`refetch.removed`'s zero case is a branch guarantee** `[derived]`. It renders only when
at least one id left the discovered slice; a fetch that removed nothing renders no line,
and a fetch that changed nothing at all renders `refetch.unchangedOnly` instead. So
`count = 0` is unreachable, which is the first of the two shapes §1.0's count rule asks
for.

**Its largest case is a fetch that comes back empty, and that is a caller rather than an
exception** `[derived]`. When a refresh answers with no models at all, every id the
source used to advertise leaves the discovered slice in one step — the maximum this line
will ever have to name, on the page whose whole job is which models this source has. So
an emptying refetch takes the same exit as any other readable answer: §0.8 sends it to
*Refetch result*, whatever the row count. It read otherwise until this round, jumping
straight to the empty state on the grounds that `sourceDetail.empty` 「already says the
stronger thing」 — which reversed the two. 这个来源没有返回型号 is a statement about
*this* fetch's result; `refetch.removed` is the statement about what the source had
before it, and nothing else on the page carries that. The two are one payload described
twice and the second does not cancel the first: the removal line sits under the status
bar where it always sits, the empty table renders its own sentence underneath, and the
next load keeps only the table's — which is the same one-fetch lifetime the diff has
everywhere else.

**`emptyNeverFetched` points at the status line rather than naming a cause**
`[derived]`. Its entry is one fact — `last_discovered_at` is null — and that fact is
produced by a revoked credential, a host that never resolved, a cooldown that has covered
every attempt so far, and a source added minutes ago whose first fetch is still the next
thing to happen. Naming the credential is right in one of those and wrong in the rest, and
wrong in the specific way that costs the user real work: it sends someone whose base URL
has a typo off to regenerate a key that was fine. The status line directly above is the
surface that *has* classified something — `status.needsAction.*` or `status.error` — so
this line defers to it and keeps for itself only what its own entry proves, which is that
no list has ever arrived and there are two ways forward.

**Two of these keys carry the null-case half the slot rule requires** `[derived]`
`[contract]`. §1.0 states the rule; both of its instances land on frame 06, because
this is the one surface that renders a source's own fields rather than a conclusion
drawn from them.

- **`sourceDetail.summary` opens with a host, and a source may not have one.** `base_url` is
  `api_key`-kind only, and null there means the vendor's official endpoint `[contract]`;
  a subscription never carries one at all. So `{{host}}` is populated for exactly one
  population — api_key sources pointed at a custom endpoint, which is what §3 calls a
  relay — and that is also the only population for whom it is load-bearing, since two
  relays are otherwise indistinguishable in this bar. Everywhere else the segment is
  dropped and the line renders `gateway.modelCount` instead, reused verbatim rather than
  restated under a new key; this bar already reuses `upstream.state.standby` the same
  way and for the same reason. Synthesizing the vendor's official hostname to fill the
  slot is specifically ruled out — it would need a separate vendor→API-host registry,
  and §1.4's billing/support destinations are no such input. It would also teach every
  subscription user an endpoint they cannot change and did not ask about, which is the
  mechanism D-8 exists to keep off the screen.
- **`sourceDetail.empty` claims a fetch happened.** 这个来源没有返回型号 is a statement about a
  completed discovery, and it is false for a source that has never had one — where the
  honest sentence is a different sentence with a different first action, since refetching
  a source whose credential was wrong from the start just fails again. Both are strings
  for an empty table, and `last_discovered_at` chooses between *them* rather than
  choosing the state: non-null takes `empty`, null takes `emptyNeverFetched`, and a table
  with rows in it takes neither. That variant is a flat sibling rather than `empty.neverFetched`
  because `empty` is itself a leaf, and a locale file cannot hold a string and an object
  at one key — the nested spelling reads better and does not exist.

**The status line reports the inventory's age, not a probe** `[frame]`. 使用中 ·
型号列表更新于 16:02 says when this table was last refreshed; it deliberately does not
carry latency or a last-checked timestamp, because nothing on this page performs a
probe. A freshness stamp next to a refetch button is a closed loop the user can
act on. A latency figure next to a refetch button would invite exactly the conflation
the previous rule forbids.

**The source identity label is unconditional and non-editable** `[frame]` `[contract]`.
The card and detail header show `provider or endpoint host · established protocol`, for
example `ai-relay.chainbot.io · Anthropic Messages`. It is not the retired
「接口由你指定」 badge: Source carries no manual/automatic provenance, so the label never
claims how the protocol was selected. `PATCH /api/models/sources/<source_id>` still has
no protocol field; changing it requires a new Source.
Consequently no `interfaceBadge.manual` or protocol-edit copy exists. The ordinary
identity pill is always present and reads only persisted endpoint/protocol facts.

**Extreme data** `[derived]`: the table does not collapse — the whole point of the
page is the full inventory, so it scrolls (the frame's 13 rows are an instance, not
a limit); long model ids truncate at 250 with the full value in `title`; a tier
list wider than 470 wraps to a second line and grows the row rather than
clipping — tiers are user-typed, so an arbitrary count is normal input, not an
edge case; tier strings are free text and are **not** case-normalized (D-5) — `high` and
`High` are two tiers, because `reasoning_efforts` is a list of strings the upstream
answers to and nothing here folds their case — but the two values the contract refuses are
refused **before** the request rather than after it: `reasoning_efforts` declares
`minLength: 1` and `uniqueItems` `[contract]`, so 回车 on an empty input and 回车 on a
value the row already carries both commit nothing and send nothing. The alternative is a
`PATCH` that can only ever be rejected, landing on 档位没保存上 with 重试 enabled over
input that will fail every time it is retried. Prevention is also why no copy exists for
that rejection: 约束四 asks for a state for every reachable error, and this one is not
reachable. A duplicate is visible as the chip already sitting in the row, and an empty
input is visible as an empty input, so neither no-op needs a sentence to explain
itself; the count `sourceDetail.summary` interpolates must be plural-safe
in English at 0 and 1, and it is `{{count}}` — the variable name is not a preference here
but the thing i18next selects the plural form from, so this rule reads it from §1.6's copy
table rather than naming its own.

---

### 1.7 Frame 08 `Doqav` — Live failure (gateway taken over)

**The question it answers:** *something ran out — did my work stop?* No: the same
overview, in the state where a head source died and the next one is serving. This
is not a separate screen. It is frame 01 telling the truth about a bad moment,
which is why every difference below is a state change and not a layout change.

**Deltas from 01** `[frame]`

| Element | 01 | 08 |
| --- | --- | --- |
| Header | run pill only (`A5e3S1` 网关运行中) | unchanged run pill **+** `q4k3s` pill, `#7C5BFF1A` fill, `apo8D` dot and `R3KU82` 「1 处接管中」 both `$--violet` 11/600; **auto-sized**, not a fixed box |
| ChatGPT source card | `CyTn7` 「正在供给 Codex」 `$--mint` | `D88ZO` 「暂不可用 · 15:40 后自动重试」 `$--gold` — the supply line is *replaced*, and what replaces it names when the source comes back |
| aihub source card | `iP3mu` 「正在供给 OpenCode」 | `rfkUM` 「正在供给 Codex、OpenCode」 |
| Codex group chip | — | `bbC4N` `#7C5BFF1A` with `g0smH` 「接管中」 `$--violet` 10/600 |
| Codex group subtitle | `X2B1F` 「网关 · 正常」 `$--mint` | `ZDTAZ` 「网关 · 降级」 `$--gold` (the `supply_status` mapping — §1.0 C-6, not a bespoke string) |
| Codex model rows | `huxZ3` / `wJHuc` / `Hq2Cb` 「当前 ChatGPT 订阅」 `#9BA3B8CC` | `hiauM` / `JB110` / `q5tdkr` 「当前 aihub(接管)」 `#7C5BFFCC` |
| Wire layer | 4 paths | **5**, and the change is two-part: `w_ChatGPT→Codex` (`jqjj0` → `gtjOy`) is **demoted** from `#5BFFA0` @1.75 to `#FFC857` @1, and `AEaxi` `w_aihub→Codex(接管)` is **added** at `#7C5BFF` @1.75 |
| Legend | 3 keys | **5** — plus `LmQFp` `#7C5BFF` 「接管中 · 临时改走」 and `oopTe` `#FFC857` 「暂不可用 · 供给已暂停」 |

**The wire delta is the whole design of this frame, and it is subtractive before it is
additive** `[frame]`. The naive rendering of a takeover deletes the dead relation and
draws the new one, which produces a picture indistinguishable from a normal day — the
user sees Codex being supplied and no reason to look further. 08 instead keeps the
original path drawn, thinned to 1 and turned gold, and lays the violet path over it.
Both facts are then on screen at once: *this is where your traffic used to come from,
and it is paused*, and *this is where it comes from right now, temporarily*. A build
that removes `gtjOy` has removed the only element that says a takeover is a
**deviation** rather than a configuration.

That is also the derivation of the two inks, and neither is invented here — §1.0 owns
the palette and this frame is where two of its rows are drawn. Violet marks the
takeover, gold marks the paused supply, at the pill, the chip, the subtitle, the row
suffix, the wires and the legend, without a single crossover. The earlier revision of
this table asserted gold for the takeover wire and 4 legend keys; both were stale, and
the round-3 circuit-breaker traced them to §1.0 rather than to this section. What
§1.7 owns is the *delta*; what it must never do is restate the palette, which is why
every ink above is given as the frame's measured value next to the node that carries
it.

The same fact is stated at three grains: a page-level pill (*is anything wrong*), a
group chip and subtitle (*which Agent*), and a per-row current-source suffix
(*which model*). That is deliberate — see D-14.

**States** — §0.8, rows marked §1.7.

`[derived]`: **Exhausted is not takeover and must not borrow its ink.** With no
candidate left the group shows 「没有可用来源」 and the wire layer draws **no violet
path** — violet means *rerouted*, and painting it where nothing was rerouted would
report a recovery that did not happen. The gold demotion still applies, because the
head source really is paused; what is absent is the thing that replaced it. The header
pill counts backends in takeover, so at zero it is absent, not `0 处接管中`.
`[contract]` AC-30 fixes the counting rule across grains, and an exhausted chain
contributes zero to it.

`[derived]`: **A head blocked by `needs_action` or `error` is not takeover either, and
the word on the pill is the reason.** §4.3 derives takeover from a head that is
unavailable *for a recoverable quota/cooldown or live connection-backoff reason*; violet says 临时改走, and
`model-hub.md` §4.5 is explicit that `error` carries no `retry_at` and that
`needs_action` waits on the user. Painting violet over either promises a return that
nothing in the system will deliver — the same defect as the Exhausted case, one condition
over, and both are why this frame's entry condition is a predicate rather than 「the head
is unavailable」. That state is §1.1's *Serving past a blocked head* — written there as
the negation of takeover, so it also holds the process and structural blockers this
paragraph does not name — and it renders **01**:
the group subtitle is 网关 · 降级 through C-6, the serving relation draws as an ordinary
supply path because that is what it now is — not a deviation from the configuration but
what the configuration resolves to until the user acts — the model row names its current
source without the 接管 suffix, and the pill excludes the backend entirely, contributing
zero to AC-30 exactly as an exhausted chain does. What it keeps from this frame is the
**gold demotion on the head's own relation**, because gold is 「供给已暂停」 — a statement
that *this path is not carrying traffic*, which is true of both blocker kinds.
The source's own ink is a different subject with a different owner: §1.6's status mapping
reads `#FF6B6B` for both, and a surface that derived one from the other would be right
only for as long as the two happened to agree.

`legend.unavailable` **lost a 暂 to make that true.** It read 「暂不可用 · 供给已暂停」,
and the first half is a claim about time that the legend is not entitled to make: it
labels an ink, the ink labels a relation, and the relation is paused for a `cooldown` that
clears itself and for a `needs_action` that waits on a person, with no way to tell them
apart from the wire. Painting the temporary word over the second is the same defect as
painting violet over it — one register down and easier to miss, because a legend reads as
vocabulary rather than as a promise. Temporariness is a real thing this document says
elsewhere and says with evidence: `upstream.state.unavailableRetry` says it because
`retry_at` is what selects that key. Nothing selects it here, so the legend states the
fact both cases share and leaves the cause to the surface that has one.

*Frame 08 still draws the longer label.* `oopTe` reads 「暂不可用 · 供给已暂停」 and the
two measurement records in this document quote it that way, because that is what the
canvas says. This is the copy register overriding a drawn string and not a
mis-transcription of one: §0.2 gives the frame authority over what is *drawn* — geometry,
ink, which elements exist — and the copy tables are where strings are specified, which is
why every state cites keys rather than the words beside them. The change is recorded here
rather than silently applied so the design file can follow it.

**Copy** — `models.hub.takeover.*`; every other string is shared (§1.0).

| Key | 中文 | English |
| --- | --- | --- |
| `pill_one` | {{count}} 处接管中 | {{count}} takeover active |
| `pill_other` | {{count}} 处接管中 | {{count}} takeovers active |
| `chip` | 接管中 | Taken over |

**Extreme data** `[derived]`: with N takeovers the pill says N and each affected
group carries its own chip — there is no "and others" summarization, because the
one question a takeover raises is *which one*. Violet paths are generated per
rerouted relation and gold demotions per paused one, so overlapping wires must remain
individually traceable (distinct routing, not stacked identical curves); a takeover
that reroutes two backends onto the same source draws two violet paths, not one
thicker one.

---

### 1.8 Frame 09 `UVR97` — Direct-only home (the page with nothing adopted)

**The question it answers:** *nothing here is on a gateway and there are no sources —
what is this page, and why would I want one?* It is the same page as 01, in the state
where nothing is adopted. Upgrading into it is the common way to arrive; it is not the
only one.

**One visible backend set owns filtering and page dispatch** `[derived]` `[contract]`:
`installedAgents = AgentSupply[]` filtered to `cli_present: true`. Source presence is
evaluated before that set: any retained Source selects 01 so its registered UI cannot be
hidden by an empty Agent set. Only when `sources == []` may an empty `installedAgents`
set select No backend found; only a nonempty set is quantified for all-direct / any-Hub
page selection, counted in the pill and rendered as rows. A hidden catalogue row's
persisted mode therefore cannot select a page that has no row for it, and the empty set
is never treated as vacuously “all direct.”

**Display condition** `[derived]`: `installedAgents` is nonempty, every member is in 直连
mode and no source exists. The empty `installedAgents` case is this section's No backend
found branch below. All terms are read from current state, and that is deliberate —
**this is a repeatable empty state, not a first-run screen.** The return path is
contracted end to end: AC-31's
round trip switches a backend Direct → Gateway → Direct and 「preserves saved Sources and
route configuration」, and §4.5's Source `DELETE` then removes the survivors — refusing
while a configured chain names one, and with `force=true` removing the Source and every
hop naming it in the same transaction. A user who walks that path lands back here, and
the page they get is this one. Nothing in the contract would let it behave otherwise:
`AgentSupply`'s thirteen properties are all current-state, no route or schema in
`docs/plans/model-hub-contracts/` records whether adoption ever happened, and a
condition keyed on that history would be keyed on an input the product does not have.
So the frame is specified as the empty state it is, rather than promising a
disappearance the inputs cannot deliver.

This is a *state of the Models surface*, not a separate onboarding route — the
moment one installed backend switches, the page becomes 01. Specifying it as a route would create
a second address that has to be kept in sync with the first and that users can reach
after it stops being true.

The rule an implementation has to encode is an ordered switch on **three** terms — *does
any source exist*, *is `installedAgents` empty*, and only for a nonempty set, *does any
member run through the gateway* — because
`undo.3` (§1.9) promises that switching the last backend back to 直连 keeps the sources
the user added. That promise makes 「all direct, sources non-empty」 a reachable state,
and retained storage makes 「zero installed, sources non-empty」 reachable too. Both must
keep 01's Source consumer visible.

| Any source exists | `installedAgents` | Any installed backend on the gateway | Page title / tabs | Body |
| --- | --- | --- | --- | --- |
| **Yes** | empty or nonempty | either | 01 title + tabs `[frame]` | 01; upstream Sources plus exactly zero or more installed Agent groups |
| **No** | empty | not quantified | `oPD53` 「模型」, tabs absent | No backend found; neither card renders |
| **No** | nonempty | **No** | `oPD53` 「模型」, tabs absent | this frame, and it occupies the whole page |
| **No** | nonempty | **Yes** | 01 title + tabs `[frame]` | 01 — this frame is gone as a page |

**The retained-Source row needs no new frame, and this section's own reasoning is what settles
it** `[derived]`. 09 is 01 「in the state where nothing is adopted」, and in that row
something is: the sources are still there. That alone would leave the choice open,
so the frame decides it — 09 draws no upstream column, so rendering it here would hide
sources the product just promised to keep, leaving no surface to inspect or delete them
on. So the
page is 01, and every element of it is already specified: the upstream module lists the
retained sources (§1.1), and each of zero or more `installedAgents` groups renders in its
current mode. An all-direct group carries 切换到网关 on
its header (`g3Wh0P`) exactly as a partially-adopted page renders its still-direct
groups; an empty set renders no group and is never summarized as all direct. The wire
layer draws only actual supply relations. The run
pill follows §1.0's runtime keying here, not 09's adoption keying — the engine's
liveness is a real fact again once the component has been installed, which is the split
the pill paragraph below already draws.

*Postmortem.* The display condition carried both terms and the branch table carried one,
so the two rules selected different pages for the same state — and the branch table, being
the one written 「as the rule an implementation has to encode」, is the one a build would
follow. The tell was in the prose all along: 「the moment one backend switches, the page
becomes 01」 describes a one-way trip, and this document specifies the return trip
elsewhere. A rule that reads correctly forward and silently drops a state on the way back
is not caught by reading it forward again.

**Two things about that switch are easy to get wrong; the owner ruling of 2026-08-23
settles the first, and the frames settle the second.** First, the tab strip is page
chrome across every overview landing: the usage ledger and switch log outlive the
Sources they name, so deleting the last Source cannot delete the only route to either
history. Frame 09 predates those sections and supplies the Sources & gateway body under
that strip. Second, this frame does not survive as a block inside 01 — but
its *function* does, relocated. The three backend rows here each carry 切换到网关; on 01
and 08 the same action rides on the still-direct backend's own group header (`g3Wh0P`
on 01, `lcPvy` on 08, next to the 「直连」 subtitle). So partial adoption keeps every
remaining backend one press from the gateway, without the page having to keep an
onboarding card around for the two backends that have not moved. The 你会多出三件事 card
is the part that does not relocate — it exists only on this frame, and it leaves as soon
as the page becomes 01. **What it is not is first-run-only**, and writing it that way was
the error: it argues from what the user has right now — no gateway, no sources, and three
things they would gain — not from whether they have ever adopted one. A user who adopted,
reverted and deleted their sources sees it again, and it is still true when they do. That
is the difference between an onboarding card and an empty state, and this is the second.

**What the Sources & gateway body drops, and why** `[frame]`. Frame 09 renders **no
three-column `cols` track, no dispatch rail, no wire layer and no legend.** The overview
shell still keeps the three-tab strip for the two independent history sections.
There is no gateway module to occupy the second column, no supply relations to draw, and
therefore no inks to explain. An empty gateway column with a placeholder would be worse
than its absence: it would assert that a thing exists here and is currently broken,
which is the opposite of the truth.

**The page and the module have different names, and neither is 「模型网关」** `[frame]`.
Measured across the original full-page set: the page title is 「模型」 (`oPD53` here, `YkN0P` on 01,
`VaXos` on 08, and so on), the first tab — the module this document specifies — is
「来源与网关」, the second is 「用量」, and the third is 「日志」. The string 「模型网关」 is **not rendered
anywhere in the product surface**; it appears only in the design file's own frame names
(「模型网关 09 — …」), which are canvas labels for the author, not copy. This is worth
recording because the plan documents use 「模型网关」 as the project's name for the whole
effort, and a lane that carries a project name onto a page produces two visible strings
with the same word at two different grains — a page called 模型网关 containing a tab
called 模型网关 — which tells a user the tab is the page. D-29 states the rule.

**Geometry** `[frame]`

| Element | Metric |
| --- | --- |
| `body` | `fill_container`, vertical |
| `topRow` | 1120×330, `gap 16` |
| `card_当前直连` | `fill_container` width, `$--surface`, radius 14 |
| `card_你会多出三件事` | **452 fixed**, `$--surface`, radius 14 |
| Card head | 56 tall, title 16 / 700 |
| Backend row | 64 tall, `$--background`, radius 10 |
| Backend tile | 34×34, radius 9, **`#FFFFFF0A` with a `$--muted` glyph** |
| Benefit item | `$--background`, radius 10; numeral tile 20×20 radius 999, `$--mint-soft` with a `$--mint` numeral |
| `note_逐个切换` | `fill_container`, `#FFFFFF06`, radius 14 |

**The backend tiles are neutral here, and that is a semantic statement** `[frame]`. On
01/03 each backend's identity tile carries its brand ink (cyan, mint, violet — §2 D-20).
On 09 all three are `#FFFFFF0A` with a muted glyph. **All three, together** — that is
what makes it a page decision and not a per-backend one, and D-20 depends on the
difference: a tile that muted for *this* backend and not that one would be reporting a
state, which is the one thing an identity tile may never do. 09 draws no supply
relations at all, and colouring its tiles would spend the page's only semantic inks on
decoration before the user has learned what they mean.

**Element inventory**

| Element | Displays | Interactive | On activate |
| --- | --- | --- | --- |
| Run pill | 「{{count}} 个后端都在直连」, muted dot on `#FFFFFF0A` | no | — |
| Backend row ×3 in the frame; one per `installedAgents` member at runtime | Name, 直连 pill, which login it uses | 切换到网关: yes | Open frame 10's confirm for **that backend** |
| 你会多出三件事 | The three things adoption buys | no | — |
| `note_逐个切换` | That adoption is per-backend and reversible | no | — |

**The run pill states a fact, not a fault** `[frame]`. It reads 「3 个后端都在直连」 with
a **muted** dot — not the mint 「网关运行中」, and not the error 「网关未运行」. The engine
genuinely is not running, and on 01 that would be a fault; here it is the expected
consequence of a choice the user has not yet made. Same underlying status, different
meaning, because the surrounding state differs — which is exactly why §1.0's state
machine keys the pill on runtime status *and* this page keys it on adoption. A build
that shows 「网关未运行」 in red on a first-run screen is reporting a problem the user
does not have.

**At zero installed backends the all-direct pill does not render, and Source presence
still owns the body** `[derived]` `[contract]`. Every AgentSupply row
carries server-authoritative `cli_present`; the page derives `installedAgents` once, then
uses that same set for rows, mode quantification and `{{count}}`. An empty set therefore
renders zero Agent groups without pretending that the fixed backend catalogue is itself
an installed set. With Sources it remains on 01; without Sources it reaches No backend found.
The branch occurs before plural selection, so the UI never renders 「0 个后端都在直连」 /
「All 0 backends are direct」.

So the page branches once more, and it branches **before** the pill:

| Sources | Backends installed | Header pill | Body |
| --- | --- | --- | --- |
| **≥1** | **0** | 01's runtime-owned pill, never `shell.allDirect` | 01: Sources module, zero Agent groups |
| **0** | **0** | absent `[derived]` `[contract]` (`cli_present` is false for every row) | Empty state: `empty.title` / `empty.body`, and neither card renders |
| **0** | **≥1**, all direct | `shell.allDirect`, count = `installedAgents.length` | This frame as drawn |

The empty state keeps the 你会多出三件事 card off the page too: three benefits that all
begin 「切换到网关」 argue for an action with no subject here, and the row list that would
carry that action is what is missing.

**The exit out of the empty state is outside the product, and the copy says so rather
than pretending otherwise** `[derived]`. There is no in-product action that can create a
backend: installing a CLI happens in a terminal. A state with no exit is a dead end, so
this one states the exit it actually has — install a backend, come back to this page —
and carries `empty.install` to the install documentation, which is not ours to inline.
That is the whole of it: one fact, one instruction, one link. What it must not do is
offer a 重试 or a 刷新 button, because nothing about this state is a failed read that a
second read could fix. §1.0's rule that a count-bearing key is
either guaranteed a non-zero count or given a zero-case key is satisfied here by the
first shape: this is the only surface that renders `shell.allDirect`, and above it the
pill is unreachable at zero.

**Copy** — namespace `models.hub.direct.*`

| Key | 中文 | English |
| --- | --- | --- |
| `card.current` | 当前:直连 | Currently: direct |
| `card.current.sub` | 每个 Agent 后端各自用自己的登录,直接连厂商。 | Each agent backend uses its own login and connects to the vendor directly. |
| `pill.direct` | 直连 | Direct |
| `backend.claude.detail` | 走它自己的 Claude 登录 | Uses its own Claude login |
| `backend.codex.detail` | 走它自己的 ChatGPT 登录 | Uses its own ChatGPT login |
| `backend.opencode.detail` | 用它自己的模型配置 | Uses its own model configuration |
| `action.switchToGateway` | 切换到网关 | Switch to gateway |
| `benefits.title` | 切换到网关,你会多出三件事 | Switch to the gateway and you gain three things |
| `benefits.1` | 额度用尽不断线 | Your session survives a quota running out |
| `benefits.1.detail` | Claude 订阅用满时,自动换到你添加的下一个来源,对话继续。 | When your Claude subscription is exhausted, it moves to the next source you added and the conversation continues. |
| `benefits.2` | 一个 Key 供给多个后端 | One key supplies several backends |
| `benefits.2.detail` | 同一个 API Key 可以同时给 Claude Code、Codex、OpenCode 用。 | The same API key can serve Claude Code, Codex and OpenCode at once. |
| `benefits.3` | 按后端、按型号自己定 | Decide per backend and per model |
| `benefits.3.detail` | 哪个后端的哪个型号走哪个来源,都可以逐条指定。 | You can specify which source each model on each backend uses. |
| `note.perBackend` | 逐个后端切换,互不影响 —— 其余后端保持直连。切换后随时可以切换回直连。 | Switch one backend at a time — the others stay direct. You can switch back at any time. |
| `empty.title` `[derived]` | 这台机器上没有找到 Agent 后端 | No agent backend was found on this machine |
| `empty.body` `[derived]` | 装好 Claude Code、Codex 或 OpenCode 之后,回到这一页,它们会出现在这里。 | Install Claude Code, Codex or OpenCode, come back to this page, and they will appear here. |
| `empty.install` `[derived]` | 怎么安装 | How to install |

**Copy states outcomes, not architecture** `[frame]`. Each of the three benefits names a
thing that happens to the user (the session survives; one key covers three backends; you
choose per model) rather than a mechanism that makes it happen (failover, a local proxy,
a route table). This frame is where that rule was hardest to hold, because
the honest description of the gateway *is* a mechanism, and the user has no reason to
care about it yet.

**Extreme data** `[derived]`

- **A backend the user does not have installed** is omitted from the list rather than
  shown disabled; the pill count follows the list. `{{count}}` is derived from the rows
  rendered, never hard-coded to 3. **At zero rows the pill is not rendered at all** — see
  the branch table above; this bullet is the rule that makes zero reachable, so it is the
  one that has to name the exit.
- **One installed backend already on the gateway** ends this frame's display condition; the page
  is 01.
- **Long account names** truncate in the detail line with the full value in `title`.

---

### 1.9 Frame 10 `g7MOA4` — Enable the gateway for one backend

**The question it answers:** *what exactly happens if I press 切换到网关, and can I take
it back?* A confirm over frame 09, stating consequences and the exit before the user
commits.

**It is a state of 09, not a second layout** `[frame]` — the same relationship 08 has to
01. Everything behind the scrim is byte-identical to 09; the delta is `scrim MHuuA`
(1440×1100, `#05050BE0`) plus `dialog_UkQqY`. The byte-identity is established the same
way 08's is.

**Geometry** `[frame]`

| Element | Metric |
| --- | --- |
| Dialog `UkQqY` | 560 wide, height hugs content, `$--surface`, radius 14 |
| `head` `ojIOL` | `padding [16,20]` `gap 4`; title 15 / 700; close 15px `#FFFFFF59`, named by `adopt.cancel` (§1.0) `[derived]` |
| Subtitle | 11.5 / normal, `#9BA3B8B3` |
| `dbody` `PtmwS` | `padding 20`, `gap 16` |
| Section label | 11.5 / 600, `#FFFFFF8C` |
| Bullet | 13px `$--mint` `check` icon + 12.5 / normal `$--foreground` |
| `foot` | `#FFFFFF05`, 1px top border; 取消 `#FFFFFF0A`, 切换到网关 `$--mint`, both radius 7, 12 / 600 |

**Element inventory**

| Element | Displays | Interactive | On activate |
| --- | --- | --- | --- |
| Title | 「把 {{backend}} 切换到网关」 | no | — |
| Subtitle | That the other backends are unaffected | no | — |
| 会发生什么 ×4 | The consequences of adopting; the first two are selected by backend | no | — |
| 可以撤回 ×3 | That it is reversible, and precisely where the exit is | no | — |
| 取消 | Leave unchanged | yes | Dismiss; nothing is written |
| 关闭 (head) | The same leaving, one press up; `adopt.cancel` names it (§1.0) `[derived]` | yes | Dismiss; nothing is written |
| 切换到网关 | Commit | yes | Switch **this backend only**; M5 refreshes Sources before the current result lands on 01 |
| Failure strip `[derived]` | `fail.title` over `fail.detail`, in the Failed state only | no | — |

**The dialog names the exit by location, not by promise** `[frame]`. The second
可以撤回 bullet reads 「回退入口:这一页的 Claude Code 卡片 → 切到直连」. "You can
change this later" is the standard phrasing and it is nearly useless: it is exactly what
a user hears before spending twenty minutes failing to find the control. Naming the
control and the surface it lives on costs one line and converts a reassurance into an
instruction. This is the same reasoning as D-14 — a way out that cannot be found is not
a way out.

**The subtitle states this backend's scope, never the other backends' mode** `[derived]`.
This dialog is not 09's alone: §1.1's register opens it from 01 as well, and on 01 some
backends are already on 网关. A subtitle reading 「其余后端保持直连」 would therefore be
false in exactly the situation a partially adopted user is in, and it would be false about
the one thing the dialog exists to reassure them of. 保持原样 says the same thing without
naming a mode it cannot know — which is what the element inventory above has always
claimed the line does. §1.8's `direct.note.perBackend` may still say 直连, and the
difference is not an inconsistency: that frame's entry condition is *every backend in
直连*, so there the mode is a fact the surface owns rather than a guess about its callers.

**Copy** — namespace `models.hub.adopt.*`

| Key | 中文 | English |
| --- | --- | --- |
| `title` | 把 {{backend}} 切换到网关 | Switch {{backend}} to the gateway |
| `subtitle` `[derived]` | 只影响 {{backend}},其余后端保持原样 | Affects {{backend}} only; the other backends are unchanged |
| `section.effects` | 会发生什么 | What will happen |
| `effects.1` `[contract]` | 若 Avibe 识别到当前 {{vendor}} 登录,且这个后端还没有原生来源,该登录会成为这个后端的原生来源。 | If Avibe recognizes the current {{vendor}} login and this backend has no native Source, that login becomes this backend's native Source. |
| `effects.2` `[contract]` | 否则这次只切换模式:不创建来源,也不调整已有来源的顺序。 | Otherwise this changes only the mode: it creates no source and does not reorder existing sources. |
| `effects.1.opencode` `[derived]` | OpenCode 自己的模型配置原样保留,这次切换不改动它 | OpenCode's own model configuration is kept as it is; this switch does not change it |
| `effects.2.opencode` `[derived]` | 它的型号从此由这一页上的来源供给;还没有来源时,先添加一个 | Its models are supplied from the sources on this page; if there are none yet, add one first |
| `effects.3` | 型号菜单不变 | The model menu does not change |
| `effects.4` | 正在进行的对话不受影响,下一次请求开始生效 | Conversations in progress are unaffected; the change applies from the next request |
| `section.undo` | 可以撤回 | You can undo this |
| `undo.1` | 随时可以切换回直连 | You can switch back to direct at any time |
| `undo.2` | 回退入口:这一页的 {{backend}} 卡片 → 切到直连 | Where to undo: the {{backend}} card on this page → Switch to direct |
| `undo.3` | 切回后,你添加的来源会留着,只是不再供给 {{backend}} | If you switch back, the sources you added stay; they just stop supplying {{backend}} |
| `effects.install` `[derived]` D-26 | 先安装网关组件({{component}},约 {{duration}}),再切换 | The gateway component ({{component}}, about {{duration}}) is installed first, then the switch happens |
| `cancel` | 取消 | Cancel |
| `confirm` | 切换到网关 | Switch to gateway |
| `confirm.install` `[derived]` D-26 | 安装并切换 | Install and switch |
| `fail.title` `[derived]` | 没能切换到网关 | The switch to the gateway did not go through |
| `fail.detail` `[derived]` | {{request}} · {{status}} · {{reason}} | {{request}} · {{status}} · {{reason}} |
| `fail.reason.transport` `[derived]` | 没能连上网关 | The gateway could not be reached |
| `fail.reason.refused` `[derived]` | 网关没有接受这次切换 | The gateway did not accept the switch |
| `fail.reason.notReady` `[derived]` | 网关起来了,但没有就绪 | The gateway started but did not become ready |
| `fail.reason.unknown` `[derived]` | 没有给出原因 | No reason was given |

**The fourth reason is the one that makes the other three safe to be specific**
`[derived]`. §0.9 has `{{reason}}` always present, so every failure this dialog can reach
must land on a word. The first three are the steps this dialog actually has: the mode
`PATCH` either gets no answer (`transport`) or gets one that is a refusal (`refused`), and
the D-26 path's middle step, `POST /api/models/runtime/start`, can answer while the
runtime still never reads healthy (`notReady`) — the only place that third reading is
reachable, and it is reachable there. But three readings chosen from the steps are a guess
about which failures exist, not a proof. Without a residue the
implementer's only reachable move is to interpolate whatever the server said, which is
the untranslated string this section spent a round removing everywhere else, or to pick
the nearest of the three and report a cause that was not the cause. 没有给出原因 is
weaker than the other three on purpose: it is the honest reading of a failure this
document did not anticipate, and it is the one line here that never misdiagnoses.

**The first two bullets are selected by backend, and only OpenCode differs** `[derived]`.
For Claude and Codex they are also the complete adoption precondition, not a promise that
`cli_present` can substantiate: executable presence proves neither a recognized login nor
the absence of an existing native Source `[contract]`. The pair therefore states both
transaction outcomes before the press. It never tells an unrecognized-login user that a
Source will appear, and it never claims an existing native Source will be moved to first.

Every backend row on 09 opens this same confirm, so its copy has to be true for all three.
`effects.1` / `effects.2` are total for Claude Code and Codex: a sanctioned recognized
login is adopted only when no native Source exists, and every other legal press changes
mode without creating or reordering a Source `[contract]`. OpenCode has no such login —
09's own row says 「用它自己的模型配置」, and
the eligibility matrix gives it no `native_cli` row at all, only API-key and hub-held
Sources. Rendering 「你现在的 {{vendor}} 登录成为第一个来源」 for it would either name a
vendor that does not exist as a single value or promise a source the switch does not
create. So an OpenCode confirm renders `effects.1.opencode` and `effects.2.opencode` in
those two slots; bullets 3 and 4, `section.undo` and all three `undo.*` are unchanged,
and the count stays four. The second variant also has to state the zero-source case out
loud, because for OpenCode it is reachable directly from 09 — where no source exists by
definition — and the page it lands on is §1.0's supply-absent treatment. A confirm headed
会发生什么 that omits *nothing will be supplied yet* is not disclosing the consequence, it
is deferring it to the moment the user's next request fails.

*Postmortem.* One copy table served three backends and was written from the two that
behave the same way. Nothing in this document was wrong about OpenCode — §1.8's row states
its config model plainly, one screen earlier — the confirm simply never asked whether its
own sentence survived that row. The general form: **a string with a `{{backend}}` slot is
a claim quantified over every value that slot can take**, and it must be checked against
the least convenient one, not the one it was drafted from. A rule that counts the rows
does not read what they say.

**Every bullet is a consequence the user can check afterwards** `[frame]`. 型号菜单不变
and 正在进行的对话不受影响 are there because they are the two things a cautious user
actually fears, and both are falsifiable — which is what makes stating them worth the
space. Nothing here explains what a gateway *is*; that argument belongs to 09's benefit
card, and repeating it in the confirm would turn a decision surface into a second pitch.

**States** — §0.8, rows marked §1.9.

**A committed mode switch does not yet own a complete landing projection** `[contract]`
`[derived]`. The success envelope carries the post-commit `AgentSupply`, so that exact
object is held as write evidence and is never reconstructed. A qualifying G-14
transaction may also have created a native Source, but neither the envelope nor any
AgentSupply inference carries that Source object. M5 therefore reads Sources before the
success landing. If that read fails, the mode change remains committed: 01 renders the
held AgentSupply with the last-good Source list marked stale, `upstream.unread` offers a
read-only retry, and ordinary navigation remains available. No path calls the mode
`PATCH` again or describes its result as uncertain.

**The Failed state has a rendering, and it is not a new one** `[derived]`. The failure
renders as one strip at the top of `dbody` `PtmwS`, above 会发生什么 — the same place the
consequences are read from, because a failure is the consequence that actually happened.
Its ink is §1.5's error treatment, cited rather than re-specified: fill `#FF6B6B14`,
stroke `#FF6B6B40`, a `circle-x` in `#FF8A8A`, title Inter 12 / 600 in `#FF8A8A`, and the
machine detail under it in JetBrains Mono 10.5 `#9BA3B8B3`. Nothing else in the dialog
changes — the bullets stay, 取消 stays, and 切换到网关 stays in its full mint treatment.
That last point is the one place this differs from §1.5's dimmed-重试 rule, and the
difference is real rather than an exception: there, an ambiguous Auto result cannot
progress until the user supplies a concrete interface; here the input was never the
thing that failed, so a second press is not a guaranteed repeat. Stating it this way
keeps one rule — *a control is dimmed when pressing it cannot change the outcome* —
rather than two rules that happen to disagree.

**The dependency case neither refuses nor installs behind the user's back** `[derived]`
D-26. This was G-6, and the ruling rejected both of the obvious answers. Refusing is
wrong because the user asked for an outcome — 「让这个后端走网关」 — and「你还没装
网关组件」is a report about the product's internals, not an answer; it leaves a person
holding an error they did not cause and cannot act on from this dialog. Installing
silently is wrong for the opposite reason: it spends the user's disk and a chunk of time
on something they never saw named, and the first they learn of it is a progress bar. So
the rule is: **name it, price it, and put a button on it.** The extra line goes in the
会发生什么 section, because that is exactly what it is — a consequence of pressing the
primary — and the primary's label changes so the button says what it will do. The
estimate is deliberately rough (「大约一分钟」-grade, not a byte count): its job is to
tell the user whether this is a "press it now" or a "come back later" decision.

This is also why frame 10's shell needs no new state to be *drawn*. The delta is one
bullet and one label, both inside structures the frame already has, which is the same
economy §1.6 used for the interface rule. `[frame]` for the shell, `[derived]` for the
two strings — see the copy table's `effects.install` and `confirm.install` rows.

**One primary, three contracted steps, one read-before-resume rule** `[derived]`
`[contract]`. 安装并切换 promises install → start → switch. The first step sends
`POST /api/models/runtime/install`; its durable `installing` state owns progress and its
closed `status.error_key` selects the verbatim
`settings.models.install.fail.detail` on failure. The later request
steps may use `fail.detail`. 重试 reads runtime status and AgentSupply before acting, then
resumes at the first unproved step; it never restarts an owned install or repeats a mode
switch already visible as `hub`. A proved mode commit is not a visible-exit shortcut:
the returned or reread AgentSupply enters M5, and only M5's Source read can make the
landing projection current.

*It says 「可能处于未完成状态」 and not 「什么都没装上」, and the difference is a claim this
UI cannot make.* An earlier wording read 没有任何东西被装上,可以重试。 — a promise about
the disk, asserted by a surface that never watched it. The managed-runtime install is not
atomic from the user's side: it stages bytes, removes any existing install directory, and
only then moves the staged copy into place, so a failure can land with partial bytes
written *or* with a previously working install already destroyed. A string that says
nothing was installed is false in both of those readings, and false in the direction that
matters — it tells a user not to look at a component that may now be broken. The rewritten
sentence is cause-neutral on purpose: the closed error key reports no filesystem detail,
and it states the
one thing that is true in every case — retrying reinstalls from scratch rather than
resuming. §1.0's *Install failed* row carries the same correction: the reading after a
dismissal is whatever `health` reports on the next read, not an assertion this surface
makes about the filesystem. Reusing it is the same discipline D-26 applies to the
component name, the duration and the consent rule: two entry points into one operation, one set of
strings, no second wording to keep in step. What differs between the surfaces is the
title, and it should — 没能切换到网关 is true here and false on 01, because only this
press promised a switch.

**Extreme data** `[derived]`: `{{backend}}` and `{{vendor}}` are interpolated in six
places, so the dialog must survive the longest backend name without reflowing its foot;
bullets wrap rather than truncate, because a consequence half-shown is worse than one
that costs a line. The dependency line adds a seventh interpolation (`{{component}}`)
and an eighth (`{{duration}}`), and both are rendered inside the bullet, never as a
separate banner.

---

### 1.10 Frame 11 `cyaYh` — Edit source / remove source

**The question it answers:** *where do I correct a source after creation, and where do I
remove the source itself rather than one model row?* The source overflow on 06 owns both
actions. Frame 11 is an exhibit sheet: its edit and remove dialogs are shown side by side
for registration and are never simultaneous runtime state `[frame]`.

Those two actions are an additive frame delta, not the overflow's complete menu (C1).
Healthy sources retain 重新授权 when their credential capability permits reauth and 更换 Key
when it permits static-key replacement; the same producers enter §1.11's shared repair
lifecycle. A `needs_action` card suppresses only a duplicate action already rendered inline.

**Element inventory**

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| Source overflow | 编辑来源; capability-gated 重新授权 or 更换 Key; 移除来源 | selected source + credential capability | yes | Edit / Remove open frame 11; either credential action enters the matching §1.11 producer with the exact source-detail origin held |
| Edit head | `sourceDetail.edit.title` plus kind, credential owner and established protocol | selected source | close | Dismiss unchanged |
| 显示名称 | current `display_name` | source | yes | Edit locally |
| Base URL | current `base_url` | API-key source | yes | Edit locally; clearing a custom value commits `null` and restores the official endpoint |
| Edit helper | Why protocol is fixed and when a Base-URL edit can affect supply | static | no | — |
| 取消 / 保存 | abandon / commit changed fields | local draft | yes | Dismiss / guarded `PATCH /api/models/sources/<id>` |
| Remove head | `guard.title.removeSource` plus kind, endpoint and model count | selected source | close | Return without forcing |
| Affected-hop list | `would_remove_hops`; each row names backend/menu model and, when contracted, its position | guarded refusal; absent before the first request | no | — |
| Remove helper | The transaction's destructive scope before the request; after refusal, the server-backed impact | static / guarded refusal | no | — |
| 取消 / 移除来源 | abandon / confirm the current stage | selected source / held refusal | yes | Dismiss / initial non-forced `DELETE`, then `?force=true` only after a guarded refusal |

**Edit geometry** `[frame]`

| Element | Metric |
| --- | --- |
| Scrim | 1440×1100, `#05050BE0` |
| Source overflow `b4_more` | 33 wide, `padding [8,9]`, `gap 6`, `#FFFFFF0A`, radius 7; 14px ellipsis |
| Dialog | left 180, top 360; 520 wide, height hugs content, `#0E0E18`, `#FFFFFF24` border, radius 14, shadow `0 16 40 #00000099` |
| Head | `padding [16,20]`, `gap 4`; title 15 / 700; data line JetBrains Mono 10.5 / 400 `#9BA3B8`; close 15px |
| Body | `padding 20`, `gap 14` |
| Field | `gap 6`; label 11.5 / 600 `#FFFFFF8C`; input 36 high, `padding [0,12]`, `#FFFFFF08`, `#FFFFFF14` border, radius 8 |
| Values | Name Inter 12.5 / 400; Base URL JetBrains Mono 11.5 / 400 |
| Helper | `gap 7`; info icon 13px; text 420 wide, Inter 11.5 / 400, line 17, `#FFFFFF73` |
| Foot | `padding [14,20]`, `gap 8`, `#FFFFFF05`; buttons `padding [8,14]`, radius 7, labels 12 / 600; 保存 uses mint |

**Remove geometry** `[frame]`

| Element | Metric |
| --- | --- |
| Dialog shell and head | left 740, top 360; the same 520-wide shell and head metrics as Edit |
| Body | `padding 20`, `gap 14` |
| List label | Inter 10.5 / 700 `#FFFFFF73`; count pill `padding [3,8]`, `#FFFFFF0A`, radius 999, 10 / 600 |
| Hop list | `padding 8`, `gap 6`, `#080812`, `#FFFFFF14` border, radius 10 |
| Hop row | 52 high, `padding [0,10]`, `gap 10`, `#FFFFFF03`, `#FFFFFF14` border, radius 8; main 12 / 600 `#F5F1E8B3`; meta JetBrains Mono 10.5 `#9BA3B8B3` |
| Helper | `gap 7`; info icon 13px; text 420 wide, Inter 11.5 / 400, line 17, `#FFFFFF73` |
| Destructive primary | `#FF6B6B1A` fill, `#FF6B6B59` border, `#FF6B6B` label; otherwise the shared foot metrics |

**The edit helper is governed by the contract, not by the exhibit's optimistic sentence**
`[contract]`. The drawn line says changing name or address does not rerun matching and
leaves models and routes unchanged. That is true for a display-name-only edit and false
for a Base-URL edit: the authoritative mutation matrix stages the new inventory, guards
the supply impact and may remove affected hops on force. `sourceDetail.edit.hint` is the
string of record, preserving the frame's fixed-protocol point while naming the guard
instead of promising no consequence. This is a copy correction, not a geometry change.

V1/V2 are the sole metadata-draft validator register `[derived]` `[contract]`. This
section does not keep a second field list: clearing a custom Base URL reaches the official
endpoint because V2 normalizes it to `null`, and every other Base URL or display-name
predicate comes from the same table. Save compares normalized values, while F1/F3 retain
them (C2/C7).

**Removal keeps confirmation before the first destructive request, then escalates in the
same shell** `[frame]` `[contract]`. Choosing the overflow action opens frame 11's 520-wide
remove dialog with the selected source identity and the transaction-scope helper, but no
affected-hop or supply-gap rows: the client has no server evidence for those yet and does
not scan chains to invent them. Its first 移除来源 press sends the non-forced delete.
A success removes the source; empty impact skips the report and reconciles through M2,
while non-empty impact is reported below before that same read. A guarded `409` fills the
same dialog with the server's staged consequence,
and only the next destructive press re-sends with
`?force=true`. The confirmation therefore protects even a source with no route impact,
while every consequence row remains evidentiary.

The forced response is newer evidence than its refusal preview `[contract]`. R2 therefore
holds and renders the returned `removed_hops` and `interrupted` before any surface reread; a
second client may have changed chains or protected Agent selections between the two
requests. The terminal report composes frame 11's existing hop block with §1.11's existing
`SupplyGap` block, omits whichever array is empty, and keeps both exact success arrays until
完成; M2 then reads Sources, Agents/source orders and Route chains together. With two empty
arrays the report is skipped, the exact Source is removed locally and the same M2 read runs.
R1 applies the same response-total rule to a Base-URL save: the returned `source` is the next
projection, non-empty impact evidence is shown first, and M1 reconciles the complete surface.
The same M rows also own D-36 commit inference. If a lost metadata response is reconciled by
the requested normalized fields appearing on the held Source, M1 runs before close; if a lost
delete is reconciled by that exact Source being absent, M2 transfers the evidence into M0.
Every other exact Source absence enters that same owner directly, so `source_not_found` cannot
jump from a modal to 01 with stale orders or chains. Those reads
cannot recover `removed_hops` or `interrupted`, so both members are marked unavailable rather
than empty and no impact report is invented. If any report-free read fails, C9 enters
Committed projection stale with exactly the returned or inferred write evidence and the last
good dependent projections marked stale. Its Retry repeats the same M1/M2 read and cannot
repeat the edit or deletion; DP-4's Done-equivalent exits preserve that evidence and stale mark.

The frame draws position pills directly from each refusal row's contracted one-based
`RouteHopRef.position`; it issues no per-chain lookup. It still inherits G-23 for a non-empty `would_interrupt`, because the registered
dialog has the hop block and summary sentence but no second body block that names protected
menu models and Agents. That remaining open gap is not filled from inference here.

**States** — §0.8, rows marked §1.10. The dialogs trap focus; Tab stays inside. DP-1
reversible states make Escape and the head close equivalent to 取消 and restore focus to
the source overflow trigger. DP-2 disables every dismissal path while Saving source or
Removing source owns a request. DP-4 committed reports instead make close, Escape and an
outside press equivalent to 完成; Committed projection stale adds the same exits beside its
read-only Retry and carries the stale-dependent-projection marker to the receiving surface.
Neither takes Cancel semantics, repeats the committed mutation or restores the held
pre-write origin (C3/C4/C6/C9). Enter submits Edit only when V1/V2 are valid and focus is in
a single-line field; both destructive stages require activating the focused button and
are not default Enter actions `[derived]`.

**Copy** is registered in the existing owners: source-menu and edit strings under
`models.hub.sourceDetail.*`, and refusal strings under `models.hub.guard.*`. Dynamic head
metadata is data and receives no copy key. The two response reports add only their
operation-specific shell strings and reuse the existing hop / `SupplyGap` row copy:

| Key under `models.hub.sourceDetail.*` | 中文 | English |
| --- | --- | --- |
| `edit.impact.title` `[derived]` `[contract]` | 来源已更新,部分供给受到影响 | The source was updated, and some supply was affected |
| `edit.impact.detail` `[derived]` `[contract]` | 以下路由或型号已被移除或中断。 | The following routes or models were removed or interrupted. |
| `edit.impact.refreshFail` `[derived]` | 来源已更新,但模型页面暂时无法刷新。 | The source was updated, but the model surface could not be refreshed. |
| `edit.impact.done` `[derived]` | 完成 | Done |
| `remove.impact.title` `[derived]` `[contract]` | 来源已移除,部分供给受到影响 | The source was removed, and some supply was affected |
| `remove.impact.detail` `[derived]` `[contract]` | 以下是这次移除实际影响的路由和型号。 | These are the routes and models actually affected by this removal. |
| `remove.impact.refreshFail` `[derived]` | 来源已移除,但模型页面暂时无法刷新。 | The source was removed, but the model surface could not be refreshed. |
| `remove.impact.done` `[derived]` | 完成 | Done |
| `impact.removedHops` `[derived]` `[contract]` | 已移除的跳 | Removed hops |
| `impact.interruptedModels` `[derived]` `[contract]` | 已中断的型号 | Interrupted models |

---

### 1.11 Frame 12 `qQvkP` — Needs-action / error source cards

**The question it answers:** *when a source has stopped supplying and its current state
already selects a recovery capability, what action is available where that state is visible?*
Frame 12 draws two `needs_action` card deltas over frame 01: an OAuth source with 重新授权
and an API-key source with 更换 Key `[frame]`. The same shell also renders contracted
`error` sources with their channel-selected existing action; it does not create a second
card component `[derived]` `[contract]`.

The card is not the only credential-repair producer (C1). The healthy-source overflow in
§1.10 keeps the same capability-gated 重新授权 / 更换 Key actions, and both entry points reuse
the lifecycle below. What differs is only the held origin and focus return target: the
blocked card when repair was inline, the vendor-observation action when it followed an
external destination, or the source overflow control when repair was elective.

**This is a component exhibit, not a coherent page snapshot** `[frame]`. The unchanged
gateway groups behind the two cards are authoring context and do not claim that broken
sources remain current or that downstream supply stays healthy. Implement only the two
drawn geometry variants from this frame, reuse that shell for `error`, and derive downstream
rollups from the normal §1.1 payloads.

**Element inventory**

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| OAuth needs-action card | Existing source identity and detail; rose dot; canonical `sourceDetail.status.needsAction.*` cause + `upstream.state.supplyStopped` | source | card + 重新授权 | Card → 06; 重新授权 → the shared acknowledgement, with channel-specific warning copy |
| API-key needs-action card | Existing source identity and detail; rose dot; canonical cause + `upstream.state.supplyStopped` | source | card + 更换 Key | Card → 06; 更换 Key → credential replacement entry |
| Error card projection `[derived]` `[contract]` | The same identity, rose state and supply-stopped line; canonical `sourceDetail.status.error` | source + supply channel | card + one existing channel action | Card → 06; Hub 重新拉取 → §1.6 Refetching; native 重新授权 → the shared acknowledgement |
| Repair action | One action selected by credential capability and cause; the same credential producers may also come from the healthy-source overflow | source + §1.4 static vendor register for the two vendor-directed causes | yes when a registered action exists | Starts the matching shared credential repair, or opens the registered external destination; it never also opens 06 |
| Self-managed fallback | `upstream.repair.contactProvider` after either vendor-directed cause on an `api_key` Source | source kind | no | — |

**Geometry** `[frame]`

| Element | Metric |
| --- | --- |
| Subscription card | Fill width, 108 high, `padding [0,12]`, `gap 10`, `#080812`, `#FFFFFF14` border, radius 10 |
| API-key card | Fill width, 106 high; otherwise the same shell |
| Identity tile | 34×34, radius 9; mint-soft fill |
| Name / detail | Inter 12.5 / 700; detail JetBrains Mono 10.5 / 400 `#9BA3B8CC` |
| Blocked state | 5px `#FF6B6B` dot + Inter 10.5 / 600 `#FF6B6B` |
| Repair button | `padding [5,12]`, `gap 6`, `#FF6B6B1A`, `#FF6B6B59` border, radius 7; label 11 / 600 `#FF6B6B` |

**The copy register wins over the exhibit's abbreviated causes** `[frame]` `[contract]`.
The PNG reads 授权已过期 and 凭据无效; §1.6 already registers the contract's four total
`detail_key` strings and §1.1 requires cards to reuse them. Frame 12 therefore contributes
the two drawn action keys and rose geometry, not a second cause vocabulary. The first example
renders 需要重新登录; the second renders 凭据被吊销. Both append the frame's
literal `upstream.state.supplyStopped`, so the full line still states the drawn supply
consequence without replacing the canonical cause `[frame]` `[contract]`. An `error`
projection uses the already-registered `sourceDetail.status.error` plus that same suffix;
it does not borrow a `needs_action` cause or add new cause copy. The derived vendor-observation
button key below is a channel-phase label, not another cause `[derived]` `[contract]`.

**Action selection is the contract's state × classification × channel-capability mapping**
`[contract]`: a classified OAuth expiry reauthorizes; a revoked static API key is replaced;
balance is topped up; a banned account goes to the vendor. For unclassified `error`, Hub
keeps the existing Refetch producer and `native_cli` keeps Reauthorize because its login is
the available observation primitive. Frame 12 draws the first two `needs_action` producers
and reuses their shell for `error`; it does not turn either drawn button into a universal remedy.

The other two branches consume §1.4's static subscription-vendor register rather than an
OAuth-flow field `[derived]`. On a subscription Source, `balance_exhausted` renders
`upstream.repair.topUp` and opens that vendor's top-up destination;
`account_banned` renders `upstream.repair.contactVendor` and opens its support/appeal
destination. Both reuse the drawn repair-button shell and open in a new browser context.
That gesture proves no recovery, so the mounted card enters Vendor recovery observation:
a Hub Source exposes its existing 重新拉取 producer, while `native_cli` exposes
`upstream.repair.reauthorizeToRefresh` — 「重新登录以刷新订阅状态」 — and then MUST pass
the existing acknowledgement before reauth. Returning from the vendor page is not evidence
that the vendor changed the account; only the channel observation or a later Source payload
can change what is rendered.

**Observation target ledger** `[derived]`: native currently targets acknowledged Reauthorize.
If the engine later contracts a non-destructive native observation capability, replace only
that target; the card shell, phase, state/copy mapping and evidence rule remain unchanged.

An `api_key` Source takes neither link branch, including one created from an official
compatibility preset. Its `vendor` says which protocol family was configured, not who
operates the account. For `balance_exhausted` or `account_banned` it therefore renders the
non-interactive `upstream.repair.contactProvider` fallback and keeps the card target to 06;
it never sends that user to Anthropic or OpenAI on an identity the payload did not prove.

Both Hub and `native_cli` reauth actions, including the native observation label above,
first open the same registered acknowledgement phase
`[contract]`, and only 继续登录 sends the literal `{acknowledge_irreversible: true}`. The
body is not shared: Hub selects `upstream.repair.reauthConfirm.detail.onFailure`, whose
complete conditional sentence says that failure is the cost and cancellation does not
replace the current login; `native_cli` selects
`upstream.repair.reauthConfirm.detail.immediate`, whose complete sentence says that starting
one CLI login immediately makes every Source sharing it unavailable and success restores
only the selected Source. Neither body is assembled from a sentence that is false for the
other channel (C8).

**Reauth imports §1.4's one OAuth machine; these are its only deltas** `[contract]`:

- acquisition and every fresh retry preserve `intent: reauth`: they repeat
  `POST /api/models/sources/<id>/reauth` with the held channel body, never the create-only
  `POST /api/models/oauth/start`. RR-1/RR-2 classify the returned flow before any form: a
  terminal acquisition is status-read immediately and is never presented;
- Forms A, B and C are selected from a **non-terminal** returned `presentation`, never a
  vendor table. PD-1–PD-3 preallocate and navigate the gesture-owned browser context, then
  keep the non-null `auth_url` as a visible same-flow fallback. PD-5 reads a held paste flow
  every 2 seconds while that required URL is null, and its first non-null value navigates the
  original PD-1 context without starting another flow or context. `expects` selects the branch,
  and PD-4 renders a resolved `instructions_key` or the expects-specific fallback when it
  is null or unresolved; G-33 remains only the missing Form B `device_code` output. Submit,
  the evidence-class matrix, 2s polling, timeout, cancel and the F1–F5 treatments are
  otherwise §1.4 verbatim. In particular, E2 transport/`engine_down` evidence keeps polling,
  E3a/E3b distinguish action-required from accepted paste input, and an acquired reauth flow's
  E8 `flow_not_found`, E9 `flow_expired` / terminal failure or E6 materialization failure runs
  RR-5's complete M3 read for Hub or native before handoff. A pre-flow reauth acquisition
  failure instead reads only the selected Source for either channel; the evidence milestone,
  never the channel name, selects the scope. Only E6's `discovery_failed` /
  `migration_item_conflict` stops immediately; R5 renders a conditionally present,
  exact nonempty `interrupted_pairs` report and never invents one when the member is absent;
- a successful terminal carries `source`, `recovered` and `interrupted_pairs`; it never
  consumes create-only `added_to` or `adopted_by`;
- RR-10 owns every dismissed flow: the dialog leaves immediately, then background cleanup
  settles its authorized cancel attempt before it re-reads the complete model surface. A
  cancel failure or ownership handoff does not suppress the read.

The 更换 Key card action is the producer, not the secret field. It opens the key-entry
step owned by credential repair; plaintext remains local until submit, and only submit
sends the guarded replacement route. Its guarded `409` imports §1.6 `Qp6FI`; the only
delta is the operation: hold the typed key, render the `guard.*.replaceKey` strings, and
confirm by re-sending `{key, force: true}`. Cancel returns to the held key-entry state with
the key intact. Frame 12 fixes the missing card affordance without claiming unexported
modal geometry.

R3 and R4 share the report shell, not a wire tail `[contract]`. R3 holds OAuth reauth's
`source`, `recovered` and `interrupted_pairs`; it classifies `source.state` before array
cardinality. A blocked returned Source enters Repair unresolved and stays visible whether
the array is empty or not. Each non-empty pair is still a `SupplyGap`: backend and protected
menu model, followed by the named-Agent line only when `agents` is non-empty, and renders as
evidence under either Repair unresolved or a non-blocked Repair impact result.
R4 instead holds the standard guarded-mutation `source`, `removed_hops` and `interrupted`,
and renders the existing hop and SupplyGap blocks independently. `recovered` is explicitly
not a second R3 UI branch: it reports whether the prior state was blocked, while
`source.state` is the complete current projection. No response array can be rebuilt by a
later read, so DP-4 keeps the exact R3/R4 envelope rendered while M3/M4 refresh Sources,
Agents/source orders and Route chains together. A failed read keeps the report and exact
envelope mounted, adds read-only Retry, and leaves Done, close, Escape and outside press as
legal committed exits; those exits carry the evidence and stale-projection marker and never
question the credential write. Only a non-blocked empty-impact success may take the
report-free M3 handoff; it holds the exact empty envelope and returned Source while the
complete read settles, and failure enters Committed projection stale. A blocked empty-impact
success remains Repair unresolved instead (C9/C10).

RR-7 is the response-lost counterpart of that rule `[derived]`: a held blocked origin that
the authoritative reread now shows clear is mutation-specific commit evidence, but it is not
a response envelope. It therefore enters M3/M4 before a visible repaired handoff and marks
every response-only tail member unavailable. A failed complete read enters Committed
projection stale with that Source evidence; a later projection read may complete the handoff
but can never reconstruct or render an impact row the lost response alone could have carried.

The shared acknowledgement, key-replacement refusal and frame 11 removal all cite §0.8's
C2/C3/C5 checks: a no-op exit restores the exact state and valid draft under the modal,
returns focus to its invoking control and never promotes the source to `Ready` `[derived]`.
Committed repair reports/results instead cite C6, and the replacement key / paste-back
fields cite C7. The acknowledgement also cites C8 for channel consequence coverage, and empty-impact
or unavailable-impact repair cites C9 for its committed read-failure state. C10 owns attempt
scope and received/inferred commit evidence before either report or handoff; C11 owns the
workflow milestone each non-terminal flow state can prove. C12 owns delayed presentation
delivery, C13 makes attempt-scope reconciliation mandatory for every terminal/absence, and
C14 gives each modal failure a non-mutating exit. C15 requires authoritative landing
evidence whenever one of those branches changes the page form: a successful AgentSupply is
dispatched and installed immediately, sibling failure cannot erase it, and a classifier
alone may retire an illegal surface but cannot project its destination. FF-1 preserves an
active target only while it is mounted, focusable and admitted; PF-1 chooses a rendered
fallback only after that test fails. C16 gives each gesture or single producer edge to ET,
composite settlement to AR, pure page installation to AS, focus validity/fallback to
FF-1/PF-1, read closure to RL, wire provenance to AP and asynchronous control admission to
CA. Each atomic owner accounts for cleanup, visible feedback, destination focus and next
owner; different outcomes cannot share a row, and visible feedback without a registered key
or named copy block is incomplete. None can be inferred from the reversible no-op rule.

**States** — §0.8, rows marked §1.11. Within a card, Tab reaches the card target and then
the repair button; Enter or Space activates the focused target. Activating the nested
repair button does not bubble into the card's open-detail action. Idle confirmations trap
focus; Escape is cancel; Enter activates only the focused control. Pre-flow Reauthorizing
and Replacing key disable dismissal while their mutation owns the response; once a flow is
held, §1.4's explicit cancel path owns its late answer and RR-10 reread (C4). Every impact
report and unresolved result keeps focus inside; close, Escape and outside press invoke the
same committed exit as 完成 and never restore the invoking origin. Committed projection stale
focuses read-only Retry first, but Done, close, Escape and outside press remain legal committed
exits that carry held evidence and explicitly stale dependent projections (C6/C9/C14/DP-4) `[derived]`.

**Copy** — the causes reuse `models.hub.sourceDetail.status.needsAction.*`; the drawn supply
suffix is `models.hub.upstream.state.supplyStopped`; credential actions, vendor exits,
self-managed fallback, acknowledgement, unresolved outcome and terminal impact strings are
registered under `models.hub.upstream.repair.*` in §1.0; the credential refusal's operation strings are
`models.hub.guard.*.replaceKey` in §1.6. No i18n file is changed by this registration.

---

### 1.12 Frame 13 `Q9q5lF` — Subscription vendor menu

**The question it answers:** *which subscription am I adding before the vendor-specific
dialog asks how it should be used?* It is the producer for §1.4's vendor parameter and
the only effect of 01's 添加订阅 press `[frame]` `[contract]`.

**Element inventory**

| Element | Displays | Interactive | On activate |
| --- | --- | --- | --- |
| 添加订阅 trigger | Shared `upstream.addSubscription` label and plus icon | yes | Toggle the menu |
| Claude 订阅 row | Vendor subscription name + 原生推荐 | yes | Close; open 04 with `vendor: anthropic` |
| ChatGPT 订阅 row | Vendor subscription name + 网关推荐 | yes | Close; open 04 with `vendor: openai` |

**Geometry** `[frame]`

| Element | Metric |
| --- | --- |
| Component exhibit | 720×420, `padding [56,60]`, `gap 14`, `#080812` |
| Trigger | `padding [7,12]`, `gap 6`, mint fill and border, radius 7; icon 12px; label 12 / 600 `#080812` |
| Menu | 300 wide, height hugs content, `padding 6`, `gap 2`, `#0E0E18`, `#FFFFFF24` border, radius 10, shadow `0 10 28 #00000080` |
| Row | fill width, `padding [9,10]`, `gap 8`, radius 7; active/focus fill `#FFFFFF08`; label 12.5 / 600 |
| Native badge | `padding [2,7]`, `#5BFFA01A`, radius 999; label 10 / 600 mint |
| Gateway badge | `padding [2,7]`, `#FFFFFF0A`, `#FFFFFF14` border, radius 999; label 10 / 600 muted |

The authoring caption above the exhibit is not product text. On open, focus moves to
Claude 订阅, the first row. Arrow Up/Down moves between rows, Home/End moves to the first
or last row, Enter/Space selects, Escape returns focus to 添加订阅, and an outside press
dismisses without choosing `[derived]`. A selection closes the menu before 04 opens, so
returning from 04 never leaves a stale menu under the dialog. The menu row that held focus
has unmounted by then, so a no-op 04 dismissal back to 01 explicitly restores focus to the
still-mounted 添加订阅 trigger rather than falling back to the document body. A successful
terminal enters 06 instead, and that receiving surface owns focus (C3).

**States** — §0.8, rows marked §1.12.

**Copy** — namespace `models.hub.addSubMenu.*`

| Key | 中文 | English |
| --- | --- | --- |
| `vendor.claude` `[frame]` | Claude 订阅 | Claude subscription |
| `vendor.chatgpt` `[frame]` | ChatGPT 订阅 | ChatGPT subscription |
| `recommendation.native` `[frame]` | 原生推荐 | Native recommended |
| `recommendation.gateway` `[frame]` | 网关推荐 | Gateway recommended |

Long localized recommendation badges may not widen the 300px menu. The badge wraps only
as a last resort; the vendor label keeps the flexible track and truncates with its full
value in `title` `[derived]`.

---

### 1.13 Frame 14 `IM4c2` — Runtime off

**The question it answers:** *what remains when the managed gateway is not running?*
The page shell, runtime state pill and unchecked switch remain. The tab strip, Sources,
Agent gateway models, route controls, supply graph and every internal configuration
dialog are absent. Configuration is preserved; it is hidden rather than deleted.

**Element inventory** `[frame]`

| Element | Displays | Interactive | On activate |
| --- | --- | --- | --- |
| Runtime pill | `shell.notStarted` or `shell.stopped` | no | none |
| Runtime switch | Unchecked `Switch/Unchecked` | yes when start/install is available | Start the runtime, or open the managed-install confirmation |
| Closed-state icon | Neutral power glyph in a 44×44 bordered tile | no | none |
| Closed-state title | `shell.closed.off.title` | no | none |
| Closed-state body | `shell.closed.off.body` | no | none |

**Geometry** `[frame]` — the frame is 1440×1100 Dark and reuses Frame 01's shell.
The internal area is a fill-width 854px vertical band with only top/bottom borders;
content is centered, with a 44×44 radius-8 icon tile, 17px title and 12.5px body.
The runtime switch sits beside the state pill in the page header. No hidden internal
surface consumes layout space.

`down` uses the same positive inventory with failed-start body copy; `not_installed`,
`installing`, `starting`, `stopping`, unsupported and unread states replace only the
center title/body and busy glyph. A retained live F2 snapshot may keep the prior internal
surface visible, but its switch is disabled until an authoritative status read lands.

---

## 2. Interaction decisions, and why

Each rule is one line, with one line of why. The why is not commentary: when two
rules collide in code, the reason is the only thing that tells a lane which one to
keep. Without it, the rule that is easier to write wins.

**D-1 — "Relay station" is not a category.** A relay, an aggregator and a
self-hosted endpoint are all *an API key with a custom base URL*.
*Why:* the official/unofficial split is unanswerable for compatible endpoints, and
a category the product cannot adjudicate becomes a label that lies. `[spec §3]`

**D-2 — Protocol selection is explicit but never self-proving.** The add form offers
Auto detect plus each supported interface before the first request. Auto is the default;
a concrete choice restricts observation to that one protocol.
*Why:* compatible relays often expose multiple or misleading routes. The operator may
know the contract while the product still owns the establishment check required to save
it.

**D-3 — Manual selection narrows verification; it never bypasses it.** A concrete
selection produces exactly one candidate probe. State ④ keeps the form and selector
visible, and Retry stays dimmed while Auto remains selected after auto-detection was
ambiguous.
*Why:* one visible owner for the candidate removes probe-order uncertainty without
turning a user claim into a stored fact. A matching upstream response remains the gate.

**D-4 — Identification is a gate; the model pull is not.** Nothing persists until an
upstream response identifies the interface — state ③ offers only 取消 / 重试 — while
拉取型号 is optional, because 添加 performs it anyway.
*Why:* the two unknowns are not the same kind. A protocol the product cannot confirm is
a value that would have to be invented, and an invented protocol fails later, at request
time, far from the fix (D-3). A model list that is merely stale is a fact with a
timestamp, and 06 already shows its age. So the check that would have to guess blocks,
and the one that can be repeated later does not. An earlier revision of this file
inverted that — it kept a 仍要添加 escape in ③ on the grounds that a probe cannot
distinguish "your key is wrong" from "the vendor is down this minute" — and the owner
ruled for the contract (§0.6, E-3). The distinction survives in where it applies:
the user is never blocked on the vendor's *health*, only on the product's ability to
tell what it is talking to. State ⑤ is that sentence read forwards rather than
backwards — protocol known, inventory unknown, so the add proceeds — and D-27 states
the invariant both halves obey.

**D-5 — Reasoning tiers are a user-typed list with no default and no prefill.**
Empty renders as 未设置档位.
*Why:* discovery returns model ids only. A prefilled tier would be a value the
product invented, and the user would read it as a fact about the model.

**D-6 — `$--cyan` means exactly one thing: 原生 — this supply comes from a `native_cli`
hop.** It is a property of the *hop*, never of the backend, and it never inks a control.
*Why:* a colour is readable at a glance only while it has one referent. A second
meaning does not add information, it halves it. The referent has to be the hop rather
than the backend because a gateway-mode backend can perfectly well be supplied by a
native source — cyan into a 网关 group is a normal picture, and a build that reads cyan
as "this backend is 直连" will draw the wrong thing on that row (E-4, D-24).

**D-7 — A collapsed backend group shows at most the first six models.** The prefix is
selected only by backend menu order; row state never expands it implicitly. Every
remaining model stays reachable through the counted disclosure row, and expanding keeps
the same total order. `model_supply.has_runnable_hop` still selects the state indication:
a structurally empty Route renders `models.launch.route_unconfigured`, while a nonempty
false row renders `legend.unavailable` when that row is visible.
*Why:* the overview is a scanning surface, and an unbounded exception rule lets a backend
with many unavailable models consume the entire graph track and push later sections out
of view. Six rows keep the group bounded without deleting or reordering any model; the
explicit disclosure is the route to every state beyond the prefix.

**D-8 — The user does not perceive the supply mechanism.** Protocol, channel and
injection are never *displayed*: no surface reports which one is in use, and none offers
a choice about it. The add flow is the exception the rule is stated against — there the
product cannot proceed without an answer it is unable to observe, so it asks. §1.5 owns
what that costs in rendered words, and the wording is bounded by D-27's property rather
than by a count of places.
*Why:* the mechanism is the product's job, and surfacing it invites decisions the
user has no basis to make. Earlier revisions carved out a second exception — a quiet
badge on frame 06 for sources whose interface the user had hinted — on the reasoning
that hiding somebody's own decision makes it unfindable. The ruling deleted the badge
instead (§1.6, E-2), and the reasoning it replaced that with is better: the hint was
never a decision the user can revisit, so making it findable buys nothing and teaches
everyone else that protocols are their problem.

**D-9 — Default routing belongs to one backend.** Its ordered subset supplies the
effective planner for inherited models. Sources PUT changes this subset under exact-plan
guards and leaves manual arrays unchanged. Inventory chooses the matching tier before
passthrough; health determines which planned hop is runnable.

**D-9a — A backend in 直连 mode exposes no order surface at all.** No 默认路由 button
on its group head, and the drawer is unreachable for it.
*Why:* a direct backend consults no source order, so the editor would edit a list
nothing reads — the most expensive kind of dead control, because it looks like it
worked. This is the same rule as D-16 applied to configuration rather than to display:
do not render a value the system does not hold.

**D-10 — A source outside a backend's order is held out of *that order*, not excluded
from the product.** Frame 03's held-out section says so in its label and its rule; §1.3
owns both.
*Why:* the earlier 不参与排序 phrasing asserted something much stronger and false — the
same source may lead another backend's order, and a per-model custom chain can name it
directly. A label that overstates a scope teaches a wrong model of the system, and this
one taught the exact model E-1 was resolved against. The section still exists to answer
"why isn't this source in the list" *before* it is asked. `[spec §4.1]`

**D-10a — An ownership transfer shows the way back on the surface that takes it.**
*Why:* the cost of taking ownership is invisible and deferred — what you took over stops
tracking what changes elsewhere — so it has to be stated where it is incurred, not
discovered a month later. And an ownership transfer with no return path is a one-way door
built by accident.
**S-1 left this decision with no instance in the product.** Its one instance was frame
02's per-model chain, where choosing 自定义 detached a model from the backend's source
order and 恢复跟随来源顺序 was the way back. S-1 deletes the derivation, so there is no
longer a transfer to show a way back from: a configured chain is configuration from the
moment it exists. The decision is kept rather than deleted because the next surface that
derives something on the user's behalf will need it, and because the reason a rule was
introduced is the part that does not go stale. §1.3 records what its own drawer lost to
the same deletion, as a real loss, not answered here.

**D-11 — A successful add has no success screen; it lands on 06.**
*Why:* the question after adding is always "what did I just get". A confirmation
screen answers a question nobody asked and adds a click before the answer.

**D-12 — Adding a subscription is a choice between two named consequences, not a
toggle, and the recommendation is per vendor.**
*Why:* the recommendation differs by vendor for a legal reason, not a technical
one, so it cannot be a single global default — it has to be stated per vendor,
where the reason applies.

**D-13 — A terms-of-service warning sits inside the option that triggers it.**
*Why:* a page-level warning reads as boilerplate and gets skipped; a warning
inside the option reads as a consequence of the thing you are about to click.

**D-14 — A takeover is announced at all three grains at once** (page pill, group
chip, row suffix).
*Why:* the same fact answers three different questions — is anything wrong, which
Agent, which model — and a user arriving at any one grain must not have to hunt for
the other two.

**D-15 — A failure never makes a mutation the only way out.** Where the failure is
modal, the exit is that modal's own 取消, and §1.4 / §1.5 say which states carry one.
Where the failure is a page or a bar, the surface it failed on is already the way out
and no control is added for it.
*Why:* an error whose only affordance is a mutation forces a decision the user may
not be equipped to make yet. Leaving is always a legitimate answer. Stated as a
constraint rather than as a roster, it also binds the next failure state somebody draws.

**D-16 — A surface that cannot prove a fact renders indeterminate, not a last-known
or defaulted value.** Engine down ⇒ derived columns show `—`; refetch failed ⇒ the
previous list is kept and the *bar* carries the failure.
*Why:* a stale value is indistinguishable from a fresh one, so the cheap nicety
becomes a lie precisely when the user is trying to diagnose something.

**D-17 — These frames specify surfaces, not addresses.** No navigation path, route,
breadcrumb or sidebar position may be read off them; where this file states a location
it carries a `[spec]` marker and the authority is `model-hub.md`, not the drawing.
*Why:* the shell in these frames exists to make the composition readable, and its
navigation structure is **not** the shipped one. A frame is a picture of a surface; a
reader who infers routing from it is inferring from a decision that was never made. The
cost is asymmetric — a wrong metric is caught by the next fidelity check, a wrong route
is caught by users.

**D-18 — Placement is the mode surface; there is no mode switch widget.** A backend's
mode is expressed by *where its configuration lives* — a 直连 backend has a
切换到网关 action and nothing else, a 网关 backend has 默认路由, model rows and
切到直连.
*Why:* a mode toggle would make the two modes look like two settings of one thing,
inviting the user to flip it and see. They are two different configuration models with
different surfaces; making the surfaces differ is what teaches that, and it costs no
extra pixels.

**D-19 — Provenance pills are a closed enum with a fixed palette, and a user-set value
of an enum is inked differently from a system-derived one.** 原生 / 本机凭据 = cyan
(`#3FE0E51A` / `$--cyan`); 订阅 = mint (`#5BFFA01A` / `$--mint`); API Key = muted
neutral (`#FFFFFF0A` / `$--border` / `$--muted`). A value the system derived uses the
muted neutral; a value the user set uses the strong neutral (`#FFFFFF14` / `$--border`
/ `$--foreground`) — 06's 自动拉取 versus 手动添加 is the precedent and, since S-1, the
only instance this file specifies.
*Why:* found the hard way. 03's API Key pill and 02's chain pill had both drifted to
violet, which put four unrelated meanings on one ink and made the takeover colour
unreadable. The rule was derived from the inventory rather than from the names: the API
Key pill is muted in 8 of its 10 instances, so the outlier was provably the defect, and
06 already distinguished derived-from-system from set-by-user with exactly this pair of
neutrals. Naming a colour after a concept is how the collision happened; deriving it
from what the other instances actually do is how it got fixed.

**D-20 — Backend identity tiles are brand ink, not semantic ink, and form factor is what
says so.** The 34×34 / 30×30 rounded chips carry each backend's own colour (Claude Code
cyan, Codex mint, OpenCode violet) regardless of that backend's mode; semantic ink lives
only in pills, wires, dots and state text.
*Why:* OpenCode's tile is violet while its state is 网关 · 正常, so reading tiles as
semantic would report a takeover that is not happening. Two ink systems can share a
palette as long as one glance separates them — here, a filled rounded square containing
a glyph is never a status. Frame 09 mutes all three tiles, and *all three* is what keeps
the rule intact rather than breaking it: a page may decide it draws no brand ink, and
09 does, but muting one backend's tile and not another's would make the tile a status
after all. Per-backend, mode changes nothing — Claude Code is direct on 01 and its tile
is cyan there.

**D-21 — The state-text layer and the wire layer are separate vocabularies, and a colour
means different things in each.** Wires: cyan = 原生, mint = 网关供给, violet =
接管, `#FFFFFF26` = 已启用 · 当前未被使用, gold = 供给已暂停. State text:
mint = 使用中 / 正常, gold = 降级 / 暂不可用 / 冷却 / 供给暂不可用, rose = 需处理 /
异常 / 无可用来源, muted = 可用 · 当前未供给 / 未配置型号路由 / 暂无 Agent 使用,
cyan = 原生 provenance only, violet-tint
`#7C5BFFCC` = a takeover hop label. The group-status vocabulary (§1.1) is assigned here
in full — 正常 mint, 降级 gold, 供给暂不可用 gold, 无可用来源 rose,
未配置型号路由 muted, 暂无 Agent 使用 muted — and
the split worth stating is §4.5's: a wait that heals itself takes the same gold as every
other wait, one that does not takes rose, and the last two are not Source faults at all:
the fifth means enabled Agents lack a selected model or configured Route, while the sixth
means no enabled Agent uses this backend.
*Why:* a wire describes a *relation between two things*; state text describes *one
thing's condition*. Collapsing them into one legend forces both to be wrong somewhere —
gold as a relation means supply stopped, gold as a condition means degraded, and those
are not the same claim. §1.0's ink table is the single place both are written down.

**D-22 — A group head's status line is `<mode> · <status>` on the gateway and the mode
word alone in Direct; mode comes from `mode`, while status comes from the closed
`named_agents` aggregate.** 网关 · 正常, 网关 · 降级, 网关 · 供给暂不可用,
网关 · 无可用来源, 网关 · 未配置型号路由, 网关 · 暂无 Agent 使用 — and bare 直连, because a
direct backend arbitrates nothing and so has no supply whose health could be reported.
§1.0's C-6 is the total mapping, and no other surface derives a status line of its own.
*Why:* mode and health are independently variable and users confuse them constantly —
"is it on the gateway" and "is it working" are different questions, and a single word
answers whichever one the reader happened to be asking. The old header read the
top-level `supply_status`; when the global default Agent belonged to Codex, Claude's group
therefore rendered 未选型号 even while an enabled Claude Agent had a healthy explicit
model and Route. Aggregating the already-authoritative `named_agents` projection keeps
the header at backend grain without inventing a backend-wide selected model.

**D-23 — The legend swatch may deviate from the ink it stands for, only downward in
alpha, and only where the wire's own alpha is below the legibility floor.** The
已启用 · 当前未被使用 wire is `#FFFFFF26` at 1.75px; its 20×1 legend swatch renders
`#FFFFFF33`.
*Why:* a legend that cannot be seen fails at the one job it has. This is a real
exception to "every colour resolves to a declared token", so it is written down and
bounded here rather than left for a reviewer to discover as an unexplained literal —
the token rule admits exactly this one deviation and no other.

**D-24 — 原生 and 直连 are two different properties of two different things, and neither
may appear in the other's sentence.** 直连 is a property of a **backend**: it means
`mode: direct`, and it is never an ink. 原生 is a property of a **hop**: it means
`native_cli`, and it is the cyan relation ink. Each word may be rendered wherever its
own subject is named — §1's copy tables are where those renderings live — and neither
may be rendered about the other's subject. The two fused into one label renders nowhere.
*Why:* they are independently variable, and the compound quietly asserts they are not.
A native source can sit in a gateway backend's chain — cyan wire into a 网关
group — and a direct backend reaches its vendor with no hop of any kind, so 直连 is not
a *sort* of 原生. Fusing them cost this document real errors on six frames: a legend key
that named a relation after a backend mode, and a status line that read 直连 · 正常 for
a supply path that does not exist (D-22). Splitting the words is what let both facts be
stated at all. E-4 is the escalation this closed.

**D-25 — Subscription channels are added one at a time; the product owes no combined
action.** Frame 04 is a radio group and its note reads 「两条都要也可以,分两次添加。」
*Why:* a both-at-once action has to define what a half-failure leaves behind, and every
answer is bad — roll back a login the user completed, or land in a state the user did
not choose. Two sequential adds have no partial state to define, cost one extra press,
and are self-evidently recoverable. This is a decision, not an accident of how the frame
happens to be drawn: a later lane that "improves" it into one press is re-opening a
question that was closed on purpose.

**D-26 — 切换到网关 against an uninstalled runtime neither refuses nor installs
silently: it names the component, prices it, and puts a button on it.** The confirm
gains a 会发生什么 bullet and its primary becomes 安装并切换.
*Why:* the user asked for an outcome, so 「你还没装网关组件」 is a report about the
product's internals rather than an answer — it hands back an error the person did not
cause and cannot act on from that dialog. Installing silently fails the other way: it
spends their disk and their minutes on something never named, and the first they see of
it is a progress bar. Naming it inside the consequence list costs one line and turns a
dead end into a decision. This was G-6; it is now ruled and the gap is struck.

**D-27 — A saved source always has a protocol with a named owner (catalog pin, user
declaration, or matching response proof); every path that cannot obtain that owner
produces "not added".** (2026-09-04 supersession of the 2026-08-26 response-shape-only
wording.)
> 已保存的来源恒有一个有主的协议;凡拿不到主的路径,产物都是「没有添加成功」。

*Why:* this is the property that makes 05's four exits derivable instead of negotiable.
③ refuses because nothing established a protocol; ④ refuses from Auto and leaves
through a declaration that authenticates; ⑤ may save because the protocol *is*
established and only the inventory is missing (E-5) — and an unknown inventory is a health
fact a source can carry, while an unknown protocol is a value every later request would
have to guess. Stated as a property rather than as a permission, it also decides cases
nobody has drawn yet: any future add path inherits the same test.

**D-28 — Each surface consumes its server-owned projection.** Default routing reads
`AgentSupply.sources`; Source cards read effective `adopted_by`; model rows/editors
read effective AgentChain with actual `manual_override` and `route_origin`.
Default Sources may be absent from matching tiers; manual routes may name other eligible
Sources. Neither membership nor adoption is live health. No frontend inventory matching
or array-equality heuristic derives route intent.

**D-29 — The page is 「模型」, the module is 「来源与网关」, and 「模型网关」 is never
rendered.** The project's name for this work is not a string in the product.
*Why:* the plan documents call the whole effort 模型网关, and carrying that onto a
surface produces two visible strings a level apart sharing a word — a page named 模型网关
containing a tab named 模型网关 — which reads as though the tab *is* the page. The
frames already do this correctly; the rule is written down so a lane reading the plan
files does not "fix" the page title to match them. Note that the design file's frame
names do contain 模型网关: they are canvas labels for the author, and D-17 already says
a frame's shell is not the shipped shell.

**D-30 — 切到直连 commits on the press; only 切换到网关 gets a confirm.** Adopting the
gateway opens frame 10 (§1.9); leaving it sends `PATCH /api/models/agents/<backend>/mode`
straight from the group head with no intermediate surface, and F1 in place is the whole
of its failure handling.
*Why:* a confirm is owed where an action is hard to take back, and these two directions
are not symmetric. Adopting changes where a backend's traffic goes and is the step frame
10 exists to explain, down to where the undo lives (`undo.1`, `undo.2`). Leaving
destroys nothing: the same dialog's third line already promises
that the sources stay and only stop supplying that backend `undo.3`, and §1.8's
*Retained sources* is the state that promise lands in. Putting a confirm on
the reversible half would make the product ask twice for the thing it just told the user
was free, and a confirm the user learns to click through stops protecting the adoption
that needed one. This is the same asymmetry D-15 applies to a cancel: the direction that
gives something back does not owe a gate.

**D-31 — A tier edit commits the whole `reasoning_efforts` list, so adding and removing
are one state, not two.** `PATCH /api/models/sources/<source_id>/models/<model_id>`
「replaces the complete capability list」, and §0.8 gives both directions the single
`Tier commit` row.
*Why:* the alternative is to model the chip UI's two gestures as two states, which then
owe two entry conditions, two failure treatments and — under the frozen rule against
enumerating request-level failure kinds — two strings for one rejection. Nothing
downstream distinguishes them: the body is the same array, the route is the same route,
and the server cannot tell an add from a removal either. The one place they genuinely
differ is where a rejected edit is put back, and that is a sentence inside the failure
treatment rather than a second row. See §1.6.

**D-32 — A deadline this dialog chose is re-read before it is believed.** *OAuth failed*
entered by the polling bound passing re-reads `GET /api/models/oauth/status/<flow_id>`
once when 重试 is pressed, and a `success` reading closes into 06 instead of cancelling
and starting a second flow.
*Why:* the other two entries into that state are readings — the provider said `failed`
or `cancelled`, and both are finished on arrival. The bound is not a reading. It is 15
minutes this file picked, spent only because `oauth-flow.schema.json` admits
`expires_at: null`, and what it establishes is that no answer came back inside it.
Believing it costs the user a sign-in they may have actually completed and hands them a
second one to complete again, while the flow object has been holding the answer the whole
time. One request, on a press the user has already committed to, is the cheapest check
available, and it is the only branch on which this dialog can end with the outcome the
user wanted instead of with a retry. The general form, which outlives this dialog:
**where a client-side timeout stands in for a contract field that is missing, the timeout
is a reason to ask again, not a verdict.**

**D-33 — Recovery is a re-dispatch, never a jump.** No exit may name one destination
for a payload it has not read yet. A state left by a new reading names the reading:
§1.0's *Starting*, *Impaired*, *Unreachable* and *Partial* hand payloads back to the
dispatch *Loading* performs, rather than to Ready, except that H1 first consumes a held
`install_start_switch` live reading to continue the already-promised mode `PATCH`.
*Why:* Ready is a conjunction — `health` `ok`, both page reads answered, at least one
source — and every exit that named it on recovery was asserting three facts on the
strength of one. The visible cost is a page that renders 网关运行中 over a source list it
has no row for, which is exactly the reading *Empty (no sources)* exists to give. The
structural cost is larger: a destination nobody derived cannot be checked, so the
register stops being a thing a program can read. *Sources unread* was already written
this way — 「the list decides, not the fact that one arrived」 — and this generalizes it.

**D-34 — Engine health and a page read are different grains, and both render.**
`degraded` is the shell speaking about itself; a failed source or supply read is a region
reporting on its own payload. The first does not consume the second — Impaired renders at
shell grain and the failed region renders *Sources unread* or *Partial* underneath it.
*Why:* an ordered dispatch that stops at the first true condition quietly makes health
outrank evidence. Impaired's treatment is F2, which only pays for itself when there is a
last good result to keep; on a first paint there is none, so stopping there left the
failed region with no unread line and no 重试 — under the one engine reading that makes a
failed read *more* likely, not less. §1.1's *Chain unresolved* is this rule one grain
further down, and it is why that row exists instead of a transition into Unreachable.

**D-35 — The collapse row is the chain collection's re-read control.** Collapsing a group and
expanding it re-issues one chain-collection read for that backend. §1.1's *Chain
unresolved* names it as the repair, and no per-row 重试 is added to the frame.
For a page-owned RO `unknown` whose exact-chain observation failed or did not match, this same
re-expansion is the only read retry: it reacquires one RO-O generation when Hub-observable,
retains the submitted pairs/stage and sends no mutation. A nonmatching answer installs the
current chain but does not settle or abandon the attempt; Direct-suspended keeps the evidence
and sends nothing.
*Why:* each collection member is row-grain, allowed to be unresolved, and drawn nowhere, so a row whose read
failed had `—` in three columns and nothing that would ask again — a dead end, which is
worse than the dead control D-9a rules out and is not licensed by it. A control already
on the frame, already meaning 「show me this group's rows」, re-reads them at no cost in
surface. The two alternatives were a per-row button on a surface with one row per model,
and a poll cadence this file has no basis to choose; the frame rules out the first and
「no exit keys on elapsed time」 rules out the second.

**D-36 — A lost response is reconcilable exactly when the client already holds its
subject's identifier.** F1 leaves a mutation's outcome unknown and the repair is always
a read; what decides whether that read is a *reconciliation* or merely a refresh is
whether the client can name what it is asking about. §1.2 holds `(backend, menu_model)`
and normalized submitted intent. Only the exact chain `GET` is presence-total for
the Route under comparison: manual intent must equal the submitted nonempty exact array;
restored intent must be `manual_override: null`, irrespective of changing inherited hops.
Array equality alone cannot prove intent. Legacy empty input and absent values compare
canonically as inheritance, never as an empty-disable state. `AgentSupply.routes` is a real
but optional schema member and this workflow deliberately does not consume it; the Agents
read settles mode/page authority only. Runtime annotations do not participate in the chain
comparison. Mode and Route attempt settlement are orthogonal: Direct mode makes that exact
read temporarily illegal but proves neither commit nor non-commit. RO therefore retains its
`attempt settlement × Route observation × owner × legality` product plus generation/submitted pairs/stage across Direct/Hub
forms. Every authoritative Hub landing first establishes CF-H, then closes LF-H only through
a Source observation acquired after that landing and invokes the same producer-independent
hook; `(unknown, page session, Hub-observable)` acquires exactly one chain GET. A matching
read enters M6; a nonmatch installs the latest chain but leaves the attempt unknown; a failed
read likewise retains evidence. Both permit only their registered chain-read Retry, never a
PUT. Reload is the sole implicit discard of this client-held evidence and creates no persistent
reconciliation ledger. In the mounted editor, exact nonmatch records `nonmatching@epoch`
independently: a sibling Source failure preserves it and admits only a Source read Retry;
after Source succeeds, CA-D7 may acquire only a newer exact-chain read. Explicit ET-8b
abandonment drops the old workflow; a later edit is a distinct workflow from current installed
authority. The API exposes no attempt identity or terminal-status endpoint, so the client
cannot prove that the old PUT died and **no automatic or recovery PUT resend exists**. Either a received
R6 success or that exact inferred commit enters M6 before visible handoff, and inference
marks the response-only `removed_hops` / `interrupted` members unavailable.
§1.6's *Refetch failed* holds the
`source_id` it sent, so `GET /api/models/sources` answers about that exact source and
settles the question. §1.9's *Failed* holds the backend, so the runtime and agent reads
settle each step it promised. §1.10's failed metadata save holds both the Source id and
the requested normalized values, so their exact appearance proves the commit; its failed
delete holds the Source id, so exact absence proves the deletion outcome. **That proof does
not bypass invalidation ownership:** the inferred save enters M1 and the inferred deletion
enters M2 before either visible exit. §1.11's RR-7 is the repair form: only a held blocked
origin now read clear is mutation-specific evidence, and it enters M3/M4 before a repaired
handoff. Because the response was lost, response-only impact/tail arrays are unavailable,
not empty, and no M row may invent them from the reread.
§1.5's ⑦ holds the `SourceCreate.client_nonce` generated before send: an exact Source-list
match proves commit. A miss permits one same-nonce resend on that user press;
`source_create_in_progress` returns to actionable ⑦ and schedules nothing, so only a
later user Retry starts another list-read + resend attempt. Committed
`source_nonce_conflict` rereads the list to claim the Source rather than replaying an old
response. §1.4's *Start failed* holds the exact `(client_nonce, vendor, channel)` tuple;
retry coalesces with the reserved start and returns its pending flow or settled result.
Both lost-response paths therefore reconcile exact producer identity without resemblance.
*Why:* the alternative is matching by resemblance — picking the nearest-looking Source
out of an unordered list, or reading no answer as no flow — and both are D-28's error in
a new place: a value read off the wrong subject and rendered as if it were the right one.
Writing the rule once also stops the register from re-deciding it per row: every state
answers the same two questions — can this exact subject be read, and if that read proves a
commit, which M row owns all projections the attempt may have invalidated?

---

## 3. Anchors into the behaviour spec

This file never restates the behaviour spec. Use these anchors:

All section titles below were **regenerated from `ca45aeb6`** — the current head of
#1215 — not from `master` and not from the `176b41b7` basis this file was first written
against. The three that changed there are §4.2, §4.3 and §4.6, and two of them changed
because the concept did: *resolution* became *configured-chain execution*, and *route
policy* became *configured-chain storage*. A register is the one place where a stale
title is invisible — it still reads like a citation — so it is rebuilt from the source
rather than spot-corrected.

| Question | Authority |
| --- | --- |
| What the product promises the user | `model-hub.md` §2 — *Product promise (user-facing, locked 2026-08-07)* |
| Which nouns UI copy may use, and which are required | `model-hub.md` §3 — *Vocabulary (v3 recut; UI copy uses only these nouns)* |
| What a source is and what it carries | `model-hub.md` §4.1 — *Supply — Sources (global assets, no ordering)* |
| How a chain is populated when a Source is added | `model-hub.md` §4.2 — *Gateway strategy — add-time defaults, then explicit configuration* |
| **How a request executes an effective chain — the sole authority** | `model-hub.md` §4.3 — *The only normative effective-chain execution algorithm* |
| Whether eligibility is client- or server-decided | `model-hub.md` §4.4 — *Configuration eligibility is server-authoritative (v3)* |
| Source states, self-healing classes, `detail_key` vocabulary | `model-hub.md` §4.5 — *State taxonomy — classified by "does it heal itself"* |
| How a configured chain is stored and mutated | `model-hub.md` §4.6 — *Route intent storage and mutation* |
| Downstream Agents | `model-hub.md` §4.7 |
| OpenCode identifier scheme | `model-hub.md` §4.8 — *locked 07-23, retained in v3* |
| Which module owns which class of configuration | `model-hub.md` §5 — *Surfaces — two modules, one understandable handoff* |
| Modes and onboarding | `model-hub.md` §6 — *Modes & onboarding* |
| Security boundaries | `model-hub.md` §7 |
| Explicit non-goals | `model-hub.md` §9 — *(v3)* |
| Behaviour acceptance criteria | `model-hub-implementation.md` §8 — *AC-1…AC-31, v3 addenda through 2026-08-09; AC-29/30/31 arrived with the `176b41b7` basis and are cited in §0.2, §0.5, §0.6 and §0.7* |
| Final contract shapes and their landing lane | `model-hub-implementation.md` §8, *Final contract shape handoff* — FC-01…FC-14 |
| Frozen wire contracts | `docs/plans/model-hub-contracts/` |
| Probe result shape (incl. saved-source recovery tests) | `model-hub-contracts/probe-result.schema.json` |
| Source shape | `model-hub-contracts/source.schema.json` |
| Chain shape | `model-hub-contracts/agent-chain.schema.json` |
| Routes | `model-hub-contracts/api.md` |

Three boundaries worth stating explicitly, because they are the places a lane would
otherwise write the same thing twice — or write it in two places that then disagree:

- **`model-hub.md` §5 owns module-level semantics; §1 here owns frame-level
  implementation.** §5 decides which module owns which class of configuration and
  which interaction rules govern the handoff between them. §1 decides element
  inventory, states, verbatim copy and extreme data for a given frame. These are
  different grains, so a genuine contradiction between them is a defect on whichever
  side misread the other's grain — there is no standing winner, and a conflict is
  escalated rather than resolved by precedence.
- **`model-hub.md` §4.3 remains the sole normative authority for routing
  resolution.** Nothing in this file may be read as modifying it. Where a frame
  displays a resolved chain, the frame is a *view* of §4.3's output.
- **`model-hub-implementation.md` §8 owns acceptance; this file owns the surface.** A
  statement that would hold or fail regardless of what is on screen belongs in §8 — and
  this file keeps no acceptance list of its own to put it in.
