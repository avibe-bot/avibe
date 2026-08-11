# Model Hub — UI & Interaction Spec (gateway frames)

Companion to `model-hub.md`. That file is the **behaviour** authority; this one is
the **surface** authority: what each frame shows, which states it can be in, the
exact words it says in both locales, and what it does when the data is ugly.

It exists because a design file answers only *what it looks like*. It does not
answer *why it looks like that*, *how one state becomes another*, or *what happens
at 40 sources instead of 4* — so every implementation lane invents its own answer,
and the answers disagree. Everything below was decided while the frames were being
drawn; this file is where those decisions land instead of evaporating.

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

There is no 07: it was removed during the design pass and the remaining frames
were deliberately **not** renumbered, so that every existing reference to "08"
keeps pointing at the same picture.

**Thirteen frame exports are covered; twelve are specified.** Frame 02's interaction contract is
not in this file `[contract-gap]` G-32. Its drawing is merged and §1.2 records the
decisions behind it, but the mutation the editor owns has no state in §0.8, and request
sequencing, guard refusal, lost-response reconciliation and failure copy are exactly the
class of fact a drawing cannot carry (§0.2 item 5). Rather than half-cover it, 02 is
stated as excluded: the route sits in §0.4's table with that reason, the debt sits in
§0.5 as G-32, and a separate round writes the section under the same register-and-gate
discipline as the rest. Everything this file says about the covered frame set still
includes 02, because those are readings of a drawing and the drawing is there.

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

**Verification basis.** Every anchor and every `[spec]` / `[contract]` claim below
was checked against **`ceace07f`** — the squash of the spec lane's PR #1215, and an
ancestor of `master` — **not** against the pre-#1215 `master` whose §3, §4.1, §4.2,
§4.6 and §5 it supersedes. Every anchor this file cites is reachable from `master`
today, and the standing caveat that this file must not merge before #1215 lands is
retired by #1215 having landed.

The basis moved three times while this file was being written: `7984aabf` → `176b41b7`
→ `ca45aeb6` → `ceace07f`. The first move added AC-29/30/31, and **AC-30** (takeover is
derived, and a chain with no runnable hop renders none of takeover's visual semantics)
and **AC-31** (Direct is a mode and the first state of an existing install; Native names
a hop and never a mode) land directly on frames 08, 09 and 10. The second move is
**S-1**, and it is larger: a configured chain is now stored configuration executed as
written, with no runtime Source/model matching, no `follow | custom` state, and no
second projection derived from a backend order. The third is the remainder of #1215's
own review — five commits between `ca45aeb6` and the squash, sixteen contract files,
1245 insertions against 1000 deletions — and it is the one move this document did not
re-read commit by commit. It did not have to: the contract artefacts in this worktree
are `ceace07f`'s byte-for-byte, so §0.10's class E re-derives every `[contract]` claim
here from the landed text on every run, and drift against the basis is a red gate
rather than a reading.

Re-reading on each move is not bookkeeping, and this round proves it twice over. Two of
the three frame-versus-contract conflicts in §0.6 existed only at `176b41b7`. And at
`ca45aeb6`, **D-28 turned out to be ruling on `order_enrolled_by`, a field S-1 deletes**
— a decision that still read as a live citation because a citation to a removed name
looks exactly like a citation to a present one. Nothing flags it; only re-reading the
basis does. It is re-derived in §2 with that history attached rather than quietly
repointed.

**What S-1 deleted, and what this file does about it.** §1.1's legend note,
`gateway.row.followsOrder` / `gateway.row.custom`, and the whole of §1.2's follow/custom
machinery specified a derivation S-1 abolishes. They were `[frame]` strings measured from
frames 01 and 02 as they were drawn before the rebuild. **Frame 02 has since been redrawn
under S-1 and merged**, so the resolution is no longer a scheduled rewrite: the stale
specification is deleted, and `design.pen` is left to say what those surfaces contain.
§1.2 keeps only the question the frame answers, the fact that the mode choice is gone,
and a pointer; §1.1 keeps the same for its legend note and the two model-row keys. The
rule this follows is the one this file uses everywhere: **a decision may be written down;
a fact about a drawing may not be written down twice.** Prose that reproduces a frame is
a second copy of the frame, and second copies go stale silently — which is exactly how
these three passages survived S-1 in the first place.

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
and several do: the frozen `agent-supply.schema.json` has neither `hops` nor
`adopted_by`, and FC-05 states both. What an FC item may not do is stand in for a shape
nobody has stated. Where it only *requires* another file to contract something and that
file does not — FC-12 requires `api.md` to carry `adopted_by` for the surface that uses
it, and every read `api.md` defines omits the field, which is G-20 — the value has no
definition anywhere the surface can reach, and the claim is `[contract-gap]` with a
registry row. The two cases look
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
is missing. The five below are neither.

Four of them belong to a surface outside this frame set, so silence about them here is
a boundary, not an omission. The fifth is a different kind of row and its own cell says
so: the route-chain `PUT` is drawn **inside** this frame set, by frame 02, and is
excluded anyway, because 02's interaction contract has not been written and a separate
round takes it. The distinction matters because the two kinds are fixed differently —
a §0.5 row is work someone still owes, a row here is normally work that lives somewhere
else — and because leaving any of them unwritten reads identically from inside this
file: a capability the product has and nobody can find. The chain `PUT` is the case
where both are true at once, so it is written in both places: excluded here so this
document's accounting is complete, and registered as G-32 so the debt has an owner.
A row here is therefore not a claim that nobody owes the work; it is a claim that this
document is not where the work lands.

| Contracted route | Where it lives instead |
| --- | --- |
| `POST /api/models/migration/scan` | The migration surface. None of these frames offers an import, and a scan with nothing to show it is not a screen. This is also the one row here that is not a mutation — the read-only `POST` counted out above — and it is listed anyway, because the question this table answers is 「where is this route drawn」 and not 「what does it write」 |
| `POST /api/models/migration/apply` | The migration surface, following its own scan |
| `PUT /api/models/agents/opencode/menu` | The open-menu configuration surface, which is where a menu is chosen; frame 01 renders the resulting supply and never edits the menu behind it |
| `POST /api/models/agents/<backend>/probe` | Diagnostics. It answers "would a turn resolve right now", which none of these frames asks — 01 reports the supply it already has, and a probe run from a page that is not asking would report on something the user is not looking at |
| `PUT /api/models/agents/<backend>/chain` | Frame 02 draws the editor that owns it and a later round specifies it; this document does neither. This is the one row in this table excluded from *inside* the frame set: §1.2 records the decisions behind the redrawn editor and states no build requirement, so this document holds no state for the mutation, no reading of its guarded `409`, no lost-response reconciliation and no failure copy. Excluded because 02's interaction contract has not been written and a separate round takes it, not because it lives on another surface; the debt is G-32 |

### 0.5 Contract-gap registry

Every `[contract-gap]` in this file, in one place, with retired rows retained as an
audit trail. A live `[contract-gap]` statement describes the intended surface, and is
**not** a requirement on the build: where a frame draws an affordance that sits on a gap,
the section that owns that frame says so explicitly rather than quietly requiring it.

The evidence column is re-verified against the contract each time the branch takes a
merge, and names the commit it was verified at rather than the commit it was first
written at — a citation to a stale baseline reads exactly like a citation to a live one.
All live-gap evidence below was re-verified at `ceace07f`, which is #1215's squash: the
frozen contract artefacts are now on master, so these are gaps in a settled contract
rather than in a moving one. A row whose Missing cell is struck through has since been
withdrawn or registered; its final column records that disposition instead of claiming
the absence is still live.

| # | Surface | Missing | Evidence / disposition (contract baseline `ceace07f`) |
| --- | --- | --- | --- |
| G-3 | 06 model inventory | a way to retire a *discovered* model from a source's inventory, **and a place to remember that it was retired** | the ruled delete route removes a manual entry only; no other inventory-shrink route is user-initiated, and `source.schema.json`'s `models` carries no per-model retained flag |
| G-9 | ~~03 order save that drops sources~~ **withdrawn — this was never a gap** | ~~the guarded-change response for the whole-order `PUT`~~ nothing | The row read the absence of a `409` branch on the whole-order `PUT` (§1.3 Saving names the route; a withdrawn row deliberately does not, so it can excuse nothing) as something `api.md` still owed, on the strength of FC-12's 「row-for-row」 clause. It owed nothing. `model-hub.md` §4.5's Source-mutation envelope matrix is declared **authoritative and exhaustive** over 「all Source/inventory mutations, including writes that cannot remove supply」, and its eight rows do not include this route; FC-12 names 「the explicit backend Source-order PUT」 as a separate item from the mirroring clause. So the whole-order write's success echo, with 「no policy state exists」 beside it, *is* the mirror, and the absent `409` is the contract agreeing with S-1 and D-9 that reordering reaches no existing chain. Kept as a withdrawn row rather than deleted, because the number is cited in this file's own history and because a gap register that silently loses entries cannot be audited. The row names no route and quotes no body, so there is nothing left in it for a checker to excuse — but a withdrawn row is still a *number* the register defines, and a citation resolves against numbers, not against verdicts. So the rule that keeps it inert is written here rather than enforced: **no surface may carry `[contract-gap]` G-9**, and the number is not reused |
| G-10 | 01 shell pill, install in flight — **and 08's 安装并切换**, the other press that promises one | a server-side install state, and the route that enters it | `runtime-dependency.schema.json` v5's `status.health` runs `ok · degraded · down · not_started · not_installed` with nothing between the last two, and `api.md` contracts exactly two runtime routes — `GET /api/models/runtime/status` and `POST /api/models/runtime/start` — with no install route at all. So *installing* exists on the client and nowhere else, and a reload during one reads back as `not_installed`. §0.8's Installing and Install failed rows are the client-side states that gap forces, and they are marked as such. The second caller is D-26's confirm: its primary promises install → start → switch, and only the last two are routes, so its 安装 is the same client-side state and its row carries the same marker. One missing route, two surfaces that press it |
| G-11 | 09 direct-only home, zero backends | an installation flag per agent backend, and the payload that carries it | No property of `AgentSupply` (`agent-supply.schema.json`) reports whether a backend's CLI is present, and `core/handlers/model_hub/service.py:list_agents` builds its array from a literal three-backend tuple, so the payload is length 3 unconditionally and the zero-backend state cannot be produced from it |
| G-12 | 01 upstream card and 06 header, `needs_action` — **registered by frame 12** | ~~the control that replaces a dead credential~~ nothing | §1.11 registers the two repair producers drawn on the source cards: 更换 Key sends the credential-replacement flow to `PUT /api/models/sources/<id>/credential`, and 重新授权 starts `POST /api/models/sources/<id>/reauth`. §1.1 and §1.6 cite that owner instead of pointing at each other. Kept as a registered row so the former absence and its closing frame remain auditable |
| G-13 | 03 order drawer, chains that already exist | an action that re-applies the current order to chains built before it, and the route behind it | `api.md` contracts `PUT /api/models/agents/<backend>/sources` for the order itself and `PUT /api/models/agents/<backend>/chain` for one model's hops; nothing bulk-rewrites stored chains, and `model-hub.md`'s `placement-v1` runs "only during Add Source". So the order genuinely cannot reach an existing chain, and §1.3 says that rather than implying the drawer keeps chains in sync |
| G-14 | 08 adopt-gateway confirm, `effects.1` | the adoption itself: turning the backend's existing CLI login into that backend's first `native_cli` Source | `core/handlers/model_hub/service.py:set_agent_mode` (`ceace07f:2197`) validates the mode, assigns `agent.mode`, saves, and returns the payload — it creates no Source, writes no order, and has no other caller that does. The promise is a product decision and stays, because a switch that silently drops the user's working login is not a switch anybody would accept; what is missing is the code that keeps it |
| G-15 | 06 source detail, a source's own name and Base URL — **registered by frame 11** | ~~any affordance that edits them~~ nothing | §1.10 registers the overflow action, edit dialog and guarded `PATCH /api/models/sources/<id>` producer drawn in frame 11. Kept as a registered row so the former absence and its closing frame remain auditable |
| G-16 | 01 upstream card and 06 source detail — **registered by frame 11** | ~~any affordance that removes a source~~ nothing | §1.10 registers the overflow action and the source-removal guard dialog drawn in frame 11 for `DELETE /api/models/sources/<id>`. The existing 06 model-row 移除 remains a different operation. Kept as a registered row so the former ambiguity and its closing frame remain auditable |
| G-17 | 04 add-subscription, a flow that expects something pasted back — **registered by the 04 paste-back exhibit** | ~~the field that takes it, and the control that sends it~~ nothing | §1.4 registers `nOgMQ`'s paste-back dialog and its `POST /api/models/oauth/submit` producer. The drawn `paste_code` variant supplies the frame geometry; `presentation.expects` selects the registered code or callback-URL copy without changing that geometry. Kept as a registered row so the former absence and its closing exhibit remain auditable |
| G-18 | 05 add-by-key, 拉取型号 and the observation 添加 runs before it saves | the route that carries a non-persisting observation of a source that does not exist yet | **The behaviour is contracted and the route is not**, so this row registers a gap *inside* the contract rather than an undecided question. AC-26 states the operation outright — 「Add Source exposes one non-persisting submission that combines connectivity classification with response-backed protocol observation」, returning classified reachability, authentication and a protocol 「without persisting a Source」 — and `model-hub.md`'s protocol-observation ruling of 2026-08-09 requires every stored `protocol` to trace back to a real response taken *before* Save, so an observation that saves nothing is not optional to the design. None of `api.md`'s 28 route rows accepts one: `POST /api/models/agents/<backend>/probe` is backend-scoped (`{model?}`) and reports on the configured chain, `POST /api/models/sources/<id>/refresh` needs an `id` only Save produces, and `POST /api/models/sources` persists on success. §1.5 keeps every Pull-origin state, because the operation is contracted and 05 draws it; what is missing is the way to invoke it |
| G-19 | 05 add-by-key, 取消 pressed while a persisting add is in flight | what the server is left holding when the cancel lands after the transient phase | **Cancellation is contracted everywhere except the persisting half.** AC-26 requires an API-key test's success, authentication failure, adapter error, timeout **and cancellation** each to 「revoke the transient provisioned ref before the operation settles」, with a durable pending-revocation record and reconciliation behind a fault-injected revoke failure, and repeats that guarantee for unsaved model discovery; `api.md` contracts `POST /api/models/oauth/cancel` for the subscription branch. No artefact states the outcome once `POST /api/models/sources` has crossed out of that transient phase into persistence — whether a cancel there yields a Source or nothing. §1.5 states the guarantee for the phase that has one and stops at that seam rather than extending AC-26 over a boundary AC-26 does not cross |
| G-20 | 01 source card, 被哪些链收编, **and 06's status bar, which splits `active` on the same field** — on any load that is not the creation response | a *read* that carries `adopted_by` | **The contract names this exact consumer and then gives it nothing to call.** `api.md:212` defines `AdoptedBy` as 「the stable Source-card projection of persisted references」 and FC-05 requires `agent-supply` to expose it, which is what makes the field authority rather than an invention (§0.3). Every place it is actually contracted is a creation terminal: `POST /api/models/sources` → `{source, added_to, adopted_by}`, and the OAuth completion reading — `api.md`'s *OAuth completion* section, which makes the terminal shape a function of `OAuthFlow.intent` and puts the same three arrays behind `intent: "create"` and explicitly not behind `intent: "reauth"`. The reads a loaded page can call carry no trace of it: `GET /api/models/sources` → `{sources: Source[]}` and `source.schema.json` has no backend-attribution property at all; `GET /api/models/agents` → `{agents: AgentSupply[]}` and `agent-supply.schema.json`'s 13 properties do not include `adopted_by`, nor does `api.md:628`'s CI-guarded AgentSupply serializer-completeness row. So the projection survives exactly one response and a reload has nowhere to read it from, while D-28 forbids the one derivation that would rebuild it from `hops`. This is the §0.3 shape where an FC item requires another file to carry something that file does not, rather than FC-05's `hops` shape where an FC item states a shape the frozen schema omits |
| G-21 | 01 upstream card, 添加订阅 → 04 — **registered by frame 13** | ~~the step that picks which vendor the subscription is for~~ nothing | §1.12 registers the vendor menu drawn in frame 13. Claude 订阅 passes `anthropic` and ChatGPT 订阅 passes `openai` into §1.4 before that dialog renders its vendor-specific title, options and `POST /api/models/oauth/start` request. Kept as a registered row so the producer/consumer break and its closing frame remain auditable |
| G-22 | 06 for a source just added — the add flow's terminal, reached from §1.4's *Awaiting sign-in* and §1.5's ② | an element that renders `added_to` | **The contract answers a question no frame asks.** `POST /api/models/sources` and the `create` OAuth terminal both return `added_to: AddedTo[]`, an entry naming `backend`, `menu_model`, `source_id`, `model_id` and `position`, the last one-based in the persisted Route chain after commit `[contract]`. Add-time placement (`model-hub.md` §4.2) is what makes it worth showing: the source the user just added has been written into chains they did not open, and this array is the only statement of where. It has the same one-response lifetime as G-20's `adopted_by` — `GET /api/models/sources` and `GET /api/models/agents` carry no trace of it, so the landing cannot be recovered by a later read — but it is the weaker of the two gaps, because nothing in this document *claims* to render it: G-20 has drawn consumers left without a read, while this row has a read left without a consumer. §1.3 states the retirement of the hint row that used to cover the old behaviour, and names this number rather than specifying the element, because §0.2 leaves that to the frame |
| G-23 | `Qp6FI`, the shared guarded-change confirm — both callers, §1.6's *Refetch refused* and *Guard refused* | a body block that lists `would_interrupt` | **The refusal carries a list and the dialog draws a sentence.** `would_interrupt` is `SupplyGap[]`, each entry `{backend, model_id, agents}` with `model_id` the protected **menu** model and `agents` the enabled named Vibe Agents that pinned it `[contract]`, and `model-hub.md` requires that 「the confirm copy names affected Agents when any exist」. `Qp6FI` as measured has exactly one label, one count pill, one row list and one hint line, and all four are the `would_remove_hops` side; the only rendering of the gap array is `guard.hint.interrupt`, one sentence that reports the array is non-empty. The strings are specified — `guard.gap.label`, `guard.gap.subject`, `guard.gap.agents`, with `gateway.modelCount` as the pill — because copy is this document's register and an authority requires these; the block that holds them is drawing, so it is a gap rather than an invention. The same absence makes `source_last_supplier` unrenderable: its `api.md` example carries `would_remove_hops: []` beside a populated `would_interrupt`, which this dialog would draw as an empty list under a bare sentence |
| G-24 | 01 run pill, *Unsupported host* — the one pill reading that splits a `health` value on a second field | a host-platform or installability discriminator in the runtime payload | **The pill splits `not_installed` on a field the payload does not carry.** `runtime-dependency.schema.json` v5 reports `manifest.assets[].platform` for each *published* asset and nothing that says which platform the **host** is: `status` carries `installed_version`, `verified`, `listening`, `health` and `last_check`, and no property anywhere in the document names the machine the gateway would run on. The client cannot supply it either — the browser's own platform describes whichever machine has the page open, which on a remotely opened UI is not the host, and substituting it would be the same class of error as computing supply from live chains (D-28): a value read off the wrong subject and rendered as if it were the right one. So the split has no contracted input, and §1.0 states what a build does instead of requiring the guess |
| G-25 | 01 gateway group, the collapse predicate | a per-model fact that separates a chain with a live hop from one whose hops are all stale | **The only per-row number the page-level payload carries counts configuration, and says so.** `agent-supply.schema.json`'s `model_supply` rows require exactly `model_id` and `chain_length` under `additionalProperties: false`, and `chain_length`'s own description reads 「Length of the exact configured Route chain, including cooling and process-unavailable native CLI hops. It is not a count of currently runnable candidates」, with 「Every stored hop remains counted regardless of live runnability」 beside it. `model-hub.md` §4.6 then keeps a hop whose Source is gone or whose model is no longer advertised **on purpose**, 「until the Source recovers, the user removes or changes the pair, or a guarded cascade removes it」. So a chain made entirely of stale hops reports the same nonzero number as a healthy one, and `supply_status` is one word for the whole backend. The per-model chain read can see the difference and §1.1 forbids visibility from depending on it, for reasons that hold whatever this row is; what is missing is the same fact at page grain |
| G-26 | 03 order drawer, a reorder | a policy value that reads the stored Source order | **The order is contracted, and nothing the contract carries consumes its sequence.** `model-hub.md` §4.6 persists one `sources.order: string[]` per backend and calls it 「a visible Gateway configuration and Add-time placement input」; §4.2's only policy value, `placement-v1`, appends 「a newly added Source to each configuration-eligible backend Source order」 and 「every accepted exact match to that menu model's current Route-chain tail」 — an append at both ends, which lands in the same place whatever sequence the order is in. §4.2 also states that 「the only order runtime can execute is the exact hop order stored for that model」, so execution does not read it either. Membership is real and durable — a Source deletion prunes the id in the same transaction and preserves survivors, and reload rejects a dangling one — and G-13 already records that the order cannot reach a chain that already exists. This row is the other half: under the current policy value it does not reach a chain that does not exist yet either. §1.3's copy now says what the drawer stores rather than naming a consumer |
| G-27 | 05 add-by-key, the persisting `POST /api/models/sources` — and §1.4's `create` terminal, which answers with the same three arrays | the request shape that route accepts | **The response is fully contracted and the request is a name.** `api.md`'s route table gives `POST /api/models/sources` as 「`SourceCreate` → `{source: Source, added_to: AddedTo[], adopted_by: AdoptedBy[]}`」, and `SourceCreate` occurs in no other line of `docs/plans/`: no schema file, no field list, no example body. `source.schema.json` cannot be read backwards into one — it is the response entity, `additionalProperties: false` over sixteen properties including the server-assigned `id`, `created_at`, `state` and `usage`. What the dialog holds is a Base URL, a key, an optional name and, on the two exits that reach persistence after a probe, a protocol proved by a real response plus an inventory that may be empty; which of those the body carries, and how 「an empty inventory」 is expressed in it, is undefined. §1.5 keeps the persisting exits because the frame draws them and AC-26 requires the observation that precedes them — what is missing is the body they send |
| G-28 | `Qp6FI`, the hop rows — §1.6's inventory callers and frame 11's source removal | the hop's position, on the reference the refusal returns | **The rows draw position pills and the refusal names no position.** `model-hub.md` §4.5 returns 「ordered `would_remove_hops` entries naming each `(backend, menu_model, source_id, model_id)` reference」 — four fields, and no index among them. The array's own order is not the answer: it spans every backend and menu model the change touches and carries only the hops being removed, so an entry's place in it is not its hop's place in its chain. Deriving it means one `GET /api/models/agents/<backend>/chain?model=<id>` per distinct `(backend, menu_model)` the refusal names, issued from inside a confirm, each async and each allowed to fail — the dependence §1.1 refuses for the collapse predicate, arriving on a surface that has to be right the first time. So `guard.hop.position`, `guard.hop.position.removeSource` and their `{{n}}` stay in the register, because copy is this document's register and both frames draw the pill; the input is what is absent, and §1.6 and §1.10 state what the row renders until the reference carries one |
| G-29 | 05 add-by-key, ⑦'s 重试 — the re-read F1 requires of a `POST /api/models/sources` that may have committed unseen | anything the client holds *before* the send that the committed Source can afterwards be recognized by | **The repair is contracted and its input is not.** F1 has a create whose response was lost re-read before it is re-sent, and `GET /api/models/sources` is the only read that answers `[contract]` — but nothing the dialog sent survives into what comes back. `Source.id` matches `^src_[a-z0-9]{8,}$` and is server-assigned, so it existed only in the response that died; `source.schema.json` returns no plaintext credential; and neither `base_url` nor `display_name` is declared unique in `api.md` or `model-hub.md`, so two Sources may legitimately carry both. No route in `api.md` takes an idempotency key, and G-27 leaves `SourceCreate`'s body unspecified, so this file cannot even say the client *could* supply one. The read therefore answers 「what is in the list」 and never 「whether this create is in it」, and §1.5's ⑦ states that instead of picking a Source out of an unordered list by resemblance |
| G-30 | 04 add-subscription, *Start failed* entered by a lost response | a way to reach a flow whose `flow_id` never arrived | **A start that was accepted and not answered leaves a flow the client cannot name.** `api.md` contracts exactly four OAuth routes — `POST /api/models/oauth/start`, `GET /api/models/oauth/status/<flow_id>`, `POST /api/models/oauth/submit`, `POST /api/models/oauth/cancel` — and the last three all take the `flow_id` the lost response was carrying. There is no flow-list read, and `oauth-flow.schema.json` carries no property that would let one be found from what the dialog does hold: the vendor and the channel it sent are what *every* start for that backend sends. So 重试 can start a second flow beside a first that is still live, and the first ends only by expiring — `expires_at`, when the provider supplies one `[contract]`. §1.4's *Start failed* states that rather than asserting the call never reached the provider, which is the reading this row replaced |
| G-31 | 01/08 model rows — the 当前 line (`gateway.row.current`, `gateway.row.currentTakeover`), the violet reroute, and §1.1's *Takeover active* entry and exit | the chain's **current** hop, on the one read that is supposed to carry it | **The behaviour spec puts it in the read projection and the wire shape closes the object without it.** `model-hub.md` §4.3 states 「the read projection is `C` with live annotations plus `current`, never a reconstructed provider list」, writes takeover on that field — 「the current hop is not `C[0]` and `C[0]` is unavailable for a recoverable quota/cooldown reason」 — and then fixes the timing: 「recovery changes current execution position **on the next turn** without changing `C`」. The read that would deliver it is `GET /api/models/agents/<backend>/chain?model=<id>` → `{chain: AgentChain}`, and `agent-chain.schema.json` is `additionalProperties: false` over exactly `contract_version`, `backend`, `model_id`, `chain` and `supply_state`, and each hop inside `chain` is closed the same way with no `current` among the keys it does carry. The substitute is the schema's own sentence — 「the next turn uses the FIRST item with `runnable: true`」 — and it agrees with `current` everywhere except the interval §4.3 legislates: from the moment a cooling head is reported runnable again until the next turn actually moves back, the derivation names the head and `current` still names the later hop. That interval is this row's entire content, and it is why the gap is narrow rather than structural: everything else the 当前 line and the takeover visuals need is on the wire. `model-hub-implementation.md` books the missing piece as AC-30's own lane I2, 「live current-hop input」, so what is absent is the field and not the decision. This is G-20's shape — one contract file naming a value another file must carry — on a different value, a different read and a different repair, so it is registered beside it rather than folded into it |
| G-32 | 02 route-chain editor — the frame this document points at and does not specify | the editor's interaction contract: how it sequences a save, what it does with a guarded refusal, what it reconciles against when the response is lost, and what the failure line reads | **The route is contracted, the surface is drawn, and the specification is what is missing.** `api.md` carries `PUT /api/models/agents/<backend>/chain` as one of the nineteen state-changing routes, guards it with a `409` refusal, and declares it 「the `mutation.route_replace` row of the authoritative mutation matrix」; on success it returns `{chain, removed_hops, interrupted}`, so the refusal branch, the removal report and the interruption report all exist on the wire. `Q1dkS` is merged, so the controls exist in pixels. §1.2 states no build requirement, which leaves §0.8 with no state for the write and this file with no reading of any of those three response members. That is a different absence from every other row here: those register a behaviour the contract has and no frame draws, and this one registers a frame the contract *and* the drawing both have, which no section writes down. It is therefore also the one row paired with a §0.4 exclusion — the exclusion keeps this document's accounting honest about the route, and this row keeps the debt from being read as a boundary. A separate round writes the section |
| G-33 | 04 add-subscription, a flow whose declaration carries a device code | anywhere to display or copy `presentation.device_code` | **The remaining contracted value has nowhere to land.** `oauth-flow.schema.json`'s Form B is `auth_url + device_code + expects none`. This round gives every `presentation.instructions_key` a PD-4 helper-line consumer, including the null/unresolved fallback for Form B, so that former half is closed. The paste-back exhibit accepts and submits Forms A and C, but Form B submits nothing and instead needs a read-only device code the user copies out. The new input cannot render that output, so G-33 remains independently open |

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
menu, and must never be silently skipped.** That lands on machinery §4.6 and §4.4 already
have — the retained hop with its live non-runnable reason, and `chain_length: 0` driving
「无来源可供」 — with **no new field and no new mechanism**. §1.6 states it as a rule now,
not as a gap.

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

**G-3's remaining half is unaffected, and stays open.** Retiring a *discovered* model
still needs somewhere to remember the retirement, and the ruling above gives it nothing:
a menu marker describes what supply currently is, while a retirement is a user position
that has to outlive the next refresh. The two were named as one field earlier in this
document because they looked like one question; the ruling answers only the half that a
projection could answer.

**G-3 and G-7 are one principle applied twice, not two independent decisions.** The
principle: *a fact the system can project from what it already stores does not become a
stored field, and a capability that would need a stored field nobody has asked for does
not become a capability.* G-7 is the application that **kept** the capability — what had
to be remembered, that a discovered id stays in after upstream dropped it, turned out to
be projectable from the hop that already names it, so the requirement moved onto a
projection and no per-model record was added. G-3 is the application that **drops** the
capability: retiring a discovered id is projectable from nothing stored, so it would need
a real per-model retention marker — and, with it, a rule for how that marker merges on
refetch, a rule for who owns a conflict between the marker and the source's own answer,
and a rule for what a chain hop naming a retired id resolves to. Three new rules for a
capability no user has asked for. Deletion comes before optimisation, so the surface
carries no retire control for discovered rows at all (§1.6), removal stays scoped to
manual models, and G-3 stays a stated gap against the day the demand arrives.

*The earlier framing was that these two were the same missing field seen from opposite
sides* — G-3 to remember that a discovered id *stays out*, G-7 that it *stays in* — so
one user-intent record would answer both, while two boolean flags would make refresh
semantics depend on write order. The reasoning about the flags still holds; the merge did
not. One fix closed one, which means they were two the whole time and the merged entry
was hiding it. What they share is not a field. It is the rule for deciding whether to add
one.

**G-3's first half has no surface, and that is the decision rather than a deferral.** A
user-initiated retirement needs a route (half one) *and* a durable marker saying this
discovered id stays retired (half two) — otherwise the next inventory refresh re-adds it
and the control reads as broken rather than absent. The contract represents neither, so
this file specifies neither: §1.6 states that no surface claims the capability, and the
retention half is handed to the AC ledger in §0.7. Specifying half one without half two
is how a document starts promising a control that cannot keep its promise.

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

**G-2 and G-4 stopped being gaps when the basis moved to `176b41b7`, and both became
conflicts.** A gap is silence; a conflict is a sentence pointing the other way, and
the two need opposite handling — a gap is closed by adding, a conflict by somebody
retracting. G-2 was 06's protocol-edit entry having no route: at `176b41b7` the UI
landing checklist says the protocol selector exists *only* after a failed observation,
as a probe-order hint, so the contract now **forbids** the control rather than merely
omitting its route. G-4 was the quiet badge having no field to read: AC-27 now states
「the stored shape has **no** manual/automatic provenance marker」, which is not an
absence to be filled but a decision that the badge cannot be rendered. Both fold into
E-2 in §0.6, and their numbers are struck rather than reused.

### 0.6 Conflicts raised by this pass — all five now ruled

Five places where the owner-approved frames and the behaviour authority at `176b41b7`
said different things. All five are closed by owner rulings dated 2026-08-09. Each is
kept on the record rather than deleted, because a resolved conflict is evidence about
how the next one should be handled — and because three of the five moved the *design*,
which is the argument for escalating instead of quietly writing down whichever side
this lane had already drawn.

A conflict is not the same thing as a gap. §0.5's G-3 is *missing* contract — something
has to be added. The five below were *contradicted* contract — something had to be
retracted. Filing a contradiction as a gap is how a lane talks itself into implementing
the side it happened to draw.

Four of the five were new at `176b41b7`, and they arrived the same way: the frames were
rebuilt on 2026-08-09 to the owner's rulings, and the ledger grew on 2026-08-09 to the
owner's rulings, and the two passes did not see each other. That is worth saying plainly,
because it is the argument for re-reading the basis rather than trusting a stale
verification — neither side is careless, and neither can be found by reading only one.

**E-1 is closed, and it was the design that moved.** It read: *is the source order
global, or one subset per backend?* The frames drew one product-global order with
native sources held out of it (「全局顺序」, 「跟随全局顺序」, 「全局 #n」, and 03's
「不参与排序」 section); `model-hub.md` §3 at the spec lane's head defines 来源顺序 as
an ordered subset eligible for **one backend** and 「never product-global」, bans 优先级
as a global noun, and computes the order by a rule whose first step *includes* the
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

**E-2 is closed, and it was the design that moved.** It read: *can a stored protocol be
changed, and can the surface tell that a human chose it?* One half had already
narrowed — 05's `undetermined.hint` used to say the value was editable later, and the
rebuilt frame says 「保存后不可更改」. What remained was the standing instruction to put a
protocol-edit entry point at frame 06's quiet badge, plus the badge tooltip written to
match it, and at `ca45aeb6` the contract still rules against the control twice over: the UI
landing checklist allows a protocol selector 「**only after failed observation**」 as a
probe-order hint, which excludes one on a saved source's detail page; and AC-27 states
「the stored shape has **no** manual/automatic provenance marker」, so a badge drawn only
for human-supplied interfaces has nothing to render *from*.

The owner ruled for the behaviour spec. **The badge is gone from frame 06 and the edit
entry point with it**, and 05 state ④'s hint carries the whole rule instead —
「提示只改探测顺序 · 仍要真的连上才会保存;保存后不可更改」. §1.6 no longer specifies a
badge, an `interfaceBadge` copy row or a tooltip, and no `UI-n` quantifies over one.
The second contract bullet is why the ruling was inevitable in hindsight: it killed the
badge whichever way the *edit* question went, because a conditional control whose
condition is an absent field cannot be drawn at all.

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
proved, inventory unavailable」.* It was the only one of the five where the contract asked
for **more** surface than the frames drew. AC-27 says 「『Add anyway』 is available only
after the protocol was proved and a different result, such as model inventory, remains
unavailable; that uncertainty is a health fact.」 The rebuilt frame 05 drew neither that
nor E-3's version, because the rebuild removed 仍要添加 from every failure state — its two
failure states were 401 (③) and unrecognized interface (④), and both are
protocol-*not*-proved.

The owner ruled that the two 仍要添加 are different affordances wearing one word, and that
this one is legitimate. Frame 05 now draws **state ⑤** (`d6bFlX`): the interface was
recognised, the second fetch came back without an inventory, and the foot offers
取消 / 仍要添加 / 重试. §1.5 specifies it and §2's D-27 states the property it protects:

> 已保存的来源恒有一个被观测证明过的协议;凡拿不到证明的路径,产物都是「没有添加成功」。

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

Two behaviours the frames imply, which no AC through AC-31 covers and which this lane
does **not** write into any document. Both are in the PR description under 「建议移交 AC
账本」 for the spec lane to route; they are named here only so that a reader of §1.6 and
§1.0 can see that the silence is deliberate:

- **Retiring a discovered model needs somewhere to remember the retirement.**
  `source.schema.json`'s `models` describes what an inventory refresh found; it carries
  no per-model retained flag, and the ruled `DELETE /api/models/sources/<source_id>/models/<model_id>`
  removes a manual entry. So a user-initiated retirement of a *discovered* id has no
  representation that survives the next refresh. This is the second half of G-3, and it
  is the reason this file states only that no surface claims the capability, rather than
  requiring the control.
- **An install in flight has to be observable by whoever asks next, and asking twice must
  not download twice.** This is G-10's behaviour half, and it is the half a route alone
  does not settle: adding an install endpoint gives the *first* caller an answer, while
  the invariant is about the second — a reload, a second tab, or a second press of
  安装并启动 during the same download. §1.0 refuses to fake it with a client-side flag and
  shows the truthful `health` instead, which is honest and visibly worse than the product
  wants. The wire half — a state between `not_installed` and `not_started`, and the route
  that enters it — is G-10's contract-lane half and travels separately; a row can be
  blocked on both, and this one is.

The recommendation attached to it is a per-model user-intent record rather than a
boolean: the question a retirement answers is *did a human take a position on this id?*,
and a record shaped that way keeps set replacement as the default for every id nobody
touched. What used to be the list's second item — a model a chain references that a
successful refresh stops advertising — is **no longer handed over**: it was G-7, and the
owner's ruling closed it on `chain_length: 0` and the retained hop's live reason, with no
new field. §0.5 records that ruling and what it cost.

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
  `adopted_by`; the stored chain is a third projection owned by §1.2; **none may stand in
  for another**. D-28 carries the rule, the reason, the restatement that the Source order
  is an add-time placement default and never a runtime filter, and the note that S-1
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

**Failure treatments.** A failure cell either names one of these five
treatments, or names the state it moves to as `→ State`. The set is closed: a
sixth treatment is a change to this section, not a local invention in a frame.

| # | Treatment | What the user sees |
| --- | --- | --- |
| F1 | Retry in place | The surface stays open. The message is replaced in the slot the result would have used, the primary becomes 重试, and every value typed is kept. A refusal that came back persisted nothing. A request that never came back leaves that unknown, and 重试 establishes it before it re-sends. |
| F2 | Keep the last good result | A failed read leaves the last successful result rendered, and the status line carries the cause. The action that failed stays enabled. |
| F3 | Guard refusal | The request came back refused because it would break a configured chain. The shared confirm (`Qp6FI`, §1.6) states the consequence; the same request is re-sent with `force`, or abandoned. |
| F4 | Issue and do not await | A cleanup call for something the user has already left behind is owned in the background while the visible surface moves on. The departing surface does not wait and renders no cleanup error. The cleanup owner may still serialize its own work — for OAuth it settles the cancel attempt first and then re-reads the affected Source projection — because a read made before that call settles cannot account for a write the cleanup itself may materialize (D-15). |
| F5 | No request | The state issues nothing, so it cannot fail. A local draft it holds is discarded by 取消. |

**Frame-group conservation checklist** `[derived]`. Registration is additive: an exported
frame may contribute controls and states, but it may not erase behaviour already owned by
the surface around it. Every new frame column MUST check all eleven rows below. `[x] N/A` is
valid only with the reason in the cell; an empty cell or a missing row is an incomplete
registration, not something a reviewer is expected to infer from prose.

| Check | Required accounting | §1.10 / frame 11 | §1.11 / frame 12 | §1.12 / frame 13 |
| --- | --- | --- | --- | --- |
| C1 — existing capability-gated actions | Preserve every action the containing surface already offers; add the frame delta beside it | [x] Edit / Remove sit beside the existing capability-gated Reauthorize / Replace key producers | [x] The cause-specific card action is additive; the card target and healthy-source overflow producers remain | [x] The existing Add subscription trigger remains the owner; the menu adds only vendor selection |
| C2 — valid local draft | Preserve the exact valid draft across no-op exits and F1/F3; prevent a predictably invalid submit | [x] V1/V2 define the normalized display name and Base URL draft; F1/F3 retain it | [x] The channel-selected acknowledgement copy, typed key, flow intent and any paste value remain with their owning state | [x] N/A — a row activation passes one closed vendor value and holds no editable draft |
| C3 — focus return target | Name the mounted control that receives focus when a transient surface closes | [x] A no-op close returns to the source overflow trigger; a committed exit hands focus to its named receiving surface | [x] A no-op repair exit returns to the invoking card/menu control; a committed exit hands focus to the returned source projection | [x] Escape, outside dismissal and a no-op return from 04 restore Add subscription; a committed 04 exit hands focus to its named receiving surface |
| C4 — in-flight response owner | A busy state with no cancellation route cannot be dismissed; if cancellation is contracted, name the state that owns its late response | [x] Saving source and Removing source disable Cancel, close, Escape and outside dismissal until the request settles | [x] Pre-flow reauth and credential replacement are locked while busy; after a flow is acquired §1.4 Dismissing owns cancellation and any late answer | [x] N/A — the menu sends no request; selection transfers ownership synchronously to §1.4 |
| C5 — existing visual state | Hold the exact rendered origin; a no-op exit must not manufacture another state | [x] Edit/remove dialogs hold the exact §1.6 origin | [x] Card, source-detail and key-entry origins are held exactly | [x] Closed is the same footer/trigger state that existed before Open |
| C6 — committed-report exit | Once a mutation has committed, every dismissal path is the report's Done-equivalent exit and may neither restore a pre-write origin nor discard held response or D-36 commit evidence | [x] Save/remove impact reports retain their envelopes through M1/M2; inferred commits retain their exact Source/absence evidence through the same reads | [x] Repair impact retains R3/R4's distinct envelopes through M3/M4, and Repair unresolved retains R3 through M3; no path restores the invoking card/menu origin | [x] N/A — the menu commits nothing and owns no response report |
| C7 — authoritative field validation | Register every editable field against the authority that normalizes or rejects it; no field may rely on a generic request failure as its validator | [x] V1/V2 register every frame-11 field, including the complete Base URL normalizer | [x] V3/V4 register the replacement key and paste-back value; the shared reauth acknowledgement is the contracted literal `true`, not a free-form draft | [x] N/A — vendor rows emit closed enum values and expose no editable field |
| C8 — acknowledgement consequence coverage | For every capability that requires acknowledgement, register every applicable channel, the exact confirmed request value and one complete consequence sentence that is true for that channel before the irreversible boundary | [x] The existing overflow reauth action transfers its exact Source/channel to §1.11's shared confirmation phase; frame 11 adds no alternate shortcut | [x] Hub and `native_cli` both confirm and send literal `true`, but select separate complete bodies: Hub names only the failure-time cost and safe cancellation; native names the immediate shared-login outage and selected-Source-only recovery | [x] N/A — vendor selection starts create intent and crosses no existing-credential boundary |
| C9 — report-free reconciliation failure | For every committed mutation that skips its report because impact arrays are empty or unavailable after D-36 inference, register the complete-read failure state, held write evidence and read-only Retry | [x] M1/M2 enter Committed projection stale with the updated Source or committed absence plus exact empty/unavailable disposition held; Retry repeats only their complete-surface read | [x] A non-blocked M3/M4 empty envelope or RR-7 inferred commit with an unavailable response tail enters the same state; a blocked R3 result stays in Repair unresolved instead | [x] N/A — the menu commits nothing and invalidates no projection |
| C10 — mutation attempt scope and commit evidence | For every mutation covered by this frame group, name every projection its attempt may invalidate before the response is observed; both a received success envelope and authoritative D-36 commit evidence MUST pass through the owning M row before visible exit, with response-only members marked unavailable rather than invented | [x] R1/R2 own received save/delete envelopes; D-36 inferred save/delete commits enter the same M1/M2 reads with response-only impact arrays unavailable | [x] RR-1–RR-10 name repair attempt scope; R3/R4 classify received Source outcome, while RR-7 inferred repair commit enters M3/M4 with its absent response tail explicitly unavailable | [x] N/A — vendor selection commits nothing; create's later mutation remains owned by §1.4/RR-3 rather than the menu |
| C11 — workflow progress evidence | A broad transport or state class may not stand in for a later workflow milestone; register what each returned stage proves was accepted and the exact next gesture/read it authorizes | [x] Save/delete expose no intermediate accepted state: only an envelope or D-36 subject read proves commit, and both route through C10 | [x] On a paste presentation, `starting` / `awaiting_action` proves the provider still needs the held value, while only `verifying` proves submission acceptance; reauth `flow_not_found` first reconciles its held Source scope | [x] Vendor selection transfers one closed value into §1.4 Default; it is not evidence that OAuth acquisition or login began |

The C5 rule is the former held-state conservation rule in checklist form. In a DP-1
reversible phase, 取消, close, Escape, an outside press, or abandoning an F3 refusal
restores the held origin; none manufactures `Ready`. C5 does not apply merely because a
state is idle: after commit, C6/DP-4 owns every exit and the pre-write origin is no longer
a legal destination. Only a successful mutation or a later authoritative read may select
a different projection.

**Producer-envelope consumption register** `[contract]`. A mutation state is incomplete
unless it cites the row that owns its exact success shape and gives every member one
registered disposition: render it, hold it for a named receiving state, name the other
section that consumes it, or mark it irrelevant with the reason. Empty arrays produce no
report rows, but they are still consumed as the decision to skip that block; an unmentioned
member is never silently discarded. OAuth reauth is deliberately a separate producer from
the guarded Source-mutation family. The frame-11/frame-12 producers use this one register
rather than borrowing a similar-looking tail from another route:

| ID | Producer | Exact authority | Success members | Member-by-member disposition |
| --- | --- | --- | --- | --- |
| R1 | §1.10 source metadata `PATCH` | `model-hub.md` Source-mutation matrix, `mutation.source_metadata` | `source`, `removed_hops`, `interrupted` | `source` is held as the updated Source projection. `removed_hops` and `interrupted` each render their non-empty block in Source save impact reported; an empty member skips only its own block. M1 owns every invalidated projection before handoff. |
| R2 | §1.10 source `DELETE` | `model-hub.md` Source-mutation matrix, `mutation.source_delete` | `removed_hops`, `interrupted` | Each array renders its non-empty block in Source removal impact reported; two empty arrays skip the report. The absent `source` is intentional because the Source was deleted. M2 owns the complete post-delete projection read on either path. |
| R3 | §1.11 reauth acquisition and OAuth terminal | `api.md` OAuth completion, terminal `intent: reauth` | acquisition `flow`; terminal `flow`, `source`, `recovered`, `interrupted_pairs` | RR-1/RR-2 consume the acquisition `flow`; only a non-terminal flow owns presentation/polling, while a terminal flow is status-read first. §1.4 consumes the terminal `flow`. Hold `source` and classify its complete `state` before reading array cardinality: `needs_action` / `error` → Repair unresolved, rendering any non-empty `interrupted_pairs` there; non-blocked + non-empty pairs → Repair impact reported; non-blocked + empty pairs → M3 handoff. `interrupted_pairs` is evidence to render, never the repair verdict. `recovered` remains past-state evidence and selects no copy. M3 owns the affected projections. |
| R4 | §1.11 credential replacement | `model-hub.md` Source-mutation matrix, `mutation.credential_replace` | `source`, `removed_hops`, `interrupted` | Hold `source`. Render non-empty `removed_hops` and `interrupted` blocks in Repair impact reported; each empty member skips only its block. This producer has no `recovered` or `interrupted_pairs` member: those names belong only to R3's OAuth terminal. M4 owns the affected projections. |

**Mutation → projection invalidation register** `[contract]` `[derived]`. A complete
response or an authoritative D-36 subject read can prove the write; neither makes every
read model that write changed disappear. Received and inferred commit evidence therefore
share one rule: before a visible success exit, it MUST pass through the owning M row. A
D-36 inference has no response envelope, so response-only impact members are registered as
unavailable — never as empty and never reconstructed from a later projection read.
The model surface's complete read is `Source[]` + `AgentSupply[]` (including each backend's
stored Source order) + the Route-chain index. A report remains mounted with its response
arrays while that read runs. An inferred commit instead holds the observed Source or exact
absence while the same read runs. A failed read invokes F2 for the stale projection and
never turns either kind of held commit evidence into an unconfirmed write:

| ID | Commit evidence | Invalidated projections | Required reconciliation | Failure disposition |
| --- | --- | --- | --- | --- |
| M1 | R1 source metadata / Base URL, or D-36 reread matching every requested normalized field | `Source[]`; a Base URL inventory change may also change `AgentSupply[]` runnability and Route chains through the guarded cascade | After a non-empty impact report's Done-equivalent exit, before handing off an impact-free received success, **or before closing an inferred commit**, read the complete model surface. Keep R1's exact envelope when received; for inference hold the reread Source and mark `removed_hops` / `interrupted` unavailable | A report stays rendered with `sourceDetail.edit.impact.refreshFail`; a report-free failure enters Committed projection stale with the returned or reread Source plus exact empty arrays or explicit unavailable markers, whichever evidence was actually held |
| M2 | R2 source deletion, or D-36 reread proving the exact Source absent | `Source[]`, every backend's Source order in `AgentSupply[]`, and every Route chain | After the report's Done-equivalent exit, before handing off a received success with two empty arrays, **or before entering Source gone for an inferred commit**, read the complete model surface. Hold the exact Source absence; keep received arrays when available and otherwise mark both response-only arrays unavailable | A report stays rendered with `sourceDetail.remove.impact.refreshFail`; a report-free failure enters Committed projection stale with the exact Source absence plus exact empty arrays or explicit unavailable markers, whichever evidence was actually held |
| M3 | R3 OAuth reauth terminal, or RR-7 reread proving a held blocked Source clear | the returned or reread Source; for `native_cli`, every same-backend native sibling invalidated when login started; all affected Agent-supply/order and Route-chain projections | After a received Repair impact / unresolved report takes its Done-equivalent exit, before handing off a received non-blocked empty-impact terminal, **or before rendering an RR-7 inferred repair**, read the complete model surface. Keep R3's exact tail when received; for RR-7 hold the reread Source and mark `recovered` / `interrupted_pairs` unavailable | A received report stays rendered with `upstream.repair.impact.refreshFail`; a report-free failure enters Committed projection stale with the returned/reread Source plus the exact empty or unavailable tail disposition actually held |
| M4 | R4 credential replacement, or RR-7 reread proving a held blocked Source clear | the returned or reread Source plus every Agent-supply and Route-chain projection the credential attempt may have changed | Use the same complete-surface timing as M3. Retain R4's standard mutation envelope when received; for RR-7 hold the reread Source and mark `removed_hops` / `interrupted` unavailable | A received report stays rendered with `upstream.repair.impact.refreshFail`; a report-free failure enters Committed projection stale with the returned/reread Source plus the exact empty or unavailable standard-envelope disposition actually held |

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
| RR-5 | Status/submit returns `failed` / `cancelled`, E6's `discovery_failed` / `migration_item_conflict`, or E8's reauth `flow_not_found` | none / exact Source projection | create / reauth | Create re-reads the Source list. Reauth re-reads M3's complete model surface after the flow-owned outcome; native includes all same-backend siblings. For E8 this registered read happens **before** failure classification because the forgotten binding may name a deleted Source. E2 has no terminal evidence and keeps the scope with the bounded poll | No successful repair tail exists. E8's exact Source is absent, present, or still unread when the reconciliation read itself fails; a present reread is current projection evidence, not a substitute success envelope | Stop polling and use the intent-specific §1.4 failure row. Perform the registered read: E8 absent or E7 → §1.6 Source gone; E8 present → OAuth failed in front of the reconciled projection; E8 unread → OAuth failed with that read pending, whose Retry repeats **only** RR-5 before it may resend. E2 OAuth-status outages, including `engine_down`, stay in the bounded poll instead |
| RR-6 | A pre-flow reauth or key-replacement request fails or has no answer; the reconciliation read cannot find the held Source | exact Source projection | reauth / replace key | Before RR-6–RR-9 branch, lost `native_cli` reauth acquisition re-reads the complete model surface because acceptance may already have invalidated siblings; an uncertain credential replacement does the same because it may have committed M4 synchronously. Hub reauth retains its Source-scoped D-36 read because acquisition alone has not changed its projection | Absent | Enter §1.6 Source gone. No retry may recreate or select a lookalike Source |
| RR-7 | The same pre-flow failure; the held origin was `needs_action` or `error`, and the reread Source is no longer blocked | blocked Source projection | reauth / replace key | Same producer-attempt scope and mandatory pre-branch read as RR-6 | Present and non-blocked; compared with the held blocked origin | The cleared blocker is sufficient commit evidence, not a visible-exit shortcut. Enter M3/M4 before rendering the reread Source as repaired; if RR-6 already required the complete surface, that read satisfies the M row, while Hub's Source-only read expands to M3 here. Mark every response-only tail member unavailable and invent no impact rows |
| RR-8 | The same pre-flow failure; the held origin was blocked and the reread Source remains blocked | blocked Source projection | reauth / replace key | Same producer-attempt scope and mandatory pre-branch read as RR-6 | Present and still `needs_action` / `error` | Stay in Repair failed with the reread projection behind it; Retry repeats the held producer and preserves acknowledgement/key |
| RR-9 | The same pre-flow failure; the held origin was not blocked, whatever present Source the reread returns | `active`, `standby`, `cooldown` or another non-blocked projection | reauth / replace key | Same producer-attempt scope and mandatory pre-branch read as RR-6 | Present, but no state can prove an elective mutation committed without its success envelope | A healthy snapshot is **not** mutation evidence. Stay in Repair failed and repeat only on Retry. The current wire exposes no mutation-specific read marker, so the success shortcut is deliberately unavailable for elective repair |
| RR-10 | A flow-owning dialog is dismissed, or a bounded retry releases its current flow | none / exact Source projection | create / reauth | Create owns a Source-list read; reauth owns M3's complete model-surface read, including native siblings. The scope survives cancel failure and ownership handoff | Background-only projection result; it never restores a departed dialog or manufactures a terminal repair verdict | Dismissal closes visually under F4; retry may start its fresh acquisition without waiting. In either case the cleanup owner first settles its authorized cancel attempt, then performs the registered read. The read runs even when cancel fails or ownership has moved |

**Dialog phase × exit matrix** `[derived]` `[contract]`. Reversibility, not whether a
request happens to be pending at this instant, decides dismissal semantics. Every dialog
state in frames 11/12 and the shared §1.4 machine names one of these three phases.

| Phase fixture | Registered states | Primary / Done | Cancel | Close / Escape / outside | Evidence and focus disposition |
| --- | --- | --- | --- | --- | --- |
| DP-1 — reversible draft | §1.4 Default and no-flow failures; Edit open, Remove confirmation, Reauth confirmation, Key entry, guarded refusals and pre-success F1 states | Starts/retries the named producer or its mandatory reconciliation | Restore the exact held origin, or close a create dialog that has no Source origin | Same as Cancel where the frame affords that dismissal | No successful response is held. Preserve valid draft through F1/F3; a no-op return restores the invoking control's focus |
| DP-2 — in-flight, no cancellation route | Saving source, Removing source, pre-flow Reauthorizing, Replacing key | Disabled while owned | Disabled | Disabled | The state remains mounted until its response is classified; the response totality matrix owns every success member |
| DP-3 — in-flight OAuth with contracted cancellation | Awaiting sign-in/paste-back/completion, Submitting paste-back and their retryable flow states | Submit/retry follows §1.4 | Close visually into Dismissing | Same as Cancel | RR-10 owns the late cancel and reread sequence; any late submit/poll also re-reads after it settles, and the departed surface never reopens |
| DP-4 — committed evidence | Source save impact reported, Source removal impact reported, Repair impact reported, Repair unresolved and Committed projection stale | A report/result runs its registered Done exit; Committed projection stale retries only its M1/M2/M3/M4 projection read | Not rendered | A report/result uses the same operation as Done; Committed projection stale has no dismissal exit while the required read is stale | Never restore the pre-write origin. Preserve the exact success envelope or D-36 Source/absence evidence until the complete read succeeds; a projection-read failure keeps its impact, unresolved, empty-impact or unavailable-impact disposition and cannot negate the held commit evidence |

**Editable-field authority register** `[contract]` `[derived]`. C2 owns preservation;
C7 owns validity. Save/submit is disabled for an invalid row, so F1 is never used as a
predictable field validator. The register is exhaustive for editable values added or
consumed by frames 11/12 and their shared OAuth form.

| Fixture | Field / owner | Authority consumed | Normalization and valid value | Invalid disposition |
| --- | --- | --- | --- | --- |
| V1 | frame 11 `display_name` | source metadata handler: string, non-empty, at most 64 characters, `contains_credential_material(display_name) == false` | Cap the input at 64 characters and trim. Only a 1–64-character result for which the authority's credential-material detector returns false may be compared and sent | Disable Save when the trimmed value is empty or the credential-material detector matches; keep the draft local and never use F1 as its validator |
| V2 | frame 11 `base_url` | `normalize_model_hub_base_url`, used by the source metadata handler | Empty draft → `null`. Otherwise trim, require a parseable HTTP(S) URL with a hostname, no username/password or fragment, no credential-bearing query key and no credential-shaped material; compare/send the normalizer output, including its lowercase scheme and trailing-path-slash removal | Disable Save; keep the draft local. The field is editable only for an `api_key` / Hub Source with a stored credential |
| V3 | frame 12 replacement `key` | credential-replacement handler: required non-empty string; `force` remains a separate boolean | Trim; a non-empty result enables submit and is held through F1/F3 | Disable submit; never send an empty key |
| V4 | §1.4 paste-back `value` consumed by frame 12 reauth | OAuth submit shape plus the selected `presentation.expects` | Trim; a non-empty code or callback URL enables submit, and the same normalized value is retained through reconciliation | Disable submit; never infer a second format beyond the server-declared enum |

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
is, rather than colliding with itself. Writes that replace rather than append owe nothing
extra: §1.3's 保存顺序 sends one array to one route, and sending it twice lands the same
order. Which side a state falls on is decided by that question, not by the treatment.

`—` in the Copy column means the state introduces no key: it rearranges strings
the frame renders anyway. Keys are written without the `models.hub.` prefix,
as everywhere else in this document.

| Frame | State | Entry condition | Failure / pending | Copy keys | Exit |
| --- | --- | --- | --- | --- | --- |
| §1.0 | Loading | Route entered, first payload outstanding | → Unreachable / Sources unread / Partial | — | What the payload says decides where it lands, not the fact that it arrived. In the order the page reads it: `health` first — `down` → Unreachable (engine down), `not_installed` → Not installed or Unsupported host by the manifest, `not_started` → Not started, `degraded` → Impaired; of those four only `degraded` leaves the dispatch running, because it is the one reading under which the page's own two reads still answer (D-34) — the two are then dispatched underneath an Impaired pill at region grain, one of them failing → Sources unread or Partial in the region it owns; then `sources == []` → Empty (no sources); and a payload that trips none of them → Ready |
| §1.0 | Ready | `health` reads `ok`, both page reads answered, and at least one source `[contract]` | F5 | `shell.running` | Any mutation re-renders in place `[derived]` |
| §1.0 | Empty (no sources) | `sources == []` | F5 | `upstream.empty` | 添加订阅 → 13, then a vendor row → 04; 添加 API Key → 05; first source → Ready |
| §1.0 | Not installed | `health` reads `not_installed` `[contract]` | F5 | `shell.notInstalled`, `install.title` … `install.cancel` | Confirm → Installing |
| §1.0 | Unsupported host | `not_installed`, and the host has no published asset — a reading with **no contracted input**, because nothing the payload carries names the host's platform `[contract]` `[contract-gap]` **G-24** | F5 | `shell.unsupported` | Not from this page — 直连 (§1.8) is the documented escape hatch |
| §1.0 | Installing | An install confirm was accepted `[contract-gap]` G-10 | F1 → Install failed | `install.progress` | Component present → Not started, and the confirm's promise carries it on to Starting |
| §1.0 | Install failed | The install did not complete | F1 lands here | `install.fail.title`, `install.fail.detail`, `install.retry` | 重试 → Installing; dismiss → whichever state the next `health` read reports, which is *Not installed* while the component is unusable — this row asserts nothing about what the failed attempt left on disk, because it saw none of it `[contract-gap]` G-10 |
| §1.0 | Not started | `health` reads `not_started` `[contract]` | F5 | `shell.notStarted` | Run pill → Starting |
| §1.0 | Starting | Start accepted — `POST /api/models/runtime/start` | → Unreachable | `shell.starting` | Live → whichever state the payload that reports it names, the same dispatch Loading makes (D-33): both page reads answered with at least one source → Ready, `sources == []` → Empty (no sources), a page read failing → Sources unread or Partial |
| §1.0 | Impaired | `health` reads `degraded` `[contract]` | F2 at shell grain, over whatever the page already drew — on a first paint that is nothing, and the region whose read failed carries its own F1 beneath this pill (D-34) | `shell.degraded` | The next payload decides, read the way Loading reads one (D-33): `health` back to `ok` with both page reads answered and at least one source → Ready, `sources == []` → Empty (no sources), a page read still failing → Sources unread or Partial, another `health` value → whichever state that value names |
| §1.0 | Unreachable (engine down) | Status request fails, or `health` reads `down` `[contract]` | F2 | `shell.stopped` | Recovery → the same dispatch Loading makes of the payload that reports it (D-33) — at least one source → Ready, `sources == []` → Empty (no sources), a page read still failing → Sources unread or Partial |
| §1.0 | Partial | Sources load, per-backend supply does not | F1 on a first paint, in the region the group rollups would have filled; F2 on any later read, which keeps the rollups already drawn | `gateway.supply.unread`, `gateway.retry` on a first paint; `—` on a later one, which states nothing new because nothing it was showing changed `[derived]` | 重试 → the supply read runs again and what comes back decides, read against the source list this page is already holding (D-33): a reading with at least one source → Ready, or whichever rollup §1.1 names for it; a reading while `sources == []` → Empty (no sources), which a first-paint retry reaches whenever the list that succeeded beside it was the empty one; another failure → back here |
| §1.0 | Sources unread | The mirror: `GET /api/models/sources` fails while `health` and per-backend supply both answer `[derived]` | F1, in the region the list would have filled | `upstream.unread`, `upstream.retry` | The list decides, not the fact that one arrived: 重试 answers with at least one source → Ready, and with `sources == []` → Empty (no sources); a later payload carrying the list is read the same two ways |
| §1.1 | Ready | Sources + per-backend supply both loaded — the two page-level payloads every group-level element is drawn from. The per-model 当前 line is a third read, owned by Chain unresolved below, and this state neither waits on it nor fails with it | F5 | `gateway.group.subtitle.direct`, `gateway.group.subtitle.gateway`, `gateway.group.mode.direct`, `gateway.group.mode.gateway`, `gateway.group.status.ok` | Card → 06; 来源顺序 → 03; model row → 02; 切换到网关 → 10; 切换到直连 → Leaving the gateway; collapse row → Group expanded |
| §1.1 | Empty | `sources == []` | F5 | `upstream.empty` | 添加订阅 → 13, then a vendor row → 04; 添加 API Key → 05 |
| §1.1 | Loading | First paint | → §1.0 Unreachable / §1.0 Sources unread / §1.0 Partial — the same three §1.0 disperses first paint into, because this row is that same first paint seen from the module | — | The payload decides where it lands, not the fact that it arrived — the same reading §1.0 makes one module up: `sources == []` → Empty; anything else → Ready, whose per-source and per-group rows below are drawn from that same payload |
| §1.1 | Per-source `cooldown` | Source reports cooling `[spec §4.5]` | F5 — a rendered report, not a request | `upstream.state.unavailableRetry` while `retry_at` is still ahead, `upstream.state.unavailableDue` once it has passed `[derived]`, `legend.unavailable` | A later payload reports the source in a different state → that state. `retry_at` is when it becomes worth asking again, not evidence that asking worked, so nothing here promotes the source on a clock — and the two keys are that same fact said in copy: the clock running out changes the sentence and not the state |
| §1.1 | Per-source `needs_action` | The source reports `needs_action` `[spec §4.5]` | F5 | `sourceDetail.status.needsAction.oauthExpired`, `sourceDetail.status.needsAction.balanceExhausted`, `sourceDetail.status.needsAction.credentialRevoked`, `sourceDetail.status.needsAction.accountBanned`, `upstream.repair.reauthorize`, `upstream.repair.replaceKey`, `upstream.repair.topUp`, `upstream.repair.contactVendor`, `upstream.repair.contactProvider` | A later payload reports the source in a different state → that state, whatever it says `[contract]`. The payload carries the source's current status and no history, so on a first load that already reads `needs_action` there is no prior state to go back to; and the recovery has a resulting status of its own that the authority writes — a usable refresh clears the blocker and lands `standby` `[contract]` — so remembering one here could only contradict it. Frame 12 registers the card-level repair: OAuth expiry → Reauthorizing; a revoked API key → Key entry; balance exhaustion or account ban on a known subscription vendor → the §1.4 static top-up or support destination; the same two causes on an `api_key` Source → the non-linked service-provider fallback. None borrows another cause's action |
| §1.1 | Per-source `error` | Unclassified failure `[spec §4.5]` | F5 | `sourceDetail.status.error` | The source leaves `error`; the card is one tap to 06 |
| §1.1 | Group waiting | Every member of that backend's chain is cooling and none is retry-ready `[contract]` | F5 — no request of this state's can fail, and no elapsed time resolves it either | `gateway.group.status.waiting` | A later payload reports a runnable member → Ready or Takeover active. Every member's `retry_at` can pass with the group still waiting, so the exit is the next payload's reading and never the elapsed time. F5 says this state issues nothing, not that waiting is the cure |
| §1.1 | Group interrupted — CLI unavailable | `supply_status` reads `interrupted` and the blocker is the native CLI that backend depends on being unreachable **in this process**, at any source health `[contract]` | F2 — the group keeps its last rendering | `gateway.group.status.interrupted` | The CLI becomes reachable → Ready. Waiting does not resolve this one, which is why it is a different word from 暂时全部在冷却 |
| §1.1 | Group interrupted — a source needs action | `supply_status` reads `interrupted`, no member is runnable, and at least one blocker is a source in `needs_action` or `error` `[contract]` | F2 — the group keeps its last rendering | `gateway.group.status.interrupted` | The source leaves `needs_action` / `error` → Ready, or whichever rollup the chain then reads. Frame 12 owns the credential repair controls; 06 keeps 重新拉取 for a source whose stored credential still works `[contract]` |
| §1.1 | Group interrupted — empty chain | `supply_status` reads `interrupted` because the capability chain for the pinned model has no members at all `[contract]` | F2 — the group keeps its last rendering | `gateway.group.status.interrupted` | A source is placed into that model's chain → Ready. Distinct from *Nothing pinned*, where there is no chain to be empty |
| §1.1 | Group interrupted — a hop's source is gone | `supply_status` reads `interrupted`, no member is runnable, and at least one hop names a source that no longer exists `[contract]` | F2 — the group keeps its last rendering | `gateway.group.status.interrupted` | The payload stops reporting the blocker → Ready, or whichever rollup the chain then reads. Adding the source again produces a different source and does not re-satisfy the stored hop, so the exit is a chain edit — which is 02's, and this document specifies nothing about 02 (§0.2, §1.2) |
| §1.1 | Group interrupted — a hop's model is no longer callable | `supply_status` reads `interrupted`, no member is runnable, and at least one hop pins a model its source no longer advertises `[contract]` | F2 — the group keeps its last rendering | `gateway.group.status.interrupted` | 重新拉取 on 06 puts the model back into that source's inventory → Ready, or whichever rollup the chain then reads. The contract keeps the hop visible and non-runnable until an explicit refresh or edit and never re-points it `[contract]`; the edit half is 02's, which this document does not specify (§0.2, §1.2) |
| §1.1 | Backend has no usable source | Every candidate filtered out | F5 | `gateway.supply.none` | Any source becomes eligible; 来源顺序 → 03 |
| §1.1 | Backend has no models | The group resolves to zero model rows `[derived]` | F5 | `gateway.group.emptyModels` | A model becomes available to that backend |
| §1.1 | Nothing pinned | `mode` reads `hub` and `selected_model_id` is `null`, which is exactly when `supply_status` is `null` on the gateway `[contract]` | F5 — a rendered report, not a request | `gateway.group.subtitle.gateway`, `gateway.group.mode.gateway`, `gateway.group.status.noSelection` | A pinned model gives the rollup something to answer with → Ready, or whichever rollup state that reading names |
| §1.1 | Takeover active | The chain read — `GET /api/models/agents/<backend>/chain?model=<id>`, the only read that carries hops `[contract]` — answers with the head not runnable while a later hop is, **and the head's source reads `cooldown`** — the one `Source.state.status` value that clears itself `[contract]`. §4.3 writes the predicate on the chain's **current** hop and the payload carries no such field `[contract-gap]` **G-31**, so the entry is the runnable reading and the exit states what that costs | F5 | `gateway.group.takenOver`, `gateway.row.currentTakeover`, `gateway.group.status.degraded` | A later chain read reports the head `runnable: true` → Ready `[contract]`. That is still a payload reading and not a clock — the server compares `retry_at` to now and this surface reads the boolean, which is why the exit matches the two rows above and no exit here keys on elapsed time. What it is not is the reroute itself: §4.3 recovers by changing execution position **on the next turn**, so until `current` is on the wire (G-31) the violet retires up to one turn before execution actually moves back, and that interval is the whole cost of the gap at this row. This is frame 08 (§1.7) |
| §1.1 | Serving past a blocked head | The serving hop is not the head, and the head is blocked by something waiting does not clear — **stated as the negation of the row above, not as a list of causes.** `runnable = health-permits AND process-available` `[contract]`, so a head is here whenever it is not runnable and its block is not the `cooldown` *Takeover active* requires: the head's source reading `needs_action` or `error`, a source that is `healthy` while the native CLI it needs is unavailable in this process (`reason: native_cli_unavailable` `[contract]`), and a head the chain reports as `source_missing` or `model_unsupported` `[contract]`. Defined by negation because the set of non-self-healing blockers is the contract's to extend, and a row enumerated by cause has to be reopened every time it does | F5 | `gateway.group.status.degraded` | The head becomes runnable again → Ready; the head enters `cooldown` while a later hop still serves → Takeover active. Both are readings of a later payload and neither is a clock (D-16) — including the user-cleared blocks, which are reported by the same read as the rest |
| §1.1 | Chain unresolved | Row grain, not group. That same chain read is outstanding for this row, or came back failed or refused, while the two page payloads are in hand | F2 read at row grain — the group keeps everything those two payloads drew, and this row's three derived columns render `—`. The engine is not implicated and nothing on the head changes | — | The read answers → Ready or Takeover active. What re-issues it is the collapse row (D-35): collapsing and re-expanding the group re-reads every row in it, and it is the drawn control this row's repair uses, there being no per-row 重试 on the frame. The two triggers beside it are the page's own — any mutation that re-renders the group (*Ready* above) and the next load — so a row that failed is never waiting on a request nobody will send |
| §1.1 | Group expanded | Collapse row activated | F5 | `gateway.collapse` | Collapse toggled back → Ready |
| §1.1 | Leaving the gateway | 切换到直连 pressed on a gateway group `[frame]` D-30 — `PATCH /api/models/agents/<backend>/mode` | F1, in place on the group head | `gateway.switchToDirect`, `gateway.fail.switchToDirect`, `gateway.retry` | Success → the group re-renders in its 直连 form; when it was the last gateway backend the page is decided by the sources that are still there, not by the switch — no source left → 09, at least one source retained → **01** with every group in its 直连 form, which is §1.8's own *Retained sources, all direct* branch and not this frame; a failure keeps the group on the gateway and puts the line and 重试 on the group head, which is the slot the re-rendered form would have used |
| §1.3 | Ready | Drawer opened and the eligible sources resolved | F1, in the region the list would have filled → Sources unread | `order.title`, `order.subtitle`, `order.section.ordered`, `order.section.ordered.note`, `order.section.heldOut` | 取消 / 关闭 / Escape → close, discarding uncommitted moves; 保存顺序 → Saving |
| §1.3 | Sources unread | The eligible-source read came back failed while the page behind the drawer is still rendering a healthy runtime `[derived]` | F1, in place | `order.fail.read`, `order.retry` | 重试 → the read runs again, and what it answers with decides: at least one eligible source → Ready, none → Zero eligible sources, another failure → back here; 取消 / 关闭 / Escape → close, having changed nothing |
| §1.3 | Zero eligible sources | No source is eligible for this backend | F5 | `order.empty.noEligible` | 关闭. A source becomes eligible → Ready. 保存顺序 is disabled |
| §1.3 | Empty order, held-out sources remaining | The ordered section is empty and the held-out section is not | F5 | `order.empty.ordered` | 排进来 → Dirty. 保存顺序 stays enabled — an empty order is a real configuration |
| §1.3 | Dirty (uncommitted moves) | 排进来, 移出, a drag, or a keyboard move | F5 — nothing has been sent, so nothing can fail | `order.action.include`, `order.action.exclude` | 保存顺序 → Saving; 取消 → discard, close |
| §1.3 | Saving | 保存顺序 pressed — the whole order in one `PUT /api/models/agents/<backend>/sources` with `{order: string[]}` `[contract]`, which stores and re-echoes it and touches no chain | F1 | `order.save`, `order.fail.save`, `order.retry` | Success → close; 重试 → Saving again, with every move still held |
| §1.4 | Default | A frame 13 vendor row has supplied the vendor | F5 | `addSub.title` … `addSub.hint.chatgpt` | The recommended option is pre-selected; selecting the other replaces it. 去登录 synchronously preallocates PD-1's blank browser context, then sends `POST /api/models/oauth/start`; RR-1/RR-2 classify any accepted flow **before** presentation: non-terminal transfers the context to PD-2, then `presentation.expects: none` → Awaiting sign-in, a paste presentation in E3a `starting` / `awaiting_action` → Awaiting paste-back, and a paste presentation already in E3b `verifying` → Awaiting paste-back completion; each navigates when `presentation.auth_url` is non-null now or on a later flow read. Terminal → immediate status read with no form reopened. Refused because that backend already holds its one `native_cli` Source `[contract]` → Already bound, and any other answer that is not a flow → Start failed; either path closes an unused blank context. Polling, re-render and reconciliation never auto-open a second context `[derived]` |
| §1.4 | Second pass `[derived]` | Re-opened while this backend already holds its one `native_cli` source `[contract]` | F5 | `addSub.opt.added` | The native row is inert whichever account that source holds; the hub row stays choosable and is selected on open, whatever the recommendation says |
| §1.4 | Awaiting sign-in | An acquired flow carries `presentation.expects: none` and no `device_code`; Form B remains G-33. `GET /api/models/oauth/status/<flow_id>` is polled every 2s until the §1.4 evidence-class matrix selects an exit `[contract]` | → OAuth failed / OAuth materialization failed. E2 transport/outage evidence, including `engine_down`, is inconclusive and the next 2s tick retries under the same bound (D-16); E4 or E8 with the held Source present/unread stops at OAuth failed; only E6's two materialization codes stop at OAuth materialization failed | `addSub.signIn`; PD-4 resolves `presentation.instructions_key` and uses the device-code helper on null or lookup failure | PD-2 keeps the authorization link actionable. E5 `success` → `intent: create` closes **into 06 for the source that terminal names** with `added_to` and `adopted_by` in hand; `intent: reauth` → §1.11's R3 repair terminal `[contract]`; E4 → OAuth failed; E8 first runs RR-5's registered read, then absent → Source gone / present or unread → OAuth failed (the unread branch retains read-only Retry); E7 → Source gone; the polling bound passes with no terminal reading → OAuth failed — the bound is `OAuthFlow.expires_at` when the flow carries one `[contract]` and 15 minutes from acquisition when it does not `[derived]`; dismissed any of the three ways → Dismissing |
| §1.4 | Awaiting paste-back | The flow carries `presentation.expects: paste_code` or `paste_callback_url` and E3a reads `starting` / `awaiting_action` `[contract]`; entry is either the acquired form with an empty draft or a submit/reconciliation return with its held value | F5 until submit | `addSub.paste.title.code`, `addSub.paste.title.callbackUrl`, `addSub.paste.subtitle`, `addSub.paste.label.code`, `addSub.paste.label.callbackUrl`, `addSub.paste.placeholder.code`, `addSub.paste.placeholder.callbackUrl`; PD-4 resolves `presentation.instructions_key`, with `addSub.paste.hint.code` / `addSub.paste.hint.callbackUrl` on null or lookup failure; `addSub.paste.submit`, `addSub.cancel` | PD-2 keeps the server-declared authorization link actionable. V4 gates 提交 → Submitting paste-back; only that explicit gesture sends or resends the held value. 取消 / close / outside press → Dismissing (C7/C11/DP-3) |
| §1.4 | Submitting paste-back | 提交 pressed — `POST /api/models/oauth/submit` with the held `{flow_id, value}` `[contract]` | F1 → Paste-back failed for E2; only E6 → OAuth materialization failed | `addSub.paste.submitting` | E5 dispatches by held intent: `create` closes into 06; `reauth` → §1.11's R3 repair terminal. E4 → OAuth failed; E8 first reconciles the held reauth Source scope, then absent → Source gone / present or unread → OAuth failed (read-only Retry when unread). E3a `starting` / `awaiting_action` proves the value was not accepted and returns to Awaiting paste-back with it retained; E3b `verifying` alone → Awaiting paste-back completion. E2 transport/no answer → Paste-back failed; E7 → Source gone |
| §1.4 | Awaiting paste-back completion `[contract]` | Submit or the reconciliation read below answered with `OAuthFlow.state: verifying`, which is E3b's positive evidence that the held paste value was accepted | The same §1.4 evidence-class matrix and expiry/15-minute bound as Awaiting sign-in: E2, including `engine_down`, continues; only E6 stops at materialization failure | `addSub.paste.submitting`, `addSub.cancel` | E5 dispatches by held intent: `create` closes into 06; `reauth` → §1.11's R3 repair terminal. E4 → OAuth failed; E8 first reconciles the held reauth Source scope, then absent → Source gone / present or unread → OAuth failed (read-only Retry when unread); E3b stays here; E3a returns to Awaiting paste-back with the value retained for an explicit submit; E6 → OAuth materialization failed; E7 → Source gone. 取消 / close / outside press → Dismissing |
| §1.4 | Paste-back failed `[derived]` | The submit request returned E2 evidence — transport/no answer or an outage such as `engine_down` — so its flow outcome is unconfirmed | F1, in place; the input, `flow_id` and intent are kept | `addSub.paste.fail`, `addSub.retry`, `addSub.cancel` | 重试 first re-reads `GET /api/models/oauth/status/<flow_id>` (D-36): E5 dispatches by held intent to 06 or §1.11's R3 repair terminal; E4 → OAuth failed; E8 first reconciles the held reauth Source scope, then absent → Source gone / present or unread → OAuth failed (read-only Retry when unread); E6 → OAuth materialization failed; E3a `starting` / `awaiting_action` → Awaiting paste-back with the value retained, where only an explicit 提交 resends it; E3b `verifying` → Awaiting paste-back completion without resubmission; E2 leaves this state unchanged; E7 → Source gone. 取消 → Dismissing |
| §1.4 | Dismissing | 取消, the close icon, or a press outside, while a flow is in flight | F4 — the dialog closes immediately; RR-10 owns cleanup in the background (D-15) | — | The cleanup owner first settles the authorized `POST /api/models/oauth/cancel` attempt and then always re-reads the affected projection: source list for create, M3's complete model surface for reauth. Cancel failure, an ownership handoff, or a terminal flow does not suppress that later read; no late answer restores the dialog (D-16) |
| §1.4 | OAuth failed | The flow reached an unsuccessful terminal — `OAuthFlow.state` reads `failed` or `cancelled` `[contract]` — or E4 reads `flow_expired` / create-intent `flow_not_found`, or E8 reads reauth `flow_not_found` and RR-5's mandatory Source/attempt-scope read finds the Source **or itself fails**, or the polling bound above passed with no terminal reading. The flow failure selects this state only after any intent-owned reconciliation attempt; the intent and an unresolved E8 read are held | F1 for the retry itself; **F4 for the cleanup below**, whose background owner sequences cancel then reread | `addSub.error.oauthFailed`, `addSub.retry` | **Fresh acquisition preserves intent** `[contract]`: `create` sends `POST /api/models/oauth/start`; `reauth` repeats §1.11's held producer with the confirmed `{acknowledge_irreversible: true}` and never calls the create route. RR-1/RR-2 classify its answer, so an already-terminal retry response is status-read and never presented. 重试 after E4 or resolved-present E8 goes straight to that producer; unresolved E8 first repeats **only** RR-5, then absent → Source gone / present → fresh acquisition / still unread → stay here without resending. 重试 after the polling bound first re-reads `GET /api/models/oauth/status/<flow_id>` once (D-32): E5 dispatches by intent to 06 or §1.11's R3 repair terminal; E6 → OAuth materialization failed; E8 runs RR-5 before choosing Source gone / this state; E4 → fresh acquisition; paste E3a → Awaiting paste-back with the value retained; E2, E3b or non-paste E3a → launch fresh acquisition and F4 cleanup together, while that cleanup settles `POST /api/models/oauth/cancel` before its RR-10 reread; E7 → Source gone. 取消 → Dismissing, which releases whichever flow is current |
| §1.4 | OAuth materialization failed `[derived]` `[contract]` | Status or submit returned E6's `discovery_failed` or `migration_item_conflict` after this dialog acquired the flow. Authorization entered materialization and the affected source projection may already have changed; no other named code enters this state | F1 in place; polling stops immediately | `addSub.error.finalize` for `intent: create`, `addSub.error.finalizeReauth` for `intent: reauth`, `addSub.retry` | Refresh the affected projection immediately: `create` re-reads the source list; `reauth` re-reads M3's complete model surface and applies §1.6 Source gone precedence to its selected Source. The failure surface remains in front of that refreshed projection. 重试 performs fresh acquisition with the held intent and channel body, exactly as OAuth failed does; 取消 → Dismissing |
| §1.4 | Engine unavailable | The gateway is not running and gateway-upstream was chosen | F1 | `addSub.error.engineDown`, `addSub.retry` | 重试 re-sends, and that press **is** the recovery observation — nothing here watches for one — so its answer decides: whichever of *Awaiting sign-in* / *Already bound* / *Start failed* the start call then names, or still down → back here; 取消 → dismiss, nothing bound `[derived]` |
| §1.4 | Already bound | 去登录 was refused by the start call because that backend already holds its one `native_cli` Source — 「the API rejects duplicate creation with the existing Source id」 `[spec §4.1]`. **This is the race the dialog cannot see**: it disables the native row from the sources it read on open, and the singleton can appear after that | F1, in place — nothing was sent to the provider, so there is no flow to cancel | `addSub.error.alreadyBound`, `addSub.retry` | 重试 → Second pass: the dialog re-reads the sources, the native row is now the inert one, and the hub row is what 去登录 sends; 取消 → dismiss, nothing bound `[derived]` |
| §1.4 | Start failed | `POST /api/models/oauth/start` did not put a flow in this dialog's hands, for any reason that is not the singleton refusal above — a refusal it has no second reading for, or no answer at all | F1, in place — there is no `flow_id` here, so there is nothing this dialog can cancel and nothing it can re-read. That is a statement about what the client holds and **not** about what the server did: a refusal means no flow, but a lost response leaves the outcome unknown (F1), and a start that was accepted before its answer died is a flow that may still be running with no way to reach it — `api.md` contracts no flow-list read, and the three routes that could touch it all take the `flow_id` that never arrived `[contract-gap]` **G-30** (D-36). This is the one failure here that *OAuth failed* cannot absorb: that state is defined on a flow, and this dialog has none | `addSub.error.startFailed`, `addSub.retry` | 重试 → the start call goes out again and its answer is read the same three ways *Default* reads one: accepted → Awaiting sign-in, the singleton refusal → Already bound, still no flow → back here. Where the lost answer had been an acceptance, that press is a second flow started beside a first this dialog cannot see, which is what G-30 costs until something can reach it; 取消 → dismiss, nothing bound *by this dialog* `[derived]` |
| §1.5 | ① Default | Dialog opened | F5 | `addKey.title` … `addKey.submit` | 添加 → ②; 拉取型号 → ②′; 取消 → dismiss |
| §1.5 | ①′ Pull result, **Pull origin** `[derived]` | ②′ came back with an inventory — including after a hint in ④′, and after 重试 re-ran the fetch from ⑤′ | F5 — the request already succeeded | `addKey.pull.result`, `addKey.pull.empty`, and ①'s own keys, which all still render | Editing Base URL or API Key → ①, the report dropped; 拉取型号 again → ②′; 添加 → ②, which runs its own observation (G-18) and reuses nothing from here; 取消 → dismiss |
| §1.5 | ② Adding | 添加 pressed — the non-persisting observation runs first `[contract-gap]` G-18, and `POST /api/models/sources` goes out only on the outcome that has consent | → ③ / ④ / ⑤ | `addKey.adding`, `addKey.adding.detail` | The observation comes back clean → persist `[contract-gap]` G-27 → the dialog closes into 06; that persist failing, or never answering → ⑦ |
| §1.5 | ②′ Pulling, **Pull origin** `[derived]` | 拉取型号 pressed, or 重试 pressed from ③′ / ④′ / ⑤′ | → ③′ / ④′ / ⑤′ | `addKey.adding`, `addKey.adding.detail` | Success → ①′, persisting nothing |
| §1.5 | ③ Failure, **Add origin** | A probe run *as part of* 添加 came back unsuccessful — classified, or the separately contracted **adapter error**, which AC-26 lists beside authentication failure and timeout as one of the five outcomes an API-key test settles on `[contract]` and which no classification of this dialog's covers | F1 | `addKey.fail.subtitle`, `addKey.fail.auth`, `addKey.fail.auth.detail`, `addKey.fail.address`, `addKey.fail.network`, `addKey.fail.unclassified`, `addKey.retry` | 重试 → ②, whichever of the four lines was rendered — the retry re-runs the observation and does not depend on what the last one concluded |
| §1.5 | ③′ Failure, **Pull origin** `[derived]` | A probe run by 拉取型号 classified the failure | F1 | as ③ | 重试 → **another 拉取型号, not ②** |
| §1.5 | ④ Interface undetermined, **Add origin** | Reachable **and** authenticated, and the response shape matches no known interface | F1 | `addKey.undetermined.title`, `addKey.undetermined.detail`, `addKey.undetermined.label`, `addKey.undetermined.hint`, `addKey.protocol.anthropicMessages`, `addKey.protocol.openaiResponses`, `addKey.protocol.openaiChatCompletions`, `addKey.retry` | Pick a hint + 重试 → probe again in the hinted order → identified: persist `[contract-gap]` G-27 and close, or → ⑦ if that persist does not land; still undetermined: back to ④ with the attempt as evidence |
| §1.5 | ④′ Interface undetermined, **Pull origin** `[derived]` | The same outcome, from 拉取型号 | F1 | as ④ | Pick a hint + 重试, still as a pull → identified: → ①′, persisting nothing; still undetermined: back to ④′ |
| §1.5 | ⑤ Identified, inventory unavailable, **Add origin** `[frame]` `d6bFlX` | The probe proved the protocol with a real response, **and** the model fetch came back unusable | F1 | `addKey.inventory.title`, `addKey.inventory.detail`, `addKey.inventory.reason.rateLimited`, `addKey.inventory.reason.transport`, `addKey.inventory.reason.unknown`, `addKey.retry`, `addKey.addAnyway` | 重试 → re-run **the fetch only**; 仍要添加 → persist with the proved protocol and an empty inventory `[contract-gap]` G-27, close into 06, or → ⑦ if that persist does not land |
| §1.5 | ⑤′ Identified, inventory unavailable, **Pull origin** `[derived]` | The same outcome, from 拉取型号 | F1 | as ⑤ | 重试 → re-run the fetch as a pull |
| §1.5 | ⑥ Engine unavailable, **Add origin** `[derived]` | The gateway is not running when 添加 is pressed | F1 | `addKey.fail.engineDown`, `addKey.retry` | F1 in full: the form keeps every value it holds and the primary becomes 重试. Pressing it **is** the recovery observation — nothing here watches for one — and re-attempts 添加 → ②, whose own outcomes then apply; the engine still down → back here; 取消 → dismiss |
| §1.5 | ⑥′ Engine unavailable, **Pull origin** `[derived]` | The gateway is not running when 拉取型号 is pressed | F1 | as ⑥ | The same, one control over: 重试 re-attempts 拉取型号 → ②′. Nothing was going to be persisted either way, so the two rows differ only in which request the press re-sends |
| §1.5 | ⑦ Save unconfirmed, **Add origin** `[derived]` | The observation 添加 owed has already come back with consent — clean in ②, identified by a hint in ④, or waived in ⑤ — and `POST /api/models/sources` then failed or never answered. Separate from ③ / ④ / ⑤, which are readings of the *upstream*: this one is the persist step. It is a state this file can write while G-27 leaves the request body unspecified, because what failed is the request and not its shape | F1 | `addKey.fail.save`, `addKey.retry` | 重试 re-reads `GET /api/models/sources` before it re-sends, which is F1's own clause for a create that may have committed unseen — **and that read cannot name its own subject.** The `id` was server-assigned into the response that died, the key never comes back, and neither Base URL nor display name is contracted unique, so the list answers 「what is here」 and not 「whether this create is here」 `[contract-gap]` **G-29** (D-36). This row does not close that distance by resemblance — choosing the nearest-looking Source is D-28's error one surface over — so the reading is evidence for the person reading it and never a branch: 重试 → ②, which re-runs its own observation (G-18) and persists again; failing again → back here; 取消 → dismiss. Where the lost response had committed, that second persist is a second Source, which is what G-29 costs. There is no ⑦′, because Pull origin persists nothing and so has no step that can fail this way |
| §1.6 | Ready | Source detail loaded and the table holds at least one model — discovered, added by hand, or both. When `last_discovered_at` is null the inventory has no age, `{{time}}` is absent by §0.9's rule, and the status line drops that segment `[contract]` | F5 | `sourceDetail.status.inUse`, `sourceDetail.status.listUpdated`, `sourceDetail.summary` | `iGcAi` → 01; a rendered 重新拉取 → Refetching; 添加模型 → Manual draft; a tier area → Tiers editing; a row's overflow → Removing a manual entry. The §1.6 action-capability row decides whether 重新拉取 exists |
| §1.6 | Empty (no models) | The table is empty **and** a discovery has completed — `last_discovered_at` is non-null `[contract]` | F5 | `sourceDetail.empty` | Manual add, or a successful refetch |
| §1.6 | Never fetched | The table is empty **and** no successful discovery has ever completed — `last_discovered_at` is null `[contract]` | F5 | `sourceDetail.emptyNeverFetched` | A successful 重新拉取, or a manual add that commits → Ready |
| §1.6 | Refetching | 重新拉取 pressed — `POST /api/models/sources/<source_id>/refresh`, guarded | F3 when the response is a guard refusal, F2 otherwise → Refetch failed | `sourceDetail.action.refetch` | Any answer this page can read → Refetch result, **whatever the row count comes back as, none included**. An emptying refetch is not a different kind of answer, and it is the one where the diff is worth most: every id the source used to advertise has just left the discovered slice, so it is where `sourceDetail.refetch.removed` names the largest set it will ever name. Leaving straight for Empty (no models) — as this row read until this round — dropped that report at that exact moment, and replaced it with a sentence that says a fetch returned nothing and not what stopped being there. The empty table is still an empty table: *Refetch result* renders `sourceDetail.empty` under the removal line, and of §1.6's two empty readings only that one is reachable from here, since this refetch completing is what makes `last_discovered_at` non-null. It is where *Refetch result* lands rather than what this state enters; a refusal → Refetch refused |
| §1.6 | Refetch result `[derived]` | A refresh came back usable — the response carries the complete updated source `[contract]`, and this page still holds the list it was rendering, so what changed is a comparison of two payloads it has | F5 — the request already succeeded | `sourceDetail.refetch.added`, `sourceDetail.refetch.removed`, `sourceDetail.refetch.unchangedOnly`, and the keys for whatever the table now is: Ready's own while any row is left, `sourceDetail.empty` when the answer emptied it. The diff line and the table's own copy are two statements about one payload, and the second never cancels the first | Any next action, or the next load → Ready, or → Empty (no models) when the answered list was empty: the diff is a report about one fetch and nothing re-derives it, so what remains afterwards is the table's own reading. 重新拉取 again → Refetching |
| §1.6 | Refetch refused | The refresh came back refused, naming the hops a shorter inventory would remove | F3 — `Qp6FI`, the same confirm this page already starts for a removal | `guard.title.refetch`, `guard.subtitle.refetch`, `guard.confirm.refetch`, `guard.label`, `guard.count`, `guard.hop.position`, `guard.hint.safe`, `guard.hint.interrupt`, `guard.gap.label`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `guard.cancel` | 仍要拉取 re-sends the same `POST` with `force` → Refetching; 取消 restores the exact held §1.6 origin by C5 |
| §1.6 | Refetch failed | The refresh came back a classified failure, **or never answered at all** — F2's own 「otherwise」 lands both here. For the classified one `api.md` has the server update the **source-global state**, preserve the last successful model list and timestamp, and return the normal safe error `[contract]`, so the source changed state and the answer does not carry which. For a lost answer even that is not known: F1's rule leaves the outcome unknown, and a refresh that did commit has already replaced the discovered inventory — on the `force` path, after removing the hops the guard named | F2 lands here. The **previous list is kept only where the contract keeps it**, which is the classified branch and is the contract's guarantee rather than this page's choice; a lost answer guarantees nothing, which is why the re-read below is a reconciliation and not a status refresh | `sourceDetail.fail.refetch`; the status word is not this row's and comes from whichever state the re-read below lands on | The failure line goes on the bar, and the page re-reads `GET /api/models/sources` — the only read that answers with this source, there being no single-source route `[contract]`. **That read settles the mutation as well as the status**, because the page has held `source_id` since it sent the refresh and the list answers with the complete `Source`, models included `[contract]` (D-36): what it renders afterwards is that source as read, so a refresh that committed unseen is visible rather than papered over by the list this page was holding. The state it finds dispatches through this section's own status mapping: `cooldown` → Cooling, `needs_action` → Needs action, `error` → Unclassified error, `standby` or an `active` nothing adopts → Not supplying, an adopted `active` → Ready. The source absent from that list → Source gone. The re-read itself failing → the bar keeps the status word and the page keeps the list it was already rendering, a failed read being no reading at all (D-16), and the line above stays true of both. 重新拉取 stays enabled throughout → Refetching |
| §1.6 | Tiers editing | A row's tier area was activated | F5 — nothing is sent until a tier is committed | `sourceDetail.tiers.add`, `sourceDetail.tiers.inputHint`, `sourceDetail.tiers.empty`, `sourceDetail.tiers.addFirst` | Enter, or a chip's × → Tier commit; Enter on an empty input, or on a value this row already carries, commits nothing and stays here — `minLength: 1` and `uniqueItems` `[contract]`; blur / Escape → Ready, discarding whatever is still uncommitted in the input |
| §1.6 | Tier commit | A tier was added by Enter **or** removed by a chip's × — either way `PATCH /api/models/sources/<source_id>/models/<model_id>` carries the complete `reasoning_efforts` list `[contract]` | F1, on the row: the row keeps the pre-request list, states the failure and offers 重试 — an add leaves its text in the input, a removal puts its chip back | `sourceDetail.fail.tier`, `sourceDetail.retry` | Success → the answered list is what the row renders, still in Tiers editing |
| §1.6 | Manual draft | 添加模型 pressed | F5 — a local draft, sent by nothing | `sourceDetail.entry.manual`, `sourceDetail.addRow.hint` | 添加 is enabled only once the id field holds a value this source's table does not already list `[derived]`: blank, whitespace-only, or an already-listed id leaves it **disabled**, the same answer 「Tiers editing」 gives an empty or duplicate tier, and `sourceDetail.addRow.hint` is what the row states meanwhile. Enabled 添加 → Manual commit; 取消 discards the row and nothing is persisted |
| §1.6 | Manual commit | 添加 pressed on an enabled 添加 — `POST /api/models/sources/<source_id>/models` | F1, on the draft row: **the row and everything typed in it are kept**, and the primary becomes 重试 | `sourceDetail.fail.addModel`, `sourceDetail.retry` | Success → the row becomes an ordinary 手动添加 row → Ready |
| §1.6 | Removing a manual entry | The row menu's 移除 — `DELETE /api/models/sources/<source_id>/models/<model_id>`, guarded | F3 when the response is a guard refusal, F1 otherwise | `sourceDetail.fail.removeModel`, `sourceDetail.retry`, `sourceDetail.row.remove` | Success → the row is gone → Ready while any row is left; removing the last one → Empty (no models) or Never fetched by `last_discovered_at`, the same two readings §1.6 gives every other empty table |
| §1.6 | Guard refused | The `DELETE` came back refused, naming the hops it would remove | F3 — `Qp6FI`, this product's one guarded-change confirm | `guard.title.removeModel`, `guard.subtitle.removeModel`, `guard.confirm.removeModel`, `guard.label`, `guard.count`, `guard.hop.position`, `guard.hint.safe`, `guard.hint.interrupt`, `guard.gap.label`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `guard.cancel` | 仍要移除 re-sends the same `DELETE` with `force` → Removing a manual entry; 取消 restores the exact held §1.6 origin by C5 |
| §1.6 | Not supplying | The source is `standby`, or `active` with nothing adopting it `[contract]` | F5 | `upstream.state.standby` | The source starts supplying again — only the bar changes |
| §1.6 | Cooling | The source is `cooldown` `[contract]` | F5 | `upstream.state.unavailableRetry` while `retry_at` is still ahead, `upstream.state.unavailableDue` once it has passed `[derived]` | A later payload reports the source in another state → that state; `retry_at` passing is not that payload — it changes which of the two keys the bar renders and nothing else. Separate from *Not supplying* because the two answer different questions — standby says nothing is drawing from this source, cooling says nothing *can* yet and names when that changes. This section's own status mapping gives `cooldown` the gold `upstream.state.unavailableRetry`; a row that folded it into the muted standby word dropped the one fact the mapping exists to carry |
| §1.6 | Needs action | The source is `needs_action` `[spec §4.5]` | F5 | `sourceDetail.status.needsAction.oauthExpired`, `sourceDetail.status.needsAction.balanceExhausted`, `sourceDetail.status.needsAction.credentialRevoked`, `sourceDetail.status.needsAction.accountBanned` | The reported cause clears. The bar states which cause `state.detail_key` carries and the table stays live; frame 12 owns the card-level repair controls, while this page keeps 重新拉取 enabled only when the §1.6 action-capability row renders it |
| §1.6 | Unclassified error | The source is `error` `[spec §4.5]` | F5 | `sourceDetail.status.error` | The source leaves `error`. The bar reads 异常 and claims no cause; the table and every action the capability row renders stay live |
| §1.6 | Source gone | The selected source is not in a `GET /api/models/sources` read this page makes, or any source-bound mutation registered in §1.6, §1.10 or §1.11 answers `source_not_found` `[contract]` — removed by another tab, another API client, or a guarded cascade while the surface was open. This entry has precedence over every caller's F1/F3 branch; those treatments apply only after the absence reading is excluded. Distinct from every empty reading above, which is about this source's *models*; here the subject of the page is what is gone, and the list may still hold others, so §1.0's Empty does not answer either | F5 — this state issues nothing. It is what the page renders once a read or a mutation has already told it | `sourceDetail.gone` | The detail or card overlay is dropped rather than left standing over a source that is not there (D-16), and the frame's own back control `iGcAi` → 01, where the list is the surface of truth — the same answer §1.4's *Dismissing* gives for the same reason (D-16). Re-entry is not a state of this frame: there is no source left to open |
| §1.7 | Nominal | **No route is taken over or exhausted** — every configured chain is serving its own first stored hop. AC-30 makes takeover 「a projection of visible configuration plus live runnability」 rather than a stored sibling state `[contract]`, so the subject of this frame is chains and the predicate is read per chain: a Source no chain draws from, and a non-head hop that is unavailable while the head still serves, are both outside it. Reading it globally — 「no source is unavailable」 — activated 08 for an unhealthy Source nothing was using, and left the frame with no valid state whenever one existed | F5 | — | A head enters `cooldown` while a later candidate serves → Takeover; a head stops being runnable for something waiting does not clear → §1.1 *Serving past a blocked head*, which is not this frame |
| §1.7 | Takeover | The head is unavailable **for a recoverable quota/cooldown reason** and a next candidate is serving `[contract]` — §4.3 derives takeover from exactly that pair, and the paragraph below says what the other blocker kinds render instead | F5 | `takeover.pill`, `takeover.chip` | Recovery → Nominal, on the next turn `[spec §4.3]` |
| §1.7 | Exhausted | The head is unavailable and **no** candidate remains | F5 | `gateway.supply.none` | Any candidate recovers → Takeover or Nominal |
| §1.7 | Multiple takeovers | More than one backend was rerouted | F5 | `takeover.pill` | Each backend recovers independently |
| §1.7 | Loading / Empty / Unreachable | As §1.0 | As §1.0 | — | As §1.0 |
| §1.8 | Ready (first run) | Every backend is in 直连, no source exists, and at least one backend row renders | F5 | `direct.card.current`, `direct.card.current.sub`, `direct.pill.direct`, `direct.backend.claude.detail`, `direct.backend.codex.detail`, `direct.backend.opencode.detail`, `direct.benefits.title`, `direct.benefits.1` … `direct.note.perBackend`, `shell.allDirect` | 切换到网关 on a row → 10's confirm for that backend; a source added or a backend switched → 01 |
| §1.8 | Loading | First paint | → §1.0 Unreachable / §1.0 Sources unread / §1.0 Partial — the same three §1.0 disperses first paint into, because this is that same first paint seen from the direct home: the runtime status failing or reading `down` → Unreachable, `GET /api/models/sources` failing → Sources unread, per-backend supply failing → Partial. Neither page read may be dropped into Unreachable, whose entry is a runtime-status reading: this frame needs the source list to tell *Ready (first run)* from *Retained sources, all direct*, and needs the supply read to draw a backend row at all, so a read that failed is what has to be said `[derived]` D-34 | — | Payload arrives → No backend found when the rows resolve to zero, Retained sources, all direct when a source already exists and no backend is on the gateway — which is 01, not this page — and Ready (first run) otherwise. The three are exactly this frame's own branch table below |
| §1.8 | No backend found | The rows resolve to zero `[derived]` `[contract-gap]` G-11 | F5 | `direct.empty.title`, `direct.empty.body`, `direct.empty.install` | **Install a backend CLI and reload.** Neither card renders and the pill is absent; the page leaves this state only when a backend appears in the payload. This is the one state on the page with no in-product action, and the copy says so rather than leaving the user to guess |
| §1.8 | Retained sources, all direct | Every backend is in 直连 and at least one source exists — reachable through `adopt.undo.3` | F5 | — | The page is 01 with every gateway group in its 直连 form, not this frame |
| §1.9 | Default | 切换到网关 pressed on a backend row | F5 | `adopt.title` … `adopt.undo.3`, `adopt.confirm`, `adopt.cancel` | 取消 → dismiss unchanged; 切换到网关 → Committing |
| §1.9 | Committing | The confirm's primary was pressed — `PATCH /api/models/agents/<backend>/mode` | F1 → Failed | — | Success → the dialog closes and the page becomes 01 with this backend in 网关 mode |
| §1.9 | Failed | A step this confirm promised did not go through | F1 lands here | `adopt.fail.title`, `adopt.fail.detail`, `adopt.fail.reason.transport`, `adopt.fail.reason.refused`, `adopt.fail.reason.notReady`, `adopt.fail.reason.unknown`, `install.fail.detail` | The dialog stays open, states the failure, keeps 取消 enabled and the primary retryable. **The title holds for every step; the detail is selected by which step failed.** A step that was a request renders `adopt.fail.detail` — `{{request}}` is the request that failed. The install step is no request `[contract-gap]` G-10, so it renders `install.fail.detail`, the sentence 01 already shows for the same operation (D-26). **`adopt.fail.detail`'s `{{reason}}` is one of four words and never an upstream string**: `fail.reason.transport` when no answer came back, `fail.reason.refused` when one did and it was a refusal, `fail.reason.notReady` when the start step answered and the runtime still reads `not_started` or `down`, and `fail.reason.unknown` for everything else — the residue that keeps the set total, because §0.9 has the slot always present. **重试 re-reads before it resumes, and the reading decides where it re-enters** `[derived]`: every step this confirm promises has a read that proves it, and the subject of all of them is the backend the dialog was opened on, which the client has held since the first press (D-36). `GET /api/models/runtime/status` proves the first two — `health` no longer `not_installed` means the component is there, `health` reading `ok` **or `degraded`** means it started — both are a runtime answering about itself, and only the second half of that pair is a quality judgement — and `GET /api/models/agents` proves the third, `mode` reading `hub` `[contract]`. So the press is not a replay: the runtime read answers first and the sequence resumes at the first step it cannot already see done — still `not_installed` → install, `not_started` or `down` → start, `ok` or `degraded` → the mode `PATCH`. **`degraded` is on the switching side of that split, and §1.0 is what puts it there**: it maps `degraded` to Impaired with an afforded action of `none`, so it is the one health this dialog can read that offers no control at all — re-sending the start route for it would press the thing the product declines to offer, once per retry, against a component that is already answering. Reading it as 「present but not `ok`」 did exactly that. Nothing here waits for `ok` either, because no contract conditions the mode `PATCH` on health: what the sequence owes is that the component exists and is running, and an impaired runtime meets both. A backend that already reads `hub` is a `PATCH` that committed unseen, so nothing is re-sent and the dialog closes into the success it turns out to have been, the same shape §1.6's *Refetch failed* has and §1.5's ⑦ cannot get |
| §1.9 | Dependency missing `[derived]` D-26 `[contract-gap]` G-10 | Runtime `health` is `not_installed` (§1.0) | F1 → Failed | `adopt.effects.install`, `adopt.confirm.install` | The confirm gains one line naming the component and roughly how long it takes, and the primary becomes 安装并切换 — one press, three steps, reported as one outcome. **Only the last two steps have routes** (`POST /api/models/runtime/start`, then the mode `PATCH`), so this row inherits G-10 the way 01's pill does: the install's progress is client-side only, and a reload while it runs reads back `not_installed` with this backend still in 直连, because the mode `PATCH` is the third step and has not been sent. A step that fails lands in this dialog's own Failed row above, which is why the promise is safe to make as one outcome — the dialog reports no switch it did not make. **Which step failed decides the detail there**: the two route steps have a `METHOD path` to name and render `adopt.fail.detail`; the install step has none — that is what G-10 *is* — so it renders `install.fail.detail`, and no string is asked to interpolate evidence this dialog cannot produce. 取消 is unchanged |
| §1.10 | Edit open | 编辑来源 chosen from 06's source overflow; that menu's capability-gated credential actions remain beside Edit / Remove (C1) | F5 | `sourceDetail.edit.title`, `sourceDetail.edit.name`, `sourceDetail.edit.baseUrl`, `sourceDetail.edit.hint`, `sourceDetail.edit.cancel`, `sourceDetail.edit.save` | V1/V2 are the exhaustive field gates. 保存 is enabled only when every changed normalized value is valid and at least one differs from the held Source; 保存 → Saving source. 取消 / close / outside press → dismiss unchanged and return focus to the overflow trigger (C2/C3/C7) |
| §1.10 | Saving source | 保存 pressed — `PATCH /api/models/sources/<id>` with the normalized changed `display_name` and/or `base_url` `[contract]` | F3 when a Base URL change is refused; F1 otherwise → Source save failed. The request owns the dialog: Cancel, close, Escape and outside dismissal are disabled until it settles (C4) | `sourceDetail.edit.saving` | R1 owns the complete success envelope: non-empty impact → Source save impact reported; empty impact → hold the returned `source` while M1 reads the complete model surface, then close. A guard refusal → Source save refused; `source_not_found` → §1.6 Source gone |
| §1.10 | Source save refused | The guarded `PATCH` names the hops or supply gaps the Base URL change would remove `[contract]` | F3 — shared `Qp6FI` | `guard.title.editSource`, `guard.subtitle.editSource`, `guard.confirm.editSource`, `guard.label`, `guard.count`, `guard.hop.position`, `guard.hint.safe`, `guard.hint.interrupt`, `guard.gap.label`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `guard.cancel` | 仍要保存 re-sends the same `PATCH` with `force: true` → Saving source; 取消 → Edit open with both values kept |
| §1.10 | Source save failed `[derived]` | The `PATCH` failed or never answered | F1, in the edit dialog | `sourceDetail.edit.fail`, `sourceDetail.retry` | 重试 first re-reads `GET /api/models/sources` by the held source id (D-36): the requested normalized fields already present are authoritative commit evidence → M1 with the reread Source held and response-only `removed_hops` / `interrupted` explicitly unavailable, then close only after its complete-surface read; source absent → §1.6 Source gone; otherwise re-send → Saving source |
| §1.10 | Source save impact reported `[derived]` `[contract]` | R1 holds a successful metadata response with non-empty `removed_hops` and/or `interrupted` | F2 for M1's complete-surface read; the write already succeeded and the authoritative response is held. DP-4 owns every exit | `sourceDetail.edit.impact.title`, `sourceDetail.edit.impact.detail`, `sourceDetail.edit.impact.done`, `sourceDetail.edit.impact.refreshFail`, `sourceDetail.impact.removedHops`, `sourceDetail.impact.interruptedModels`, `guard.hop.position`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `sourceDetail.retry` | Render each non-empty array once: the existing hop block for `removed_hops`, the §1.11 SupplyGap block for `interrupted`. 完成, close, Escape or outside press all run M1's complete model-surface read; success returns with the current surface, failure keeps this report and changes the exit to 重试. No path restores the pre-write origin or lets a reread replace response evidence |
| §1.10 | Remove confirmation `[derived]` | 移除来源 chosen from 06's source overflow; the exact §1.6 source-detail state behind it is held | F5 — no request has been sent | `guard.title.removeSource`, `guard.hint.removeSource`, `guard.confirm.removeSource`, `guard.cancel` | 移除来源 → Removing source with the initial non-forced delete; 取消 / close / outside press restores the held §1.6 origin by C5 |
| §1.10 | Removing source | The destructive primary was activated in Remove confirmation or Source remove refused — `DELETE /api/models/sources/<id>` is non-forced on the first path and carries `?force=true` only on the second `[contract]` | F3 on refusal; F1 otherwise → Source remove failed. The request owns the dialog and all dismissal paths are disabled until it settles (C4) | `sourceDetail.remove.checking` | R2 owns the complete success envelope: either array non-empty → Source removal impact reported; both empty → remove the exact Source locally, run M2's complete model-surface read, then §1.6 Source gone. An initial guard refusal → Source remove refused; `source_not_found` → §1.6 Source gone |
| §1.10 | Source remove refused | The non-forced delete returned the guarded envelope; frame 11 renders its source-removal variant while retaining the held §1.6 origin | F3 — shared refusal semantics and the frame 11 dialog | `guard.title.removeSource`, `guard.confirm.removeSource`, `guard.label.removeSource`, `guard.count`, `guard.hop.position.removeSource`, `guard.hint.removeSource`, `guard.gap.label`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `guard.cancel` | 移除来源 re-sends the same `DELETE` with `force` → Removing source; 取消 / close / outside press restores the held origin by C5 |
| §1.10 | Source remove failed `[derived]` | Either delete request failed or never answered | F1, in place | `sourceDetail.remove.fail`, `sourceDetail.retry` | 重试 re-reads `GET /api/models/sources` by the held id (D-36): absence is authoritative delete-commit evidence → M2 with both response-only impact arrays explicitly unavailable, then §1.6 Source gone only after its complete-surface read; present → re-send the same stage, non-forced before refusal and forced after one |
| §1.10 | Source removal impact reported `[derived]` `[contract]` | R2 holds a successful delete response with non-empty `removed_hops` and/or `interrupted`; the source is already gone | F2 for M2's complete-surface read after a DP-4 exit; the read cannot negate the held delete response | `sourceDetail.remove.impact.title`, `sourceDetail.remove.impact.detail`, `sourceDetail.remove.impact.done`, `sourceDetail.remove.impact.refreshFail`, `sourceDetail.impact.removedHops`, `sourceDetail.impact.interruptedModels`, `guard.hop.position.removeSource`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `sourceDetail.retry` | Render the authoritative success arrays before any reread, even when they differ from the earlier refusal preview. 完成, close, Escape or outside press are the same exit: M2 re-reads Sources, Agents/source orders and Route chains together; success drops the detail into 01 with the current surface, failure keeps the report, states only that the surface refresh failed, and changes the exit to 重试. It never says the committed deletion is unconfirmed |
| §1.11 | Needs-action card | Frame 12's card delta is rendered for a source whose status is `needs_action` | F5 | the selected `sourceDetail.status.needsAction.*` key plus `upstream.state.supplyStopped`; one of `upstream.repair.reauthorize`, `upstream.repair.replaceKey`, `upstream.repair.topUp`, `upstream.repair.contactVendor`, or the non-linked `upstream.repair.contactProvider` fallback | 重新授权 on either supply channel → Reauth confirmation; 更换 Key → Key entry. A subscription's 补充额度 or 联系厂商 opens the matching §1.4 static destination in a new browser context and keeps this card in place; a later source payload decides whether its state changed. An `api_key` Source renders 联系你的服务商 with no link for either vendor-directed cause, because a compatibility vendor id does not identify the account operator |
| §1.11 | Reauth confirmation `[derived]` `[contract]` | 重新授权 pressed for a Hub or `native_cli` source from either the needs-action card or the capability-gated source overflow | F5 — no request has been sent | `upstream.repair.reauthConfirm.title`; exactly one of `upstream.repair.reauthConfirm.detail.onFailure` or `upstream.repair.reauthConfirm.detail.immediate`; `upstream.repair.reauthConfirm.confirm`, `upstream.repair.reauthConfirm.cancel` | The confirmation phase and literal request value are shared, but the complete consequence body is selected by channel: Hub renders only `onFailure`; `native_cli` renders only `immediate`. 继续登录 synchronously preallocates PD-1's blank context, then sends `POST /api/models/sources/<id>/reauth` with `{acknowledge_irreversible: true}` → Reauthorizing; 取消 / close / Escape restores the exact invoking card/menu origin and its focus target (C2/C3/C5/C8) |
| §1.11 | Reauthorizing | The shared acknowledgement was confirmed. The activating gesture has synchronously preallocated PD-1's blank context; `POST /api/models/sources/<id>/reauth` sends the confirmed `{acknowledge_irreversible: true}` for either supply channel `[contract]` | F1 before a flow is held → Repair failed, with every dismissal path locked while that request is pending (C4); PD-1 closes the unused context. After acquisition, §1.4 owns cancellation, 2s polling and F1–F5 | `upstream.repair.reauthorizing` | R3 and RR-1/RR-2 own acquisition: a non-terminal `flow` enters §1.4 with held `intent: reauth`, where E3a/E3b selects the actual presentation/progress state; an already-terminal `flow` is status-read immediately and never presented. RR-4 classifies the materialized terminal by returned `source.state` before pair cardinality: blocked → Repair unresolved; non-blocked + pairs → Repair impact reported; non-blocked + empty → M3 handoff. E8 `flow_not_found` runs RR-5's registered read before OAuth failed / Source gone; only E6's materialization failures stop polling and run the registered complete-surface refresh; E2, including `engine_down`, remains inconclusive. Create-only arrays are absent by contract |
| §1.11 | Key entry `[derived]` | 更换 Key pressed from either the needs-action card or the capability-gated source overflow, or a guarded refusal is abandoned | F5 — the secret remains local and no request is sent until submit | `upstream.repair.replaceKey` | V3 gates submit. A valid submit holds the normalized key and sends `PUT /api/models/sources/<id>/credential` with `{key}` → Replacing key; cancel restores the exact invoking origin and focus target (C2/C3/C5/C7) |
| §1.11 | Replacing key | Key entry submitted, or the guarded confirm below was accepted — the latter reuses the held key and adds `force: true` `[contract]` | F3 on guard refusal; F1 otherwise → Repair failed, with the key kept under F1. All dismissal paths are locked until the request settles (C4) | `upstream.repair.replacingKey` | R4 consumes the standard Source-mutation success: a non-empty `removed_hops` and/or `interrupted` → Repair impact reported; both empty → hold the returned `source` while M4 reads the complete model surface; refusal → Key replacement refused; failure → Repair failed |
| §1.11 | Key replacement refused `[derived]` `[contract]` | The non-forced credential replacement returned the shared guarded `409`; the typed key and exact Key entry origin are held | F3 — shared `Qp6FI`, with only the operation strings below changed | `guard.title.replaceKey`, `guard.subtitle.replaceKey`, `guard.confirm.replaceKey`, `guard.label`, `guard.count`, `guard.hop.position`, `guard.hint.safe`, `guard.hint.interrupt`, `guard.gap.label`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `guard.cancel` | 仍要更换 re-sends the held `{key, force: true}` → Replacing key; 取消 / close / Escape → Key entry with the typed key kept, by C2/C5 |
| §1.11 | Repair impact reported `[derived]` `[contract]` | R3 holds a successful OAuth repair whose returned Source is non-blocked and whose `interrupted_pairs` is non-empty, or R4 holds a successful key replacement with non-empty `removed_hops` and/or `interrupted`; the complete returned `source` is held | F2 for M3/M4's complete-surface read; the successful response is already in hand and DP-4 owns every exit | `upstream.repair.impact.title`, `upstream.repair.impact.detail`, `upstream.repair.impact.refreshFail`, `sourceDetail.impact.removedHops`, `sourceDetail.impact.interruptedModels`, `guard.hop.position`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `upstream.repair.impact.done`, `upstream.retry` | Render every non-empty response array under its matching hop or SupplyGap block. 完成, close, Escape or outside press run M3/M4's complete model-surface read; success returns with the current surface, failure keeps the report and changes the exit to 重试. The reread never replaces the held response evidence or restores the invoking origin |
| §1.11 | Repair unresolved `[derived]` `[contract]` | R3 holds a successful OAuth repair whose returned `source.state` is still `needs_action` or `error`, regardless of `recovered` and whether `interrupted_pairs` is empty | F2 for M3's complete-surface read; this is a successful terminal with a blocked result, not an F1 failure | `upstream.repair.unresolved`, optional `sourceDetail.impact.interruptedModels`, `guard.gap.subject`, `guard.gap.agents`, `gateway.modelCount`, `upstream.repair.impact.refreshFail`, `upstream.repair.impact.done`, `upstream.retry` | Keep the result visible in the gold needs-action treatment and render every non-empty `interrupted_pairs` block as independent evidence below it. 完成, close, Escape or outside press run M3's complete model-surface read; success returns with the current blocked projection, while failure keeps this exact result and changes the exit to 重试. Never show repaired/refreshed copy or auto-close from an empty array (C6/C10) |
| §1.10 / §1.11 | Committed projection stale `[derived]` | An M1/M2/M4 mutation or R3 terminal has commit evidence and then its required complete model-surface read failed. Hold the exact empty success envelope when one arrived; for D-36 inferred save/delete/repair commit, hold the reread Source or exact absence and mark every response-only impact/tail member unavailable. In both cases keep the last good dependent projections | F2 — the selected operation-specific refresh-failure sentence names the stale read, never the completed write | M1 `sourceDetail.edit.impact.refreshFail`; M2 `sourceDetail.remove.impact.refreshFail`; M3/M4 `upstream.repair.impact.refreshFail`; the matching `sourceDetail.retry` or `upstream.retry` | Render the committed Source or absence with the last good Agent/order/chain projections and put focus on 重试. 重试 repeats only the owning M1/M2/M3/M4 complete-surface read; success hands off its current projections, and another failure stays here. Never resend the mutation, turn unavailable arrays into empty arrays, invent impact rows, discard held commit evidence or describe the write as uncertain (C6/C9/C10) |
| §1.11 | Repair failed `[derived]` | The pre-flow reauth request or credential replacement failed before its terminal could be confirmed; the repair intent, channel acknowledgement, exact origin status and any typed key remain held | F1, on the repair surface | `upstream.repair.fail`, `upstream.retry` | RR-6–RR-9 first perform the producer-attempt read C10 registers: complete model surface for lost native acquisition or uncertain credential replacement, held Source id for Hub reauth. Then absent → Source gone; a held `needs_action`/`error` origin that is now clear → RR-7's M3/M4 handoff before rendering the reread Source as repaired; a still-blocked origin → remain here; any origin that was already non-blocked → remain here regardless of the present snapshot, because health is not mutation evidence. 重试 repeats the held producer — reauth with the same channel body, or credential replacement with the same key. After a flow is held, E4 uses §1.4 OAuth failed, E8 runs RR-5 before that classification, and E6 uses §1.4 OAuth materialization failed; E2 remains inconclusive in the bounded poll. Every branch preserves `intent: reauth` |
| §1.12 | Closed | 添加订阅 is rendered in 01's upstream footer | F5 | `upstream.addSubscription` | Activate → Open |
| §1.12 | Open | The frame 13 vendor menu is visible and focus is on its first row; the Add subscription trigger remains held as the focus owner | F5 | `addSubMenu.vendor.claude`, `addSubMenu.vendor.chatgpt`, `addSubMenu.recommendation.native`, `addSubMenu.recommendation.gateway` | Claude 订阅 → §1.4 with `vendor: anthropic`; ChatGPT 订阅 → §1.4 with `vendor: openai`. Selection closes the menu; if 04 later dismisses back to 01, focus returns explicitly to Add subscription, while a committed exit gives focus to 06. Escape / outside press → Closed with the same trigger focus (C3) |

One covered frame deliberately holds no rows: §1.2 specifies nothing (§0.2). The
`Qp6FI` confirm is not a frame of its own — its refusal rows sit with each caller,
including the frame 11 source-level mutations registered above.

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
| `{{count}}` | A cardinality. The i18next plural family on the key picks the form; the number is never written into the singular text by hand. | Always present | `addKey.pull.result`, `gateway.collapse`, `gateway.modelCount`, `guard.count`, `shell.allDirect`, `sourceDetail.refetch.removed`, `sourceDetail.summary`, `takeover.pill`, `upstream.count` |
| `{{backend}}` | The backend's product name — Claude Code, Codex, opencode — never the internal id. | Always present | `adopt.subtitle`, `adopt.title`, `adopt.undo.2`, `adopt.undo.3`, `guard.gap.subject`, `order.title`, `upstream.state.supplyingNative` |
| `{{vendor}}` | The upstream vendor's product name, as the user chose it. | Always present | `addSub.title`, `addSub.paste.title.code`, `addSub.paste.title.callbackUrl`, `adopt.effects.1` |
| `{{host}}` | The source's host, as entered, without scheme or path. | **Absent when the source has no entered host** `[contract]`: `base_url` is `api_key`-kind only, null there means the vendor's official endpoint, and a subscription may not carry one at all. §1.6 states what the one string that interpolates it renders instead. | `sourceDetail.summary` |
| `{{source}}` | A source's display name. | Always present | `gateway.row.current`, `gateway.row.currentTakeover`, `guard.title.editSource`, `guard.title.refetch`, `guard.title.removeModel`, `guard.title.removeSource`, `guard.title.replaceKey`, `sourceDetail.edit.title`, `upstream.repair.reauthConfirm.title` |
| `{{model}}` | A model's display id, as the source reports it. | Always present | `guard.title.removeModel` |
| `{{models}}` | Several model ids, joined by `、` / `,` — the ids that left the discovered slice on one fetch. Ids as the source reported them, never display names, because the row that carried a display name is the row that is gone. | Always present in the one key that carries it: `sourceDetail.refetch.removed` renders only when at least one id was removed, which is the same branch guarantee that keeps its `{{count}}` off zero. | `sourceDetail.refetch.removed` |
| `{{n}}` | A hop's 1-based position in the configured order. | Always present | `guard.hop.position`, `guard.hop.position.removeSource` |
| `{{menuModel}}` | A protected **menu** model's id — `SupplyGap.model_id`. It is its own slot rather than a second use of `{{model}}` because the two name different things and the guard turns on the difference: `{{model}}` is an id a source reports, and `model-hub.md` says 「the protected identifier is always the menu model, never a hop's upstream `model_id`」. | Always present | `guard.gap.subject` |
| `{{agents}}` | The enabled named Vibe Agents that pinned this menu model, by name, joined by `、` / `,` — `SupplyGap.agents` `[contract]`. | Always present in the one key that carries it: `SupplyGap.agents` 「is present and may be empty」, and an empty one renders no line at all, which is `model-hub.md`'s 「names affected Agents **when any exist**」 read as a branch rather than as an empty list. | `guard.gap.agents` |
| `{{time}}` | A relative timestamp, looking back — 3 分钟前 / 3 minutes ago. | **Absent when `last_discovered_at` is null** `[contract]` — no discovery has ever completed, so the inventory has no age to report. The one string that interpolates it is not rendered, and the status line drops that segment: 使用中 alone. A hand-populated source that has never fetched reads exactly that way; §1.6 states why it is Ready rather than an empty state. | `sourceDetail.status.listUpdated` |
| `{{delay}}` | A rough interval, looking forward — how long until the automatic retry. Same shape as `{{time}}` and the opposite direction, which is why it is its own slot: one string says a fetch happened 3 分钟前, the other says a retry comes 3 分钟后, and a single "relative timestamp" covers both while meaning neither. | Always present in the one key that carries it, because a `retry_at` still ahead is what selects that key. **A `retry_at` that has passed cannot fill it** — the interval renders zero or negative — and that is an ordinary reading, not an edge: no row in this document promotes a source on a clock, so `cooldown` can be reported with its retry time behind it for as long as the next payload takes. That reading renders `upstream.state.unavailableDue`, which is the absence rule's 「a state that cannot fill it uses a different key」 applied to a slot that may not be dropped. | `upstream.state.unavailableRetry` |
| `{{protocol}}` | The interface the probe proved, by its display name (§1.5's three options). | Always present in ⑤ / ⑤′, which are entered only after a protocol was proved. Never rendered by ④ / ④′, whose whole content is that no protocol was proved. | `addKey.inventory.detail` |
| `{{request}}` | The request that produced the evidence, as `METHOD path`. | Always present — a classified failure has a request by construction. | `addKey.inventory.detail`, `addKey.undetermined.detail`, `adopt.fail.detail` |
| `{{status}}` | The HTTP status the upstream returned. | **Absent on a transport failure**, which never reached HTTP. The segment is dropped by the absence rule, so the string reads `{{protocol}} 已认出 · {{request}} · {{reason}}`. | `addKey.inventory.detail`, `addKey.undetermined.detail`, `adopt.fail.detail` |
| `{{health}}` | One of exactly five words — `gateway.group.status.ok`, `gateway.group.status.degraded`, `gateway.group.status.waiting`, `gateway.group.status.interrupted`, `gateway.group.status.noSelection`. The first four are `supply_status` readings; the fifth is Hub mode with nothing pinned to roll up, which is a state of this backend and not a health of its supply. Supply health, not an HTTP status: nothing here is a code, and the group renders it whether or not any request was made. | Always present | `gateway.group.subtitle.gateway` |
| `{{reason}}` | The classified cause, from the closed set the state's own copy declares. That set is **total**: it carries a residual key the state falls back to, because this slot is always present and an unmatched failure still has to render one word. No consumer interpolates an upstream string here. | Always present | `addKey.inventory.detail`, `adopt.fail.detail` |
| `{{mode}}` | One of exactly two words — `gateway.group.mode.direct` or `gateway.group.mode.gateway`. The subtitle interpolates the word rather than carrying two whole strings, because the health half varies independently of it. | Always present | `gateway.group.subtitle.direct`, `gateway.group.subtitle.gateway` |
| `{{backends}}` | The backends that have this source configured into a route, by product name, joined by `、` / `,` — `adopted_by`'s projection, grouped and de-duplicated, never a computation over live chains (§1.0). | Always present — `upstream.state.supplying` is entered only when the list is non-empty; a source no backend has configured is 备用 instead | `upstream.state.supplying` |
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

**Two parts of the shell are conditional, and the frames disagree about them on
purpose.** Frames 09 and 10 draw the header but **no tab strip and no `cols`
track** — in the direct-only state there is no gateway module to put in the second
column and no second section to tab to, so the chrome that organizes those things is
absent rather than empty. Read the tab strip as a property of the gateway-adopted
layout, not of the page. §1.8 states the condition.

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
| `body` | 1120×854, `layout: none` (children positioned absolutely) |
| `cols` | 1120×806, `gap: 16` → upstream 384 + rail 72 + gateway 632 |
| Module card | `$--surface`, `border 1 $--border`, `radius 14` |
| Legend row | 1120×34, `gap: 18`, `space_between`, swatch 20×2, label 11 / 500 `$--muted` |

**Element inventory**

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| `title` + info icon | Page name | static | icon: hover, focus **and** activation `[derived]` | Tooltip: `shell.gatewayInfo.body`, which is the icon's accessible description; `shell.gatewayInfo.label` is its accessible name |
| Run pill | Engine liveness | `runtime-dependency.schema.json` → `status.health` `[contract]` | see the mapping below | see the mapping below |
| Tabs ×2 | Section switch | — | yes | 来源与网关 / 用量与额度; the active one gets the mint underline. **Which route these correspond to is not specified by these frames** (§0.1) |
| Upstream module | Source inventory | `GET /api/models/sources` `[spec]` | rows: yes | Open 06 for that source |
| Dispatch rail | That upstream feeds gateway | derived, decorative | no | — |
| Gateway module | One group per backend, each with model rows | per-backend supply + chains `[spec]` | rows, collapse, 「来源顺序」, mode switch | Open 02 / expand / open 03 for **that backend** / open 10's confirm |
| Legend | Colour → meaning | static; kept in bijection with the inks the page draws | no | — |

**The info icon is a control, and its string is this file's** `[derived]`. Hover is not
an affordance a keyboard or a touch user has, and this tooltip is the only place the page
says what the gateway *is* — so the icon is a focusable button, activating it toggles the
tooltip, Escape dismisses it, `shell.gatewayInfo.label` is its accessible name and
`shell.gatewayInfo.body` its accessible description. The string is registered here
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

Every state above except Ready is **not drawn** `[derived]`. Required behaviour:

- Empty: upstream module keeps its head and footer and shows one line —
  「还没有来源。先添加一个订阅或 API Key。」 The gateway module shows its backend
  groups with 「没有可用来源」 per group rather than vanishing; a backend that
  exists is a fact independent of whether anything can supply it.
- **Not installed**: the pill reads `shell.notInstalled` and **is** an activation
  target — for installation, not for start `[derived]`. It carries the same idle styling
  as Not started, for the same reason: a missing optional component is not a fault. It
  must never offer 点击启动, because starting is not the action that resolves it. The
  runtime contract enumerates `not_installed` alongside `not_started`
  (`runtime-dependency.schema.json`, `health`) `[contract]`, so a UI that collapses the
  two renders a start button that cannot succeed and reports the failure as if the
  engine had crashed. Activating the pill opens an install confirm that names the
  component and its rough duration before anything is downloaded, exactly as D-26
  requires — but it is the **non-switching variant**, and the difference is not
  cosmetic `[derived]`.

  | | From a backend's 切换到网关 (D-26) | From the run pill |
  | --- | --- | --- |
  | Title | 把 {{backend}} 切换到网关 | 安装网关组件 |
  | What it promises | install, start, and move that one backend to the gateway | install and start the component; **no backend changes mode** |
  | Bullets | `models.hub.adopt.effects.*` — consequences for `{{backend}}` | `models.hub.install.effects.*` — the component is installed, the gateway starts, and every backend stays where it is |
  | Primary | 安装并切换 | 安装并启动 |
  | Where it lands | that backend on the gateway | the same page, run pill in Starting then healthy |

  Reusing D-26's confirm here would be underspecified and then wrong: the run pill is a
  page-level control with **no backend in hand**, so an implementation would have to
  invent one to fill `{{backend}}` in the title and the four `effects.*` bullets, and
  whichever it picked would silently switch a backend the user never named. The user
  pressed 点击安装; the confirm may promise installation and nothing else. Both variants
  share the component name, the duration and the download-nothing-before-consent rule —
  which is the part D-26 exists to keep from diverging — and differ on exactly the
  consequence each entry point actually has.
- **Installing**: the confirm does not close on acceptance. Its primary becomes an
  in-place progress state — `install.progress`, spinner, both buttons inert — because a
  dialog that closes on 安装并启动 hands the user back a page whose pill still reads
  未安装, which is the one reading that means *nothing happened* `[derived]`. Dismissal
  is unavailable while it runs, for the reason D-15 gives from the other side: the way
  out of this state is the operation finishing, and a 取消 that cannot actually stop a
  download in flight would be a button that lies. What it costs to say so is one
  sentence; what it costs to leave unsaid is a user who presses 取消, sees the dialog
  close, and has no idea whether a binary is landing on their disk.
- **Install failed**: the dialog stays open, the message is replaced, and the primary
  becomes 重试 `[derived]` — the same shape §1.4's failure rows take, and for the same
  reason. Dismissing from here renders **whatever the next `health` read reports**, and
  this page asserts nothing beyond that. It said 「a failed install leaves nothing
  behind」 until this round, which is a claim about the disk made by the one participant
  that never saw it: the install has no route (G-10), so the page watched a client-side
  operation and learned only that it did not finish. Failing after the staged component
  is in place and its metadata written is an ordinary way for it to fail, and the next
  read is then `not_started` or `degraded` rather than `not_installed` — while a page
  that had already decided the answer would draw 点击安装 over a component sitting there
  installed. `install.fail.detail` tells the user the same thing in one sentence —
  组件可能处于未完成状态 — so the paragraph that promised otherwise was contradicting
  this file's own copy as well as §0.8's row.
- **Re-entry while an install is in flight is the one case this page cannot answer
  honestly, and that is G-10** `[contract-gap]`. Installing is a client-side state with
  no field behind it — `runtime-dependency.schema.json` has `not_installed` and
  `not_started` and nothing between them — so a user who navigates away and returns
  re-reads `health` and sees Not installed, while the download may well still be
  running. The page must not paper over that by remembering a local flag across a route
  change: a fabricated 安装中 that survives a reload would keep claiming progress after
  a failure it cannot observe. Until the contract carries an install state, re-entry
  shows the truthful `health` reading, and the confirm's own 安装并启动 is idempotent —
  pressing it again on a component that is already landing must not start a second
  download.
- **Unsupported host**: `manifest.assets` is per platform, and the README states that
  unsupported hosts **fail closed** with 直连 as the escape hatch `[contract]`. So on a
  platform with no pinned asset the pill reads `shell.unsupported`, carries the idle
  treatment, and has **no** activation target. This is the one place the pill's mapping
  splits a `health` value on a second field, exactly as frame 06's bar splits `active`
  on adoption: offering 点击安装 here would be D-9a's dead control with a download URL
  behind it — a confirm that names a component, promises a duration, and then cannot
  find a file to fetch.
- **That split has no contracted input, and this page does not guess one**
  `[contract-gap]` **G-24**. Deciding it needs the host's platform, and the payload
  reports only which platforms have a *published* asset: `manifest.assets[].platform`
  is a property of the manifest, not of the machine the component would land on, and
  `status` carries `installed_version`, `verified`, `listening`, `health` and
  `last_check` and nothing else. The one identifier a client can reach without asking
  the server is the browser's own, and on a UI opened from another machine that names
  the wrong host — a value read off the wrong subject is worse than no value, because
  it renders exactly as though it were the right one (D-28 refuses the same substitution
  one field over). So until the payload carries a host platform or a plain
  installability flag, `not_installed` renders **Not installed** with its install
  affordance live, and a host with no asset learns that from the install failing. That
  cost is stated rather than hidden: it is D-9a's dead control arriving one press later,
  which is the honest ordering while the page cannot tell the two hosts apart, and the
  alternative is worse — withholding the install from every host that could have used
  it, on the strength of a field describing somebody else's machine.
- **Not started**: the pill reads `shell.notStarted` and is the page's start
  affordance. It is styled as an *idle* pill — `$--muted` label on `#FFFFFF0A`,
  **not** the error treatment `[derived]`. The runtime contract classes
  `not_started` as lazy-start idleness rather than an alarm `[contract]`, and a
  page that paints idleness red teaches users to ignore the colour that matters.
  Derived columns render `—` exactly as in Unreachable; supply that has never been
  arbitrated is unknown, not empty.
- **Starting**: the pill reads `shell.starting` with the `loader-circle` spinner and
  stops accepting activation, so a second click cannot queue a second start
  `[derived]`.
- Unreachable: the run pill flips to `shell.stopped` — the error treatment, because an
  engine that *was* running and stopped answering is a fault — and every derived
  column (current source, chain, takeover) renders `—`, **not** a stale last-known
  value. See D-3: a surface that cannot prove a fact must say so.
  Recovery offers the same start action as Not started.
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
  first-paint dispatch offering only Unreachable resolves it to 网关未运行 with a Start
  button, for an engine that answered. It also discards the inventory that did load, so
  the repair costs the user a fetch that already succeeded. A partial payload is partial
  whether it is the first or the fiftieth.
- **Sources unread**: the mirror of Partial, and it needs its own row for the reason
  Partial does. A failed source list used to arrive at Unreachable, which is the sentence
  for an engine that stopped answering: it renders 网关未运行, it offers Start, and it
  blanks every derived column. None of that is true here — the status read answered, the
  supply read answered, and one list did not — so the pill keeps whatever `health` said,
  no other region degrades, and the repair is to ask for the list again. Reading an
  outage off the failure of one read is the same mistake in the other direction as
  reading health off a stale value: both state a fact the payload did not carry.

**The run pill is a total rendering of `health`, and the states above are how it gets
there** `[contract]`. Every value `runtime-dependency.schema.json` admits for it is
rendered, and no pill is drawn for anything else. One value splits, on a second field —
the same shape frame 06's bar has when it splits `active` on adoption:

| `RuntimeDependency.status.health` `[contract]` | State | Pill | Treatment | Activation |
| --- | --- | --- | --- | --- |
| `ok` | Ready | `shell.running` | idle | none |
| `degraded` | Impaired | `shell.degraded` | error | none |
| `down` | Unreachable | `shell.stopped` | error | start (`POST /api/models/runtime/start` `[contract]`) |
| `not_started` | Not started | `shell.notStarted` | idle | start |
| `not_installed`, with a manifest asset for this platform `[contract]` `[contract-gap]` **G-24** | Not installed | `shell.notInstalled` | idle | the non-switching install confirm — **never** the start route |
| `not_installed`, with none `[contract]` `[contract-gap]` **G-24** | Unsupported host | `shell.unsupported` | idle | none — 直连 (§1.8) is the escape hatch |

Starting is the one pill with no `health` behind it: it is the client's own optimistic
state between accepting the press and the next payload. A transport failure — the status
request not returning at all — renders as Unreachable, which is the one pill two inputs
share, because from the page's side an engine that says `down` and an engine that says
nothing afford the same action. And `degraded` here is the *engine* speaking about
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
`gateway.modelCount`, `gateway.collapse`, `addKey.pull.result`, `guard.count`,
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
| `shell.stopped` `[derived]` | 网关未运行 | Gateway not running |
| `shell.degraded` `[derived]` | 网关降级运行 | Gateway running degraded |
| `shell.notStarted` `[derived]` | 网关未启动 · 点击启动 | Gateway not started · click to start |
| `shell.notInstalled` `[derived]` | 网关组件未安装 · 点击安装 | Gateway component not installed · click to install |
| `shell.allDirect_one` `[frame]` | {{count}} 个后端都在直连 | The only backend is direct |
| `shell.allDirect_other` `[frame]` | {{count}} 个后端都在直连 | All {{count}} backends are direct |
| `shell.starting` `[derived]` | 正在启动… | Starting… |
| `shell.unsupported` `[derived]` | 这个平台还没有网关组件 | No gateway component for this platform yet |
| `shell.gatewayInfo.label` `[derived]` | 什么是网关 | What the gateway is |
| `shell.gatewayInfo.body` `[derived]` | 网关是本机的一层调度:它持有你添加的来源,按你配置的顺序供给各个 Agent。 | The gateway is a dispatch layer on this machine: it holds the sources you add and supplies them to your Agents in the order you configure. |
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
| `install.fail.detail` `[derived]` | 这次安装没有完成,组件可能处于未完成状态。重试会重新装一遍。 | The install did not finish, and the component may be left incomplete. Retrying installs it again from scratch. |
| `install.retry` `[derived]` | 重试 | Try again |
| `shell.tab.hub` | 来源与网关 | Sources & gateway |
| `shell.tab.usage` | 用量与额度 | Usage & quota |
| `upstream.heading` | 来源 | Sources |
| `upstream.count_one` | {{count}} 个 | {{count}} source |
| `upstream.count_other` | {{count}} 个 | {{count}} sources |
| `upstream.group.native` | 本机原生 | Native · on this machine |
| `upstream.group.hub` | 网关持有 | Held by the gateway |
| `upstream.kind.nativeCredential` | 原生 · 本机凭据 | Native · local credential |
| `upstream.kind.subscription` | 订阅 | Subscription |
| `upstream.kind.apiKey` | API Key | API key |
| `upstream.state.supplyingNative` `[spec]` | 正在供给 {{backend}}(原生) | Supplying {{backend}} (native) |
| `upstream.state.supplying` `[spec]` | 正在供给 {{backends}} | Supplying {{backends}} |
| `upstream.state.standby` | 备用 | Standby |
| `upstream.state.unavailableRetry` | 暂不可用 · {{delay}} 后自动重试 | Unavailable · retrying automatically after {{delay}} |
| `upstream.state.unavailableDue` `[derived]` | 暂不可用 · 已到重试时间 | Unavailable · the retry is due |
| `upstream.state.supplyStopped` `[frame]` | · 已停止供给 | · stopped supplying |
| `upstream.empty` `[derived]` | 还没有来源。先添加一个订阅或 API Key。 | No sources yet. Add a subscription or an API key first. |
| `upstream.unread` `[derived]` | 来源列表没读到 · 网关本身正常 | Could not read the source list · the gateway itself is fine |
| `upstream.retry` `[derived]` | 重试 | Retry |
| `upstream.addSubscription` | 添加订阅 | Add subscription |
| `upstream.addApiKey` | 添加 API Key | Add API key |
| `upstream.repair.reauthorize` `[frame]` | 重新授权 | Reauthorize |
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
| `gateway.heading` | 网关 | Gateway |
| `gateway.sourceOrder` | 来源顺序 | Source order |
| `gateway.switchToGateway` | 切换到网关 | Switch to gateway |
| `gateway.switchToDirect` | 切换到直连 | Switch to direct |
| `gateway.modelCount_one` | {{count}} 个型号 | {{count}} model |
| `gateway.modelCount_other` | {{count}} 个型号 | {{count}} models |
| `gateway.group.subtitle.direct` `[frame]` | {{mode}} | {{mode}} |
| `gateway.group.subtitle.gateway` `[frame]` | {{mode}} · {{health}} | {{mode}} · {{health}} |
| `gateway.group.mode.direct` | 直连 | Direct |
| `gateway.group.mode.gateway` | 网关 | Gateway |
| `gateway.group.status.ok` `[contract]` | 正常 | Healthy |
| `gateway.group.status.degraded` `[contract]` | 降级 | Degraded |
| `gateway.group.status.waiting` `[contract]` | 暂时全部在冷却 | All sources are cooling down |
| `gateway.group.status.interrupted` `[contract]` | 无可用来源 | No source is available |
| `gateway.group.status.noSelection` `[derived]` | 未选型号 | No model selected |
| `gateway.group.takenOver` | 接管中 | Taken over |
| `gateway.supply.none` `[derived]` | 没有可用来源 | No usable source |
| `gateway.supply.unread` `[derived]` | 后端供给情况没读到 · 网关本身正常 | Could not read this backend's supply · the gateway itself is fine |
| `gateway.fail.switchToDirect` `[derived]` | 没能切回直连 | The switch back to direct did not go through |
| `gateway.retry` `[derived]` | 重试 | Retry |
| `gateway.group.emptyModels` `[derived]` | 这个后端没有可用型号 | This backend has no models |
| `gateway.row.current` | 当前 {{source}} | Now: {{source}} |
| `gateway.row.currentTakeover` | 当前 {{source}}(接管) | Now: {{source}} (takeover) |
| `gateway.collapse_one` | 还有 {{count}} 个型号 | {{count}} more model |
| `gateway.collapse_other` | 还有 {{count}} 个型号 | {{count}} more models |
| `legend.native` `[frame]` | 原生 | Native |
| `legend.viaGateway` | 网关供给 | Gateway supply |
| `legend.connectedUnused` | 已启用 · 当前未被使用 | Enabled · not currently used |
| `legend.takeover` | 接管中 · 临时改走 | Taken over · temporarily rerouted |
| `legend.unavailable` | 供给已暂停 | Supply paused |

**The mode word is read off `AgentSupply.mode` and the health word off
`AgentSupply.supply_status`, and neither ever stands in for the other** `[contract]`. The
group header renders the outer rollup — the backend's, for the model it is pinned to —
and that field is nullable for two different reasons. Direct mode produces `null` because
nothing is being arbitrated. Hub mode produces `null` too, whenever `selected_model_id`
is `null`, because there is nothing yet to roll up — `agent-supply.schema.json` does not
merely allow that pairing, it requires it. Reading 直连 off the null therefore tells a user their backend
bypasses the gateway while its persisted `mode` reads `hub` — a false statement about the
one fact this subtitle exists to carry, and false exactly for the backend a user has
switched on but not finished configuring. So `mode` decides the first word every time,
`supply_status` only ever decides the second, and the missing rollup is a word of its own:

| `AgentSupply.mode` `[contract]` | `AgentSupply.supply_status` `[contract]` | Subtitle | Key |
| --- | --- | --- | --- |
| `direct` | not read — Direct arbitrates nothing, so it rolls nothing up | 直连 | `gateway.group.subtitle.direct` + `gateway.group.mode.direct` |
| `hub` | `ok` | 网关 · 正常 | `gateway.group.subtitle.gateway` + `gateway.group.status.ok` |
| `hub` | `degraded` | 网关 · 降级 | `gateway.group.subtitle.gateway` + `gateway.group.status.degraded` |
| `hub` | `waiting` | 网关 · 暂时全部在冷却 | `gateway.group.subtitle.gateway` + `gateway.group.status.waiting` |
| `hub` | `interrupted` | 网关 · 无可用来源 | `gateway.group.subtitle.gateway` + `gateway.group.status.interrupted` |
| `hub` | `null` | 网关 · 未选型号 | `gateway.group.subtitle.gateway` + `gateway.group.status.noSelection` |

**The four `[contract]` words are `model-hub.md` §4.5's wording, transcribed** `[contract]`.
正常 / 降级 / 暂时全部在冷却 / 无可用来源 are that section's zh (UI) column verbatim, and
it says they are the only backend-level supply-health wording — so this table is a place the
projection is rendered, not a place it is named again in words that read better here. Two of
them read as descriptions rather than labels, which is the point: `waiting` heals itself at
the earliest `retry_at` and `interrupted` does not, and 等待重试 / 已中断 blurred exactly
that split while sounding tidier. A change to any of the four is a change to §4.5 first.

`gateway.supply.none` is not a fifth word, because it is not the same grain `[derived]`.
The four above are the backend rollup rendered into the group head's status line, which
§4.5 governs. `gateway.supply.none` is a body line under a group that has no source at
all — a statement about that backend's inventory, not a reading of `supply_status`, which
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

**This document renders the backend's rollup and no other grain** `[frame]`.
`agent-supply.schema.json` declares the status name twice, and the inner one —
`AgentSupply.named_agents[].supply_status` — is a named Agent's own rollup for its own
explicit model. It holds independently of its backend's: an Agent pinned to a model no
source can serve reads `interrupted` inside a backend whose rollup reads `ok`. That
divergence is real and this release does not surface it, because no frame draws a
named-Agent row — §1.0 and §1.1 inventory backend rows and their menu-model rows, and
nothing else. Requiring one anyway is how this section read until this round, and it left
an implementer with a rendering obligation and nowhere to render it, whose only reachable
discharge was to put the group's word beside an Agent's name — the one thing the
divergence makes wrong. So the requirement is withdrawn rather than relocated: the group
header states the backend's rollup, §1.1 and §1.7 render that one mapping and state none
of their own, and a per-Agent surface is a frame somebody has to draw before this file
can specify what it says.

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
first hop is unavailable for a *recoverable* reason (AC-30, D-21, C-5). Recoverable is
`cooldown` and nothing else: it is the one `Source.state.status` value that clears on its
own, and 接管中 promises exactly that clearing. A head blocked by anything else is also
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

**A source's supply line renders `adopted_by`, not the chain** `[contract]` FC-05 —
**and on any load that is not the creation response it has nothing to render it from**
`[contract-gap]` G-20. `upstream.state.supplying*` names which backends have this source
**configured into a route** — the same reading §1.6's 使用中 gets one surface over, and
for the same reason. *Stable* is the operative word in the field's own definition: the
array is unchanged by a cooldown, a revoked credential, or a takeover that routes past
this hop entirely, so a line that read it as live traffic would be false in precisely the
states a user opens this page to understand. Two things keep the rendered word honest
anyway. The line is selected by the source's **state** first — a cooling or blocked source
shows its state word instead, which is what §1.7's card delta *is* — so it appears only on
a healthy source; and what it then claims is configuration, not flow. The live question is
the chain read's (§1.2), and D-28 is the rule that keeps the two projections from standing
in for each other. FC-05 states that `agent-supply` exposes
`adopted_by: [{backend, menu_model}]` from the persisted references and that source-card
attribution reuses that projection, and `api.md` calls that array 「the stable Source-card
projection」 in as many words — which is what makes the field authority here even though
the frozen `agent-supply.schema.json` omits it (§0.3, the FC-05 case). What is missing is
not the shape but the read: every response that carries it is a *creation* terminal, and
neither `GET /api/models/sources` nor `GET /api/models/agents` returns it, so a page that
paints this line after a reload is painting a value the contract never sends. That is
G-20, and it is the FC-12 case rather than the FC-05 one. The line groups the projection
by backend and de-duplicates, so it carries no position and no menu-model detail. This
section owns that reading; §1.1 and §1.7 render it and name no source of their own. The
chain is the other projection — the stored `hops` array, read by §1.2 and §1.3 — and
D-28 is why neither may be computed from the other.

**Under G-20 the line is absent, not derived** `[derived]`. The two candidate
work-arounds are both worse than the gap showing through: recomputing attribution from
the chains is the one derivation D-28 forbids by name, and holding the creation
response's array in client state would make the card correct for one session and
confidently wrong in the next — a source adopted by a chain edited elsewhere would still
claim its old backends, which is the failure D-28 exists to prevent. So the card renders
the line when a response carries the projection and omits it otherwise. A missing line
reads as *unknown*, which is true; a stale one would read as *known*, which is not.

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
| `heujA` upstream card | tile icon by kind, name, kind pill, mono detail line, one status line | one source | yes (whole card) | Open 06 for that source |
| `uf3re` detail | account label, or `host/path · masked key` — **and every field it can draw is nullable, so the line is specified by omission, exactly as §0.9 and §1.6 rule the same hole** `[contract]`. `account_label`, `base_url` and `masked_credential` are each `["string","null"]` in `source.schema.json`, so a segment with no value is dropped, never rendered empty and never left behind a dangling `·`; `base_url: null` is the vendor's official endpoint (§0.9's `{{host}}`) and is not synthesized into a hostname (§1.6). When nothing is left the whole line is omitted rather than filled — a subscription that reports no account label is the common case, and the card still identifies itself from four required fields (icon and pill by `kind`, name by `display_name`, state by `state`). Repeating the kind pill or the card's own name here would be the only alternative, and it would say nothing the card has not already said | source | no | — |
| `YcOFo` status | which backends have this source configured into a route — `adopted_by`, absent under `[contract-gap]` G-20 (§1.0) | that projection when a response carries it, never a computation over chains (D-28) | no | — |
| `wmROQ` / `Xitl7` footer buttons | Add subscription / Add API key | — | yes | 添加订阅 opens frame 13; its selected vendor opens 04. 添加 API Key opens 05 |
| `f8w6Xp` + `pnYa0` rail | dispatch happens between the columns | decorative | no | — |
| `GLylJ` backend group | backend tile, name, model count, head buttons, and one `{{mode}} · {{health}}` line | per-backend mode + supply health | head: buttons only | — |
| `ehGRK` / `bGsC7` 「来源顺序」 | — | — | yes | Open 03 **for that backend** |
| `IyKyp` 「切换到网关」 | — | backend in 直连 | yes | Open the 10 confirm for that backend |
| `z02Ep` / `gbrq2` 「切换到直连」 | — | backend on the gateway | yes | That backend leaves the gateway immediately — **no confirm** (D-30) |
| `Exx0a` model row | model id (mono 12), a chain chip, current-source text | chain head per model | yes | Open 02 for `(backend, model)` |
| `ZM1pm` collapse row | `还有 N 个型号` | count of hidden rows | yes | Expand in place |
| `FZUYI` wire layer | one path per supply relation + endpoint dots | derived supply set | no | — |
| `ftWgW` legend info icon | the legend's note — **the string is measured from the frame, not specified here** (§0.2) | static | hover, focus **and** activation — the same three §1.0's title icon carries | Tooltip, the note standing as the icon's accessible description |

**The 当前 line is a third read, and the page does not wait on it** `[contract]`. Every
element above it is drawn from the two page-level payloads — tile, name, count, the mode
and status line, the head buttons, the collapse row — but the serving hop is in neither:
`api.md` states that AgentSupply projects no backend-level serving head, and that
`model_supply` carries only `chain_length`. The one read that carries hops at all is
`GET /api/models/agents/<backend>/chain?model=<id>`, per model and Hub only — a 直连
backend gets the documented `direct_mode` refusal. So 「Ready」 is defined on the two
payloads on purpose: waiting on the third would hold the whole page for one row's
projection, and on a 直连 group it would wait for a read the contract refuses. That
refusal is also why a 直连 group draws no 当前 line and no takeover rather than an empty
one — there is nothing there to be pending about.

**And when that read answers, it carries the hops without saying which one is current**
`[contract-gap]` **G-31**. `model-hub.md` §4.3 puts `current` in the read projection —
「the read projection is `C` with live annotations plus `current`」 — and writes takeover
on it; `agent-chain.schema.json` closes the payload without it. What survives is most of
the row: the hops, their configured order, their health, their runnability and the
model-grain `supply_state` are all on the wire, so the cost is bounded and nameable
rather than structural. 当前 therefore renders **the hop a turn would use now** — the
schema's own 「the next turn uses the FIRST item with `runnable: true`」 — and that
reading is exact except across the one interval §4.3 legislates: 「recovery changes
current execution position **on the next turn**」, so between a head becoming runnable
again and the turn that actually moves back, the wire says the head and the truth is
still the later hop. §1.1's *Takeover active* states that interval instead of pretending
the two readings are one, and no frame here reads `current` as though it had arrived.

**Until that read answers, the derived columns say so** `[derived]`. Current source,
chain and takeover render `—` while the chain read is outstanding, and again when it
comes back failed or refused. This is not a new rendering: it is the one §1.0 already
gives those same three columns when the engine is unreachable, for the same reason (D-3
— a surface that cannot prove a fact must say so). **The rendering is shared; the state
is not** — 「Chain unresolved」 is a row-grain state of its own precisely so that this
one cannot be written as a transition into §1.0 Unreachable, which would stop the run
pill, offer 启动引擎 and clear every derived column on the page over a request about one
model. A stale last-known hop is specifically excluded. AC-30 makes takeover a projection of the chain the surface
displays, so a takeover badge drawn from a chain no longer in hand is a projection of
nothing — the one failure that rule exists to prevent. And a failed chain read degrades
those columns and nothing else, which is §1.0's Partial rule read at row grain: only the
sub-tree that failed degrades, and the group keeps everything the other two payloads
drew.

**A read that can fail has to be re-issuable, and the collapse row is what re-issues
this one** `[derived]` D-35. 「Chain unresolved」 is the only failure state on this page
whose repair is not a control drawn beside it: the three columns render `—`, the frame
carries no per-row 重试, and a row left there would be waiting on a request nothing was
going to send again. Collapsing a group and expanding it re-reads every row in it, which
costs no new control and reads as what it already means. The page's own two triggers sit
beside it — any mutation re-renders the group, and the next load re-reads everything —
so the user-available repair and the ambient ones agree. What this row must not do is
resolve on a clock: a poll cadence is a number this file has no basis to pick, and 「no
exit keys on elapsed time」 is the rule the two source rows above are written to.

**The three head buttons are mutually constrained** `[frame]`, and the constraint is
the whole model in one line: a backend is either on the gateway or not. On the gateway
it carries 来源顺序 + 切换到直连; in 直连 it carries 切换到网关 and **nothing else** —
Claude Code's head has no order button, because a direct backend consults no source
order and an editor there would edit a list nothing reads. D-9a states the rule, and it
is a set equality over the three groups.

**Track metrics** `[frame]`: `cols` 1120×806, `gap 16`; upstream module 384 wide,
rail 72, gateway module takes the rest. Everything inside those tracks is
`fill_container`, so this file records the three track widths and the fixed heights
and never a derived row width — a literal there is a number that goes stale the first
time a track moves.

**Card and row metrics** `[frame]`: upstream card `fill_container`×80, `padding [0,12]`,
`gap 10`, `radius 10`; tile 34×34 `radius 9`; name 12.5/700 Inter; detail 10.5
JetBrains Mono `#9BA3B8CC`; status dot 5px + text 10.5/600. Backend group
`$--background` fill, `radius 12`, `$--border`; head 66 tall, `padding [0,14]`,
`gap 7`, bottom border, backend name 14/700 Inter, tile 30×30 `radius 9`, count pill
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
source, and they must not share a message** `[derived]`. 没有可用来源 says *this backend
has models and nothing can serve them* — the fix is a source. 「这个后端没有可用型号」 says
*this backend has no models to serve* — the fix is a model. One message for both spends
the single action the user takes on the strength of it, and spends it on the wrong thing
half the time. The group keeps its header and its `<mode> · <status>` line either way;
only the row area differs.

**This list states the cause and does not offer the fix, and that is a scope fact rather
than an omission** `[derived]`. None of the covered frames draws the surface that edits a
backend's model menu, so there is no add affordance on this list to keep enabled — and
naming where that surface lives would be a navigation path, which §0.1 forbids this file
to draw. The empty state claims the message and the preserved shell here, and claims a
live add affordance only for the lists that actually draw one.

**Extreme data**

Collapse predicate for a backend group `[frame]` for the shape, `[derived]` for the
ordering rule:

```
N = 3                                       # ADDITIONAL nominal rows, not a total

# 0. STATE — one per-row fact, read from the payload the group is already drawn from.
#    `model_supply` is an array in hub mode and null in 直连  [contract]
state(m) = unsupplied  iff  supplyRow(m) exists and its chain_length == 0
         = nominal     otherwise      # includes every row of a 直连 group

# 1. ORDER — one total order over the whole group, computed before anything is hidden
key(m)    = backendMenuIndex(m)             # the backend's own menu order, and only that
sorted    = sort(models, by=key)

# 2. SELECT — a filter over `sorted`, which never reorders it
mustShow  = { m in models | state(m) != nominal }              # hard: never collapsed
baseline  = take([m in sorted | state(m) == nominal], N)       # N ADDITIONAL nominal rows
visible   = [m in sorted | m in mustShow or m in baseline]
collapsed = models - visible

render collapse row  iff  |collapsed| > 0
collapse label count = |collapsed|
```

**`state` is one field of the same two payloads, and it has to be** `[contract]`
`[derived]`. The only per-row fact those payloads carry is `model_supply`'s
`chain_length`, whose contract says what it counts and what it does not: 「Length of the
exact configured Route chain… It is not a count of currently runnable candidates. Zero
marks a structurally empty Route; live blockers appear through `supply_status`.」 So
zero is *this model has no configured chain at all*, and every other value is a row with
hops stored in it — a statement about configuration, which is the only thing this field
counts. That is the whole state space this predicate gets, because
`supply_status` is one word for the backend and says nothing about which row it is about.

Everything else a row can be — serving past its head, taking over, waiting — is the
third read (「Chain unresolved」 above), and **the collapse must not touch it.** That read
is per model, asynchronous, and allowed to fail: a predicate reading it decides visibility
from how many requests have come back, so the group renders one way at first paint and
reorganizes itself as answers land, under whatever the user is pointing at. It is the
same non-determinacy the override tier was deleted for two paragraphs down, arriving
through a different field, and a failed read makes it worse than non-deterministic —
there is no answer to be non-deterministic about, and the two implementable readings are
both wrong: treat a pending row as non-nominal and the whole menu expands on every paint,
treat a failed read as nominal and the rule quietly stops being hard. A row whose chain
read is outstanding, failed or refused keeps exactly the visibility this predicate already
gave it, and renders `—` in its three derived columns where it sits.

**One row this predicate cannot protect, and the reason is the payload rather than the
rule** `[contract-gap]` **G-25**. `chain_length` counts stored hops, and `model-hub.md`
§4.6 keeps a hop the live read cannot run **on purpose** — 「its live reason remains
visible until the Source recovers, the user removes or changes the pair, or a guarded
cascade removes it」. Deletion is not one of those reasons and this row does not claim
it is: §4.5 refuses an unforced Source `DELETE` while any configured chain names that
Source, and `force=true` removes the Source and every hop naming it **in the same
transaction**, so the contracted delete path leaves nothing dangling behind it. The
stale hop this row is about is the ordinary one — a model the Source no longer
advertises, or a pair whose Source sits in a state that does not clear itself — and
`source_missing` stays a real chain reading for the store that was corrupted or edited
outside those routes. So a menu model whose
every hop is stale reports the same nonzero `chain_length` as a healthy one, reads
`nominal` here, and is collapsible — while it is exactly the row that needs a person:
nothing about it heals on its own, a model the Source stopped advertising returns only
if the Source advertises it again, and a source that vanished outside the contracted
path cannot be restored by adding it back, because that produces a *different* source
which does not re-satisfy the stored hop (§1.1's *a hop's source is gone*). The
read that can see the difference is the per-model one the paragraph above rules out, and
that ruling is not what this row asks to reverse — a predicate waiting on N async
failable requests reorganizes the group under the user's hands and stops being hard the
first time one fails. What would close it is one per-row fact in a payload the group is
already drawn from, a liveness count beside `chain_length` or a flag, and
`agent-supply.schema.json` carries neither: `model_supply` rows are `model_id` and
`chain_length` under `additionalProperties: false`, and `supply_status` is one word for
the whole backend. Until then the row is reachable only by expanding the group, which is
a press the user has no reason to make, and this file says so rather than writing a
predicate on evidence it does not have.

**This is D-7 at the grain the payload supports, not a weakening of it** `[derived]`. D-7
protects the row that needs attention, and on this page that is the model no chain can
serve — it is stuck until the user configures something, and 「无来源可供」 is the only row
reading with no self-recovery behind it *that this payload can name*, G-25 above being
the one it cannot. A takeover is the opposite case: §1.7 has it
resolving on its own turn, and §1.1 reserves the violet treatment for exactly the head
blocker that clears itself, so a taken-over row inside a collapse is a row the system is
already handling. Hiding it costs the user nothing they must act on; hiding an unsupplied
model would cost them the one thing. A 直连 group has no `model_supply` at all
`[contract]` and therefore no unsupplied rows to protect, which is why frame 01 draws it
as three rows and a collapse row — this predicate at `mustShow = ∅`.

**`key` is total, it is one field, and the two steps are separate on purpose.**
`backendMenuIndex` is unique within one backend's menu, so no two models tie and `sorted`
is one determinate sequence — every row on the surface, visible or collapsed, has a
position before the collapse predicate runs. *An earlier version ranked an override tier
above it* — `(0 if m.hasOverride else 1, m.backendMenuIndex)` — which S-1 abolished along
with the follow/custom policy that gave the word meaning, and which no payload this page
loads carries: `agent-supply.schema.json` has no such property, and the one `override`
flag that survives lives on `AgentChain`, behind the per-model read 「Chain unresolved」
above. Sorting on it would have made a group's reading order depend on a read the group
does not wait for, so the same payload could hide different rows depending on how many
chain requests had come back — and the tier was doing no work for determinacy anyway,
because `backendMenuIndex` was already unique. Deleting it is the whole fix; a boolean
this file cannot derive is not a tie-break, it is an instruction to guess. Selection is then a *filter*, so expanding stops hiding rows
rather than re-deriving an order: rows the user could already see keep both their
positions relative to each other and their absolute reading order, and the revealed rows
appear where they always belonged.

*An earlier version fused the two steps and imposed an order it never meant to.* It read
`visible = mustShow ++ take(ranked, N)`, which sorts only the collapsible remainder and
concatenates — so every non-nominal row floats above every override, and the group stops
reading as the backend's menu at exactly the moment something is wrong with it. Worse, it
is a different order from the one §1.1's own collapse rule states, and the disagreement
was invisible because the two live in different sections. A concat is not an ordering rule; it is an ordering
rule someone forgot to write.

**`N` is an additive nominal baseline, not a total row floor.** This is the one
number in the file most likely to be mis-implemented, so it is worth saying why it
is additive. The baseline exists to give the group *context* — a few ordinary rows
so the abnormal ones read as exceptions rather than as the whole list. A total floor
destroys exactly that: at three cooling models the context disappears precisely when
it is most needed, and the group renders as if everything were broken.

Consequences, each a test fixture:

| `models` | non-nominal | visible | collapse row |
| --- | --- | --- | --- |
| 12 | 0 | 3 | 「还有 9 个型号」 |
| 12 | 2 | **5** (2 + 3) | 「还有 7 个型号」 |
| 12 | 5 | **8** (5 + 3) | 「还有 4 个型号」 |
| 12 | 12 | 12 | none |
| 3 | 0 | 3 | none |
| 2 | 1 | 2 | none |

- The count in 「还有 N 个型号」 is `|collapsed|`, never `|models| - 3`.
- `|models| <= |mustShow| + N` ⇒ **no collapse row at all**, not an empty one.
- Expanding is idempotent and does not re-rank: it removes the filter, and `sorted`
  was computed over every model in the group before anything was hidden.
- Zero non-nominal models is the frame's own case: 01 draws 3 rows per group plus a
  collapse row `[frame]`, which is this predicate at `mustShow = ∅`.

Other limits `[derived]`:

| Data | Rule |
| --- | --- |
| Long source name | Single line, ellipsis at the card's inner width — `upstream module 384 − border 2 − upContent padding 24 − card padding 24 − tile 34 − gap 10 = 290` at the frame's track width, and derived from the live track everywhere else. `title` attribute carries the full value. |
| Long base URL / masked key | Mono line truncates **from the middle**, keeping scheme+host and the last 4 key chars — the two ends are what identifies it. |
| Long model id | Mono, ellipsis at the `a` column; full value in `title`. |
| Many sources (> 6) | Upstream module grows to the `cols` track height (806) and then `upContent` scrolls; the head and footer stay pinned. Group labels scroll with the content. |
| Many backends (> 3) | `gwContent` scrolls; the rail line keeps spanning the visible track. |
| Zero supply relations | The wire layer renders nothing — no placeholder path. |
| Wires | Generated from the supply-relation set, never hand-placed; the frame's four paths are an instance of that generator, not a fixed asset. |

---

### 1.2 Frame 02 `Q1dkS` — Route-chain editor

**The question it answers:** *for this one model, which sources will be tried, and in
what order?*

**S-1 removed the mode choice this section used to specify.** A configured chain is
stored configuration executed as written; there is no `follow` / `custom` pair, no
segmented control to switch between them, and no second projection derived from a
recommendation. Everything this section previously said about those two modes — the
element inventory, the foot semantics, the metrics, the three inks, the mode copy —
described a frame that no longer exists, and is deleted rather than rewritten.

**The drawing is the authority for what 02 contains, and nothing here is the authority
for what it does** `[contract-gap]` G-32. Frame 02 has been redrawn under S-1 and is
merged; `Q1dkS` in `design.pen` is what it looks like and what it holds. **This section
states no build requirement** — and that is not a way of saying the requirement lives
somewhere else. The editor owns a guarded mutation, and its request sequencing, its
reading of a guarded refusal, its reconciliation of a lost response, its failure copy
and its keyboard behaviour are precisely the class of fact a drawing cannot carry
(§0.2 item 5). None of them is written anywhere.

**So 02 is excluded from what this document specifies, rather than left half-covered**
`[contract-gap]` G-32. The route is in §0.4's table with that reason, the debt is G-32,
and the deliverable is eight specified surfaces plus this pointer; a separate round
writes 02's interaction contract under the same register-and-gate discipline as the
rest. Saying so changes nothing about the build — this section never required anything
— but it changes what a reader may conclude from silence. Without it, an implementer
who opens §0.8 for the chain write finds nothing and cannot tell whether the answer is
「no states are needed」 or 「nobody wrote them」, which is the same ambiguity §0.4
exists to remove for the routes drawn elsewhere.

What still holds, and is stated as a decision rather than as a fact about the
drawing: the chain a user configures is the chain that runs (D-3), and the exits stay
live even when the list they lead to is empty (D-15). Both are §2's, not this
section's, and both survive the exclusion because they are decisions about the chain
rather than statements about the editor.

---

### 1.3 Frame 03 `qZhJ3` — Source-order drawer (per backend)

**The question it answers:** *for one backend, which sources is the gateway willing to
draw on, and in what order does the user keep them?* One ordered list, scoped to one
backend — the list add-time placement writes into, not a value any current policy reads
back out.

**It governs placement, not execution** `[spec]` S-1. A chain is stored configuration
executed exactly as stored, so nothing in this list reaches a turn; what it reaches is
add-time placement, which is where `model-hub.md` §4.6 puts it — 「a visible Gateway
configuration and Add-time placement input」. What it does **not** do is decide an order
at that moment. §4.2's only policy value, `placement-v1`, **appends** a newly added
Source to each configuration-eligible backend order and **appends** every accepted exact
match to that menu model's Route-chain tail, and an append lands in the same place
whatever sequence the list is already in, so the sequence stored here has no reader
`[contract-gap]` G-26. Reordering therefore leaves every existing chain untouched —
including the chains that were built from an earlier state of this very list — and,
while `placement-v1` is the policy, every future one as well. That is the property that
makes the list safe to edit, and it is why this section's copy states what the drawer
stores rather than naming a consumer. §4.2 calls `placement-v1` 「only the current policy
value, not an API, UI, or acceptance invariant」, so a later policy may give the sequence
a reader; what this frame must not do is draw one before the contract has it. Bringing
chains that already exist back in line with the current order is a thing a user may well
want, and it can only ever be an explicit action taken on those chains; no frame draws
one and no route carries it `[contract-gap]` G-13.

The scope in that sentence is the whole point of the frame, and it is what the frame
used to get wrong. An earlier version drew one product-global order with native
sources held out of it; `model-hub.md` §3 defines 来源顺序 as an ordered subset
eligible for **one backend** and never product-global. The owner ruled for the
behaviour spec and the frame was rebuilt. See §0.6 E-1 — kept as a closed conflict
rather than deleted, because the resolution went against the drawing.

**Entry point.** The 来源顺序 button on a backend's group head in the gateway module
— `ehGRK` on Codex, `bGsC7` on OpenCode in frame 01, redrawn as `N50iJ7` / `nzwR3` in
03's own dimmed background `[frame]`. **A backend in 直连 mode has no
such button** — Claude Code's head carries only 切换到网关 `[frame]`. This is not an
omission to be tidied up: a direct backend uses its own login and consults no source
order, so an order editor there would edit a list nothing reads. §2 D-9a states the
rule, and it is a set equality.

**Geometry** `[frame]`

| Element | Metric |
| --- | --- |
| Scrim `UA2Q1` | 1440×1100, `#05050BE0` |
| Drawer `hnsO5` | 460 wide, **full 1100 height**, right-anchored, `$--surface`, left border only |
| `head` `qNs0K` | `padding [18,20]`, vertical, `gap 6`, 82 tall `[frame]` — the only overlay head in the product that is not `[16,20]` / `gap 4`, because it stacks a title over a subtitle |
| Title | 15 / 700, `$--foreground`, + 13px info icon `#FFFFFF59` |
| Close `fUvS9` | 15px, `#FFFFFF59`; `order.cancel` is its accessible name in both locales (§1.0) `[derived]` |
| Subtitle | 11.5 / normal, `#FFFFFF73` |
| `dbody` `Gd4Bl` | `fill_container` height, `padding 20`, `gap 18` `[frame]`, **the sole scroll owner** `[derived]` |
| Section label | 10.5 / 700, `#FFFFFF73` |
| Ordered row | 58 tall, radius 9, `#FFFFFF08`, `gap 12` |
| Held-out row | 58 tall, radius 9, `#FFFFFF05` |
| Grip icon | 14px — `#FFFFFF4D` on ordered rows, `#FFFFFF33` on held-out rows |
| Ordinal badge | 22×22, radius 6; **#1** `#5BFFA01A` / `$--mint`; **#2+** `#FFFFFF0A` / `$--muted` |
| Source name / meta | 12.5 / 600 `$--foreground` over 10.5 / normal `#9BA3B8B3` |
| Type tag | radius 999, 10 / 600 — the provenance palette in §2 D-19 |
| `foot` | `#FFFFFF05`, 1px top border, buttons radius 7, 12 / 600 |

**The ordinal badge inks the first position, and only the first** `[frame]`. Rank 1 is
mint; every later rank is the muted neutral. Mint here is **control ink, not relation
ink** (§1.0): it means *first in this stored order* — a position in the list this drawer
writes, and nothing past it. Not a starting point anything walks, and not a claim about
which source is carrying traffic. The badge moves only when the order is edited
`[derived]`.

**This drawer must not claim live supply, and the reason is the contract, not caution**
`[contract]`. §4.2 makes the order 「a visible Gateway default for Add-time placement,
not a runtime capability filter」, puts 「The only order runtime can execute is the exact
hop order stored for that model」 beside it, and rules out the rest by name: 「No
adapter, UI consumer, refresh path, or runtime resolver may implement or rerun
placement」. So nothing walks this list at request time, and a badge inked as 「supplying
right now」 would be asserting that the per-backend list is the per-model one.

**What does get walked is the other list, and saying which is what keeps this one
honest** `[contract]`. Execution reads a model's own stored chain, whose next turn takes
「the FIRST item with `runnable: true`」 — so skipping a source that is cooling, out of
quota or process-unavailable is real behaviour; it happens one grain down, in a sequence
this drawer never writes. Routing configuration is per backend *and* per model, so two
models on the same backend can hold entirely different chains and neither has to begin
at rank 1. Frame 08 draws exactly that distance: ChatGPT is Codex's first source and is
paused, while aihub answers — because Codex's chain moved down its own hops, not because
anything re-read this drawer. A backend-level surface that inked rank 1 as the live
answer would therefore be wrong on the one frame in this set where the distinction is
visible, and wrong silently — the badge would look confident and describe nobody.

The rule generalises past this drawer: **a surface may only assert a fact whose inputs
it displays.** The per-model current source is owned by the frame that shows models —
01 and 08's 当前 … rows, at per-model grain, including 当前 aihub(接管). This drawer
owns the order and asserts the order. Two surfaces, two claims, one owner each; where
they overlap they would have had to agree, and the way to guarantee agreement is to
stop one of them from making the claim at all.

**Element inventory**

| Element | Displays | Interactive | On activate |
| --- | --- | --- | --- |
| Ordered rows | The order, ranked from 1 | drag, and fully by keyboard | Reorder |
| Grip | Drag affordance | drag / Space | Grab and drop |
| 排进来 `MJZ2I` | Add a held-out source to the order | yes | Append at the end, then focus the moved row `[derived]` |
| 移出 `A2Hz9O` / `FjqVJ` | Take an ordered source out of the order | yes | Move the row to the held-out section, then focus the moved row `[derived]` |
| 取消 / 关闭 `fUvS9` | Leave without saving; the icon is the button's second press and carries `order.cancel` as its name (§1.0) | yes | Close, discarding uncommitted moves |
| 保存顺序 | Commit | yes | Persist, close |

**排进来 and 移出 are one control in two directions, and both are drawn** `[frame]`. The
held-out row carries `MJZ2I`; each ordered row carries the same button reversed —
`A2Hz9O` on `YXG4r`, `FjqVJ` on `m18t4` — in an identical treatment (`#FFFFFF0A`,
`radius 8`, `$--border-strong`, `padding [9,12]`, `gap 6`, label Inter 11.5 / 700).
Membership in this order is a two-way relation, so it gets one affordance with two
labels rather than one affordance and one omission. A drawer whose only way *out* of the
order was drag-and-drop would make the two directions unequal for no reason: one a
button, the other a gesture — and the gesture is the direction with no keyboard
equivalent that produces the same result.

**Shortening the order is not a guarded change, and this drawer starts no confirmation
at all** `[contract]`. The drawer saves the whole order in one request —
`PUT /api/models/agents/<backend>/sources` with `{order: string[]}`, the route this frame
owns — and returns `{agent: AgentSupply}`: the backend's supply read back with the order
it just stored, and no policy state beside it. Taking a source
out of this list removes it from *this list*: a chain that names that source keeps naming
it, because the order is read at add time and a stored chain is executed exactly as
stored (S-1, D-9, and G-13 for the action that would reconcile the two, which no frame
draws). There is nothing for a guard to refuse, so 保存顺序 sends, succeeds, and closes.

*An earlier version of this section had it removing hops.* It read the order save as
「a save that drops sources is not a different kind of change from any other that would
remove hops」, gave §1.3 a Guard-refused state, and promised in the confirm copy that
移出的来源会从这个后端的所有路由链里消失 — while four paragraphs above, this same section
said reordering leaves every existing chain untouched *and called that the property which
makes the list safe to edit*. Both could not be true, and the one that had authority is
the one this file was not free to choose: `model-hub.md` §4.5's Source-mutation envelope
matrix is declared **authoritative and exhaustive** over 「all Source/inventory
mutations, including writes that cannot remove supply」, its eight rows do not include
this route, and FC-12 lists 「the explicit backend Source-order PUT」 as its own item
beside 「every Source/inventory mutation mirrors §4.5's total matrix row-for-row」. An
exhaustive table that omits a route is not a table with a hole in it. So the success echo
`api.md` gives this route, and its 「no policy state exists」, are the matrix being
mirrored correctly rather than a mirror left half-finished, and what G-9 recorded as owed
was never owed — which is why it is now recorded as withdrawn rather than left standing.
The cost of leaving it standing was not a documentation defect: a registered gap reads as
*not built yet*, so the next reader to close it would have taught the server to delete
configured routes on a reorder, and the copy above would have been the warning that made
it look intended.

**There is no mode, and the drawer has no ownership state** `[frame]` `[spec]`. Every
element on this surface is either part of one stored order or an action that edits it:
two sections, the rows in them, and a foot that commits or discards. The order the user
sees is the order a new chain is built from, and the only way it changes is that somebody
changes it here or a new source is placed by the add transaction.
*This section used to specify the opposite, and what it lost is worth naming.* An earlier
version drew a 跟随推荐 / 自定义 segmented control, a three-state ownership machine, a
hint row reading 「顺序已改成「自定义」:新来源不会自动排进来。」 and a 恢复推荐顺序 escape.
All four are deleted. Under the configured-chain ruling (`model-hub.md`, owner 2026-08-09)
there is **no `follow | custom` state and no second projection from a recommendation**, so
a mode switch would have been a pointer standing where a result belongs — the drawer would
have shown the *name of a policy* rather than the order that executes. The escape hatch
went with it, and that is a real loss: 恢复推荐顺序 was a one-press way back from a
reorder the user regretted. What replaced it is not a smaller version of the same thing —
it is nothing, because the honest replacement is *undo the last change*, which this
version does not build. D-10a's one-way-door concern is therefore **not** answered by this
frame; it is answered by the order still being fully editable by hand, which is weaker and
is recorded as such.
*The hint row's job disappeared rather than moving.* It existed because 自定义 froze the
order, so a newly added source could be silently held out and the user had to be told.
Under the add-time placement policy (`model-hub.md` §4.2) every accepted match is written
at a determinate position by the same transaction that adds the source, and the
transaction returns it: `POST /api/models/sources` answers with `added_to`, whose entries
carry `backend`, `menu_model`, `source_id`, `model_id` and `position`, the last one-based
in the persisted Route chain after commit `[contract]`. So 「新来源不会自动排进来」 is no
longer true and the state it warned about no longer exists — the warning was retired
because the condition was, not because the surface moved.

*Nothing renders where it landed, and that is a gap rather than a decision.* A user who
has just added a source needs to know where it went, and the one moment the answer is in
hand is the add transaction's own response — a drawer opened later cannot read it back,
the same one-response lifetime G-20 records for `adopted_by`. **No frame in this document
draws `added_to`** `[contract-gap]` **G-22**. The surface that would own it is the add
flow's terminal, 06 for the source just created — §1.4's *Awaiting sign-in* and §1.5's ②
both land there, and both land holding the array. This file states the landing and not the
rendering, because specifying an element no frame draws would be this document inventing
design (§0.2).

**The held-out section is not an exclusion list.** Its label reads 「未排入这条顺序」
`[frame]`. A source outside this backend's order is still a source: it may sit in another
backend's order, and a chain elsewhere can name it. The earlier design read this section
as 「不参与排序」, which said something much stronger and false. The two sections
partition the eligible sources exactly: no source is in both, and none is in neither.

**Keyboard operation** `[derived]`. Drag-and-drop is the drawn affordance; it is not the
specified one, because a reorder surface that only accepts a pointer is unusable by
keyboard and by assistive tech, and this drawer is the only way to express a preference
the resolver reads. Required bindings, on a focused row:

| Key | Effect |
| --- | --- |
| `Space` | Grab the row, or drop a grabbed row at its current position |
| `↑` / `↓` | Grabbed: move the row one position. Not grabbed: move focus between rows |
| `Escape` | Grabbed: cancel the grab and restore the pre-grab order. Not grabbed: close the drawer |
| `Enter` | On 排进来 / 移出: move that source between the two sections and put focus on the moved row |

Ordinals renumber contiguously from 1 after every move, grabbed state is announced
(`aria-grabbed` plus a live-region message naming the new position), and the order a
keyboard produces is byte-identical to the one a drag produces — they must write the
same value through the same commit path, not two paths that agree today. Every binding in
the table above commits through that one path, and so does every button on a row.

**The subtitle says what the drawer stores, because the sequence has no consumer to
promise** `[contract]` `[contract-gap]` **G-26**. It read 「新建路由链时,从上往下挑第一个
能用的来源」 until this round, and that is two claims the contract does not carry. *First
usable* is a runtime capability filter, and §4.2 says this order is 「a visible Gateway
default for Add-time placement, **not** a runtime capability filter」, with 「the only
order runtime can execute is the exact hop order stored for that model」 beside it — a
resolver that walked this list at request time is the per-backend priority list §9 rules
out by name. *New routing chains* is the placement half, and the only policy value the
contract carries appends at both ends: `placement-v1` puts a newly added Source at the
end of each eligible backend's order and each accepted exact match at the tail of that
menu model's chain, which lands in the same place whatever sequence this drawer saved.
So a reorder writes durable configuration and reaches nothing that reads it — G-13
already records that it cannot reach a chain that exists, and this is the same sentence
about a chain that does not exist yet. The drawer stays, because the order is
contract-owned state with a real write path, real membership rules and a real deletion
transaction behind it; what this file must not do is name a consumer in order to justify
a surface. When a policy value that reads the sequence exists, this subtitle is where it
gets said, and `placement-v1` is 「only the current policy value」 by the contract's own
words.

**Copy** — namespace `models.hub.order.*`

| Key | 中文 | English |
| --- | --- | --- |
| `title` | {{backend}} · 来源顺序 | {{backend}} · Source order |
| `subtitle` | 这个后端存下来的来源顺序。改动它不会改动任何已配置的路由链。 | The Source order this backend stores. Changing it changes no configured Route chain. |
| `section.ordered` | 排在这条顺序里 | In this order |
| `section.ordered.note` | 拖动排序 | Drag to reorder |
| `section.heldOut` | 未排入这条顺序 | Not in this order |
| `action.include` | 排进来 | Add to order |
| `action.exclude` | 移出 | Remove from order |
| `empty.noEligible` `[derived]` | 这个后端还没有可用来源。 | No source is available to this backend yet. |
| `empty.ordered` `[derived]` | 这条顺序现在是空的。把下面的来源排进来。 | This order is empty. Add a source from below. |
| `cancel` | 取消 | Cancel |
| `save` | 保存顺序 | Save order |
| `fail.read` `[derived]` | 来源列表没读到 | The source list could not be read |
| `fail.save` `[derived]` | 顺序没保存上 | The order was not saved |
| `retry` `[derived]` | 重试 | Retry |

**One failed read is not an engine verdict** `[derived]`. This drawer's list came from
one request, and that request failing says the request failed. Sending the drawer to §1.0
Unreachable — as this section read until this round — makes the page behind it declare the
runtime down on that single piece of evidence, and it does so while the page's own runtime
read may be sitting right there saying `ok`. The two readings then disagree on screen, and
the one with less evidence wins. So the failure stays where the evidence is: the drawer
keeps its own row, says what it could not read, and offers the read again. A runtime that
really is down is reported by the read that actually watches it, and 重试 from here will
find that out honestly rather than announce it.

**These three keys are what F1 costs, and 重试 is bought once** `[derived]`. Saving and
Sources unread are both F1 states, and F1 is 「the message is replaced in the slot the
result would have used, the primary becomes 重试」 (§0.8) — so each owes one line, and a
drawer that carried neither would be specifying a treatment it cannot render. The label is
shared because it is the same word in the same place on a surface that never shows both
lines at once: a drawer with no list to reorder has nothing to save. Both lines say what
did not happen and nothing about why. The save sends one array to one route, the drawer keeps
every move the user made, and the frozen rule against enumerating request-level failure
kinds means a 401, a 409 and a timeout are the same sentence here — the same shape
§1.6 gives `fail.tier`.

**The copy says placement, not execution** `[derived]`, and that is not a wording
preference. 保存顺序 stores an order and touches no chain (§0.8), which four paragraphs
above is named as the property that makes this list safe to edit — so a subtitle
promising that the first usable source *answers* describes a rewrite this save does not
perform. A user whose chains have diverged from the current placement would reorder the
list expecting live traffic to move, and nothing would move. The two section labels
carry the same correction: 排在这条顺序里 and 未排入这条顺序 name the thing being edited,
where 排在链里 named a chain this drawer does not write.

**Extreme data** `[derived]`

- **13 rows**: `dbody` scrolls; the head and the foot with its two buttons stay pinned.
  The page behind the scrim does not scroll.
- **Zero eligible sources**: both sections are empty; the drawer shows one line —
  「这个后端还没有可用来源。」 — and 保存顺序 is disabled. The drawer still opens; a
  surface that refuses to open cannot explain why it is empty.
- **Empty order, held-out sources remaining** `[derived]`: the *ordered* section renders
  「这条顺序现在是空的。把下面的来源排进来。」 and the held-out rows stay listed with their
  排进来 buttons; 保存顺序 stays enabled. This is a different emptiness from the one above
  and needs saying, because it is reachable by two routes and the repair is already on
  screen: a user can empty the order by hand with 移出, and a source that stops being
  eligible for this backend leaves both sections, so an order can also empty itself while
  a usable source sits one press away. 保存顺序 is deliberately **not** disabled here: an empty order is a real
  configuration — it means *place no source automatically when a new route is built* —
  and refusing to save it would trap a user who genuinely wants that in a drawer they
  cannot leave without undoing their work. What it does **not** mean is that the backend
  stops being supplied. This save writes an order and rewrites no chain, which is the
  property stated four paragraphs above as the reason this list is safe to edit at all;
  every hop already stored keeps serving, and frame 01 goes on rendering whatever those
  chains resolve to. 没有可用来源 appears when a backend has no runnable hop, which an
  empty order neither causes nor prevents — it decides only what the next 加入 finds
  waiting for it.
- **Exactly one source**: it renders at rank 1 with the mint badge, the grip is present
  but inert, and the drawer is still reachable — the order is trivially satisfied, not
  meaningless.
- **A `needs_action` source already in the order** keeps its rank and shows its cause;
  it is not silently dropped.
- **Long names**: source name truncates at the row width with the full value in
  `title`; the meta line truncates from the middle, keeping both ends.

---

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
| E3a — action still required | Non-terminal `{flow}` in `starting` or `awaiting_action` | The flow exists and is not done. For a paste presentation, these states prove that the provider still needs the held paste value; they do **not** prove that submit was accepted | An ordinary sign-in keeps its owned form/poll. A direct paste submit or D-36 submit reconciliation returns to Awaiting paste-back with the value retained; only a later explicit 提交 resends it | None |
| E3b — submitted, verifying | Non-terminal `{flow}` in `verifying` | The flow exists, the provider accepted the paste value when that presentation owns one, and terminal materialization is still pending | Enter or continue Awaiting paste-back completion under the same bounded poll; never resubmit the held value | None |
| E4 — terminal flow failure | Terminal `failed` / `cancelled`, `flow_expired`, or `flow_not_found` with held `intent: create` | This flow cannot produce a later success terminal; create owns no pre-existing Source subject to reconcile | Stop → OAuth failed | Retry preserves the held intent |
| E5 — terminal success | Terminal `success` envelope | Authorization and materialization completed | Stop | `flow` supplies terminal state and intent; `create` consumes `source`, `added_to`, `adopted_by`; reauth consumes R3's `source`, `recovered`, `interrupted_pairs` |
| E6 — materialization-only failure | Exactly `discovery_failed` or `migration_item_conflict` | This code can arise only after terminal authorization entered materialization, which may already have changed Source state and dependent supply | Stop immediately → OAuth materialization failed | `create` refreshes the source list; `reauth` refreshes M3's complete model surface, including same-backend native siblings, before applying Source gone precedence |
| E7 — reauth Source absence | `source_not_found` while the held intent is `reauth` | The exact repair subject no longer exists | Stop → §1.6 Source gone | Drop the repair overlay; never select a lookalike Source |
| E8 — forgotten reauth flow | `flow_not_found` while the held intent is `reauth` | Only that the flow binding is gone; a completed binding can be forgotten after its exact Source is deleted, so this code proves neither repair failure nor Source presence | Before choosing a failure state, run RR-5's registered attempt-scope read: the complete model surface for native, the exact held Source scope for Hub | Source absent → E7 / §1.6 Source gone; Source present → OAuth failed in front of the reconciled projection; read failure → OAuth failed with a read-only Retry that repeats RR-5 before any producer resend. No retry is offered against a known-absent Source |

E2 is bounded by the dialog clock, not by the first outage. E3a/E3b are deliberately
separate progress evidence: `non-terminal` alone can never authorize completion polling
for a paste value the provider still awaits. E6 is the deliberately closed
exception: treating one of its two members as silence can keep polling after
materialization already cleared a reauth Source's discovered supply. No open-ended
「server-named」 bucket is allowed to claim that transition; adding a third E6 member
requires evidence that the code is exclusive to terminal materialization and a change to
this table, while adding any other named failure requires its own evidence-class row.
The intent-specific failure copy says which journey failed, and the refreshed projection
carries what the server now says about the affected source.

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
`[derived]`. A retry launches the fresh acquisition the user asked for and gives the old
flow to an F4 background owner. Within that owner the order is fixed: settle the
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

Five states are drawn: the left column is the happy path (① default → ② adding →
success destination), the right column is the three failures (③ unreachable /
unauthenticated / wrong address, ④ connected but the interface is undetermined, ⑤
identified but the inventory did not come back). The right column is ordered by how far
the attempt got before it stopped, which is also the order in which the product stops
refusing: ③ and ④ cannot save at all, ⑤ can.

**Element inventory**

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| head sub-line | that Add performs one real connection | static | no | — |
| `f7Ao1U` 名称(可选) | free text | user | yes | — |
| `cXsiv` Base URL + hint | free text; the hint says any relay/aggregator/self-hosted address works | user | yes | — |
| `mZBBw` API Key | masked value, reveal icon | user | yes | Toggle reveal |
| `zVU7c` / `V6CtoF` 拉取型号 + hint | an optional early pull, which Add performs anyway | — | yes | Enter ②′; render its result in place |
| `S0pOY2` 添加 | — | form validity | yes | Run the add action (connect + identify + fetch) |
| `OT0Xf` state ② | spinner, 连接中…, what is happening, 通常 1–3 秒 — **and state ②′ unchanged** | in-flight | 取消 only | Abort; from ②′, back to ① with the form intact |
| `C72yS` state ③ strip | classified cause, then request evidence | probe result | no | — |
| `EJrDH` ③ foot | 取消 / 重试 | — | yes | Dismiss / re-run whatever failed |
| `vKiIo` state ④ strip | connected + authenticated, interface undetermined, with evidence | probe result | no | — |
| `WZyA8` selector | the three interface types, as a **hint to the prober** | static | yes, **nothing pre-selected** | Select one; enables 重试 |
| `Nak7y` ④ foot | 取消 / 重试 (dimmed until a hint is picked) | selection | yes | — |
| `d6bFlX` state ⑤ strip | the interface *was* identified, and the model list did not come back, with evidence | probe result + fetch result | no | — |
| `x0Gzg` ⑤ foot | 取消 / 仍要添加 / 重试 — **three** buttons, the only foot in the product with three | — | yes | Dismiss / save the source without an inventory / re-run the fetch |
| `sqZa9` success note | that the dialog closes straight into 06 | static | no | — |

**Metrics** `[frame]`: dialog 560 wide, height auto in all five states — the frame
sets no fixed height, so a build that pins one is deviating, not matching. Head
`padding [16,20]` `gap 4`; body `padding 20` `gap 14`; field `gap 6`; input 520×36
`radius 8` fill `#FFFFFF08`; field hint 10.5 JetBrains Mono `#9BA3B8B3`. 拉取型号
`padding [8,14]` `gap 6`, neutral. Result strip 520 wide `padding [11,13]` `gap 10`
`radius 9`: red `#FF6B6B14`/`#FF6B6B40` for ③, gold `#FFC85714`/`#FFC85759` for ④
**and for ⑤** (`AFl3g`), mint `#5BFFA014`/`#5BFFA040` for the success note. State ④
selector `padding 3` `gap 3` on `#FFFFFF0A`/`$--border`, **all three segments fill
`#00000000`**. Foot `padding [14,20]` `gap 8` on `#FFFFFF05`, top border; buttons
`padding [8,14]` `gap 6`. State ⑤ (`d6bFlX`, 560×148) is the same dialog shell with
one strip (`uKZuq` 560×87 → `AFl3g` 520×59 → `EbcxN` `triangle-alert` + `LePtp`
title/detail) and a three-button foot (`x0Gzg` 560×61 → `SvK44` 取消, `wouXZ`
仍要添加, `o8K7m` 重试); it carries no field, because ⑤ asks the user for nothing.

**Three of those fills are the design carrying a product rule, not styling.** Every
segment in ④'s selector is transparent — nothing is pre-selected — and ④'s primary
`LrUsk` 重试 is `#5BFFA059`, the same dimmed mint that ② uses for its in-flight
primary and that ⑤'s `o8K7m` 重试 uses. ③'s primary, by contrast, is full `$--mint`.
The rule underneath all four is one sentence: **full mint means the user has already
supplied the new information; dimmed mint means pressing it repeats a request the
user has not changed.** ③ qualifies because fixing the credential field *is* the new
information. ② is mid-flight, ④ without a hint would re-run the identical probe
order, and ⑤'s fetch would re-hit the same endpoint with the same key — in ⑤ the new
information is elapsed time, which the user supplies by waiting rather than by
editing. A build that pre-selects a segment, or that enables ④'s 重试 before a pick,
or that promotes ⑤'s 重试 to full mint, is not deviating cosmetically; it has
implemented the opposite decision.

**States** — §0.8, rows marked §1.5. Two of them are absences worth stating: this
dialog has no Loading state, because nothing is fetched before it opens, and no Empty
state, because a form has none.

**Every state past the form is an observation this dialog has no route to run**
`[contract-gap]` G-18. The operation itself is contracted — AC-26 declares one
non-persisting submission that classifies reachability and authentication and reports a
protocol only a real upstream response proved, and the protocol-observation ruling makes
an unsaved observation load-bearing for every Save that follows it — but no row in
`api.md` accepts it. So the states below stay fully specified, because a contracted
operation that a frame draws is not a state this file may drop; what is registered is the
absence of the route, and §0.5 owns that. Nothing below invents a payload, a status code
or a path for it.

**添加 observes before it persists, and that ordering is what makes ③, ④ and ⑤ offers
rather than notifications** `[contract-gap]` G-18. Each of them asks the user for a
decision — retry, pick a protocol, add anyway — and a decision offered after the row is
already stored is not a decision; ⑤ is the clearest case, because 仍要添加 is only an
offer if nothing was added when the inventory came back unusable. So 添加 runs the same
non-persisting observation 拉取型号 runs, and `POST /api/models/sources` goes out on one
of three paths, all of them past a consent: a clean observation, ④'s retry once a hint
identified the protocol, and ⑤'s 仍要添加. An earlier version had the creation call go
first and the diagnosis come back from it, which needed the persisting route to answer
non-terminally — a response no contract gives it — and left every 重试 in this dialog
re-adding a source that already existed. The seam this draws is the one G-19 registers:
before the POST the transient credential is revoked on every settlement AC-26 names,
and after it the source exists and its questions belong to 06.

**Origin is an axis, not a state, and it is the whole reason this table has primed
twins** `[derived]`. 添加 and 拉取型号 run the *same* probe, so every outcome the probe
can produce is reachable from either button, and the diagnosis it renders — the
classification, its wording, its layout — is identical across a twin. What origin
changes is what the outcome is allowed to *do*, and that is the same three things every
time:

- **重试 repeats the operation that failed, not a different one.** From a pull, every
  retry is another pull.
- **取消 returns to where the operation started, and this is the only place that says
  so.** A pull is an optional operation *inside* the dialog, so its 取消 returns to ① with
  the form's values intact; an add is what the dialog is for, so its 取消 dismisses. This
  holds for the in-flight states exactly as it does for the outcome states: ② dismisses,
  ②′ returns to ①. A cancelled in-flight add has its transient credential revoked
  server-side **for as long as the operation is still the non-persisting submission**
  (`[contract]` AC-26, which requires that revocation on every way the submission can
  settle, cancellation included); what a cancel yields once the add has crossed into
  persistence is `[contract-gap]` G-19, and this file states the guarantee for the phase
  that has one instead of extending it past the seam. **A cancelled pull revokes too**
  `[contract]` AC-26, which names unsaved discovery separately from the connectivity test
  and gives it its own independently provisioned transient ref, the same revocation on
  success, failure, adapter error, timeout and cancellation, and the same durable
  pending-revocation record when the revoke itself fails. The pull is the simpler half of
  that rule rather than an exception to it: it can never persist (below), so it has no
  seam for the guarantee to stop at, and where the add's promise ends at G-19's boundary
  the pull's covers every way it can settle.
- **A pull-origin state can never persist.** Where a state's Add-origin form offers a
  persisting exit, the pull-origin form does not offer it at all: ④′ reports its result
  inline instead of saving, and ⑤′ drops 仍要添加 and keeps the ordinary two-button foot.

Stating it as an axis rather than as a list of special cases is the point, and it is why
the table's Exit column carries no 取消 clause per row: a fact restated once per row is a
fact with eight owners, and the eighth is the one that drifts — which is exactly what had
happened to ②, whose row said 取消 returned to ① while the axis said an add dismisses. An
earlier version primed ③ alone, which left ④ and ⑤ silently shared between the two
origins —
and both of their success paths persist a source and close the dialog. A user who
pressed the optional button, picked a hint, and pressed 重试 would then have created a
source they never asked for, or lost the form to 取消; the same hole existed twice
because the fix had been written as a row rather than as a rule. 拉取型号 is labelled
可选 (D-4), and the promise that word makes is *nothing you do here commits anything* —
which is a property of the whole pull branch, not of one failure.

**②′ is the twin the earlier table was missing, and it needs no new pixels and no new
copy** `[derived]`. The probe a pull runs *is* the probe Add runs, so the period while it
is pending is the same period, and it renders the same `OT0Xf` body: spinner, 连接中…,
连上 + 认出接口 + 首次拉取型号列表 · 通常 1–3 秒, 取消 only. Every word of that sentence
is true of a pull, which is why the twin reuses `addKey.adding` and `addKey.adding.detail`
rather than earning keys of its own. The axis applies here as everywhere — 取消 lands on
① instead of dismissing, **and it revokes on the way out** `[contract]` AC-26. Nothing is
persisted, and that is a fact about the *source*, not about the credential: the pull ran
against an independently provisioned transient ref, so a cancel here is one of the five
ways AC-26 names and carries the same revocation and the same durable pending-revocation
record when the revoke itself fails. 可选 promises that nothing you do here commits
anything; it does not promise that nothing was provisioned. The third property has
nothing to bind to: an in-flight state offers no 重试.

Two consequences follow from ②′ occupying the body rather than sitting inside the form.
**A second pull cannot be started while one is in flight**, because 拉取型号 is not on
screen to press — the mechanism is absence, not a disabled button, which is why an
enumeration of this dialog's interactive elements does not and should not list it. And **取消 during ②′ is a real abort**, not
a dismissal: it stops the in-flight probe and returns the user to a form still holding
everything they typed. A build that leaves ① fully interactive during a pull has to
invent an answer for what a second press means and for which of two responses wins —
questions this dialog never has to ask.

*Postmortem.* The origin axis was stated for outcomes and quietly not for the in-flight
period, so 拉取型号 appeared to move from ① straight to a result. The rule that catches
this class: **an axis has to be applied to every state on the other side of it, including
the ones that are pure duration.** A state that only exists while something is pending is
the easiest one to forget, because nobody screenshots it and no static frame draws it
twice.

**The distinction has no visual carrier**, and that is what makes it worth stating so
precisely: the only way to get it right is to keep the origin in state, and the only way
to get it wrong is to reconstruct it from what is on screen. The property is not about
one pair: **no Pull-origin state persists anything**, and that quantifies over every
primed twin, ②′ included.

**A pull that succeeds lands in a state, not back in ①** `[derived]`. ②′ named ③′ / ④′ /
⑤′ for its three failures and, for the one outcome the user pressed 拉取型号 to get, said
only 「the inline model count in ①」 — a result reported by the state the register
describes as 「Dialog opened」. ①′ is that state written down: the form exactly as ①
renders it, plus `addKey.pull.result` reporting what came back, or `addKey.pull.empty`
when the source answered and listed nothing — the reachable zero this key has, and the
reason it takes a string rather than 「拉到 0 个型号」. It is also where ④′ lands when
the hint identifies the interface, and where ⑤′'s 重试 lands when the re-run fetch
comes back usable: the same result by a longer road, which is the point of naming it once. Three
properties hold here because it is a Pull-origin state and for no other reason. Nothing
was persisted, so 添加 from here still runs its own observation (G-18) and reuses none of
this. Editing Base URL or API Key drops the report, because a count is a fact about the
address it was fetched from. And 取消 dismisses with nothing to abort — the in-flight
abort belongs to ②′, which is where the request still is. A build without this row has to
decide all three from what is on screen, which is the reconstruction the paragraph above
names as the only way to get the origin wrong.

**④'s selector is a hint to the prober, not a declaration of the answer** `[frame]`
`[contract]`. The drawn hint is explicit — 「提示只改探测顺序 · 仍要真的连上才会保存;
保存后不可更改」 — and it settles three things at once that earlier revisions of this
file got wrong in three different ways:

- **The pick does not persist by itself.** It reorders the probe sequence for the next
  attempt. Identification still has to come from a real upstream response, which is
  AC-27's requirement, reached here by agreement rather than by exception.
- **The primary is 重试, not a save.** A build that persists the picked protocol
  directly has removed the verification the same screen promises.
- **Repeated failures must not accumulate silently**: the strip shows the latest
  attempt's evidence, for the order that was tried `[derived]`.

`[derived]` for ④'s entry gate: until a segment is chosen, 重试 stays in the dimmed
treatment. Retrying with no hint would re-run the identical probe order that just
failed, and a button that is guaranteed to reproduce the current screen is worse than
no button. (D-3.)

**This is the one screen in the product where the user supplies a fact the product
normally derives, and the frame goes out of its way to bound it** — one hint, affecting
one attempt, not stored as an answer. 「全产品唯一一处让你提示接口类型的地方」 is the
frame's own caption. At `ca45aeb6` the ledger
uses the frame's own word for it: AC-27 calls the control 「a one-time three-value
**probe-order hint**」 and FC-07 contracts it as 「a manual three-value probe-order hint」
that 「cannot save a protocol without response proof」. The frame and the contract now
say the same sentence, which is the ideal end state for a `[frame]` `[contract]` pair —
neither is quoting the other, and they agree anyway.

**State ⑤ is the one place in the product that saves something it could not fully
verify, and the rule that makes it safe is a property, not a permission** `[frame]`
`[contract]`. E-5 was raised because no frame drew this state; it is now drawn
(`d6bFlX`), and the ruling that closed it states the invariant the whole dialog
enforces:

> 已保存的来源恒有一个被观测证明过的协议;凡拿不到证明的路径,产物都是「没有添加成功」。

Read that as a partition of the four ways an add can end, and every button on this
screen falls out of it. ③ and ④ are the paths where the protocol was **not** proved,
so both refuse — that is E-3's gate, and ⑤ does not reopen it, because ⑤ is
downstream of the gate rather than around it. ⑤ is the path where the protocol
*was* proved by a real response and a *different* result — the model inventory —
did not arrive. AC-27 at `ca45aeb6` puts the same thing from the contract's side:
「『Add anyway』 is available only after the protocol was proved and a different
result, such as model inventory, remains unavailable; that uncertainty is a health
fact.」 An unknown inventory is a fact a saved source can carry; an unknown protocol
is not, because every later request depends on it.

That is why 仍要添加 exists here and nowhere else, and the frame says so in its own
caption: 「全产品唯一一处「仍要添加」」. It is also why the state carries **no field**.
④ asks the user for a hint because the product is missing something the user might
know; ⑤ asks for nothing, because the user cannot supply a model list. The only
question ⑤ puts to a person is whether to keep the connection they just proved.

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
that persist a source equals the set whose protocol came from an observed response. A
build that lets ③ or ④ save, and a build that refuses ⑤, break the same equality from
opposite sides — which is why it is worth stating as one.

**The exits that persist are specified and the body they send is not** `[contract-gap]`
**G-27**. `api.md`'s route table gives `POST /api/models/sources` the body `SourceCreate`
and answers `{source, added_to, adopted_by}`; the answer is a schema and the request is
a name that occurs in no other line of the contract. The response entity cannot be
read backwards into one — `source.schema.json` is `additionalProperties: false` over
sixteen properties, four of them server-assigned (`id`, `created_at`, `state`, `usage`),
so a body derived from it is wrong on the fields the server is the one to decide. Two of
the request's terms are not in the entity at all. The first is the credential: `Source`
carries `credential_ref` and `masked_credential`, while what this dialog holds is a
plaintext key that AC-26 routes through a transient provisioned ref — the same seam G-19
already stops at, arriving here as *what the persisting call is handed*. The second is
the distinction ④ and ⑤ exist to draw: both persist a protocol proved by a real
response, ④ with the inventory the hinted probe returned and ⑤ with an empty one it
observed, and 「identified, nothing in it」 against 「identified, these models」 is the
equality above written as a request body. So this file specifies **when** each exit
persists and does not specify what it sends; the three exits carry the marker in §0.8
where a reader meets them, and §1.4's `create` terminal answers with the same three
arrays from the branch that never sees a `SourceCreate` at all.

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
| `subtitle` | 「添加」时自动连一次:认出接口 + 拉取型号列表 | Add connects once: it identifies the interface and fetches the model list |
| `field.name` | 名称(可选) | Name (optional) |
| `field.baseUrl` | Base URL | Base URL |
| `field.baseUrl.hint` | 粘贴任何中转 / 聚合 / 自建服务的地址即可,Avibe 会自己认出接口 | Paste any relay, aggregator or self-hosted address — Avibe identifies the interface itself |
| `field.apiKey` | API Key | API key |
| `field.apiKey.reveal` | 显示 API Key | Show API key |
| `field.apiKey.conceal` | 隐藏 API Key | Hide API key |
| `test` | 拉取型号 | Fetch models |
| `test.hint` | 可选 ·「添加」时会自动拉一次 | Optional · Add fetches once anyway |
| `pull.result_one` | 拉到 {{count}} 个型号 | Fetched {{count}} model |
| `pull.result_other` | 拉到 {{count}} 个型号 | Fetched {{count}} models |
| `pull.empty` | 连上了,但这个来源没有可用型号 | Connected, but this source lists no models |
| `submit` | 添加 | Add |
| `adding` | 连接中… | Connecting… |
| `adding.detail` | 连上 + 认出接口 + 首次拉取型号列表 · 通常 1–3 秒 | Connect, identify the interface, fetch the model list · usually 1–3s |
| `fail.subtitle` | 认出接口是「添加」的前置条件 · 先按下面这条修,再重试 | Identifying the interface is a precondition of Add · fix what the line below names, then retry |
| `fail.auth` | 鉴权失败:401 Unauthorized | Authentication failed: 401 Unauthorized |
| `fail.auth.detail` | 检查 API Key 是否有效 | Check whether the API key is valid |
| `fail.address` `[derived]` | 地址不对:404 Not Found | Wrong address: 404 Not Found |
| `fail.network` `[derived]` | 网络不通:连接超时 | Network unreachable: connection timed out |
| `fail.unclassified` `[derived]` | 没连成,原因认不出来 | It did not connect, and the cause could not be identified |
| `fail.engineDown` `[derived]` | 网关没有响应,请重试 | The gateway is not responding — try again |
| `fail.save` `[derived]` | 没能确认这个来源已经保存 | The source is not confirmed saved |
| `retry` | 重试 | Retry |
| `undetermined.title` | 连上了、也通过了鉴权 —— 但认不出它说哪种接口 | Connected and authenticated — but we cannot tell which interface it speaks |
| `undetermined.detail` | {{request}} · {{status}} · 返回结构对不上任何一种已知接口 | {{request}} · {{status}} · the response shape matches no interface we know |
| `undetermined.label` | 接口类型 · 这一次由你提示 | Interface type · you hint it this once |
| `undetermined.hint` | 提示只改探测顺序 · 仍要真的连上才会保存;保存后不可更改 | The hint only reorders the probe · it still has to really connect before anything is saved, and it cannot be changed afterwards |
| `protocol.anthropicMessages` | Anthropic Messages | Anthropic Messages |
| `protocol.openaiResponses` | OpenAI Responses | OpenAI Responses |
| `protocol.openaiChatCompletions` | OpenAI Chat Completions | OpenAI Chat Completions |
| `inventory.title` | 接口已经认出来了 —— 但这次没拿到它的型号清单 | The interface is identified — but its model list did not come back this time |
| `inventory.detail` | {{protocol}} 已认出 · {{request}} · {{status}} · {{reason}} | {{protocol}} identified · {{request}} · {{status}} · {{reason}} |
| `inventory.reason.transport` `[derived]` | 没能连上来源 | The source could not be reached |
| `inventory.reason.rateLimited` | 上游限流,清单没返回 | Upstream rate limit — the list was not returned |
| `inventory.reason.unknown` `[derived]` | 清单没能读出来 | The list could not be read |
| `addAnyway` | 仍要添加 | Add anyway |
| `success.title` | 成功 → 弹窗关闭,直接进入「来源详情 · 型号管理」 | Done → the dialog closes and you land on Source details · Models |
| `success.detail` | 型号列表、重新拉取、推理强度档位都在那里维护 | The model list, refetch and reasoning tiers are all maintained there |
| `cancel` | 取消 | Cancel |

**`inventory.reason` has a third word for the same reason `{{reason}}` is always
present** `[derived]`. ⑤ is entered whenever the model fetch 「came back unusable」, and
rate limiting and a transport failure are two readings of that, not both of them: a 500,
a body that parses to nothing, a 200 with a shape the protocol does not admit all land
here too. With only the first two the frame either interpolates the upstream's own
sentence — untranslated, and the thing §0.9 forbids — or reports a rate limit that never
happened. 清单没能读出来 says exactly what ⑤'s title already says and claims nothing
more, which is what a residual reading is for.

**`fail.subtitle` is shared by every cause, which is why it names none of them**
`[derived]`. State ③ renders exactly one of `fail.auth` / `fail.address` /
`fail.network` / `fail.unclassified` underneath it, and the first three are a 401, a 404
and a timeout — only one of them is about a credential. A subtitle that names the
credential is right one time in four and actively misleading the rest: it sends a user
with a typo in the base URL off to regenerate a key that was never the problem. It points
at whichever line the classification produced instead, which is the same thing
`fail.auth.detail` does one level down — the specific advice lives with the specific
cause.

**`fail.unclassified` is the fourth line for the same reason `inventory.reason` has a
third word** `[derived]`. AC-26 settles an API-key test on five outcomes — success,
authentication failure, **adapter error**, timeout and cancellation — and ③'s three
classified lines cover two of them. The adapter error is the contract's own name for an
upstream that answered with something the adapter could not use, and it is neither a 401,
a 404 nor a timeout; with only three lines a build either files it under one of those,
which reports a cause that was never observed, or renders ③ with an empty cause slot on a
frame whose subtitle promises one. 没连成,原因认不出来 says exactly what ③'s subtitle
already says and claims nothing more, which is what a residual reading is for. The exit
is unchanged, because 重试 re-runs the whole observation and never resumes from a
classification.

The three protocol strings above are the **only** protocol names anywhere in the
product surface. They are identifiers, identical in both locales, and they
are exactly the three transports the protocol enum admits `[contract]` AC-28 — the
label 「OpenAI Chat Completions」 maps to `openai_chat`.

`inventory.detail` opens with `{{protocol}} 已认出` for a reason worth stating: the
one fact that distinguishes ⑤ from ④ — and therefore the one fact that licenses
仍要添加 — is that the interface *is* known, so the line leads with it rather than
with the failure. `{{reason}}` is a classified cause in the same style as ③'s three,
and `inventory.reason.rateLimited` is the instance the frame draws. `[derived]`: the
key is open, because the fetch can fail for reasons the probe already ruled out for
the connection itself; whatever set an implementation ends up with, each member has
to be a plain sentence about the upstream, not a status code on its own — `{{status}}`
is already in the line.

**`undetermined.hint` used to contradict AC-27, and now states it.** The string the
frame drew previously — 「选一种才会保存 · 之后可在来源详情里改」 — promised the choice
was changeable later. At `ca45aeb6`, AC-27 says the opposite: after Save the stored
protocol is preserved byte-for-byte through retest, discovery, refresh, credential and
Base-URL replacement and restart, and 「changing protocol requires a new Source」; FC-12
`PATCH /api/models/sources/<source_id>` `[contract]` carries metadata only — frame 11
now registers that editor in §1.10 — and has no protocol field at all, so there is no request that could
change one. The rebuilt string
ends 「保存后不可更改」 — the frame moved onto the contract's side. **E-2 is closed, and this string is now its whole surface.** The other
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
field is blank when 添加 is pressed, the client sets `display_name` to the URL host
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
| `sugad` source bar | 36×36 identity tile, source name, **state dot + state label** (使用中 in the drawn state) + 型号列表更新于 {{time}}, mono `host · N 个型号` | source state `[spec §4.5]` | capability-gated 重新拉取 / 添加模型 / source overflow `[frame 11]` | Refetch / append an editable row / open 编辑来源 · 移除来源 |
| 重新拉取 action capability `[derived]` | `sourceDetail.action.refetch` | `Source.supply_channel` | render for `hub`; do not render for `native_cli` | A Hub Source → Refetching. A native Source has no stored credential for this route to validate, so it exposes no activation |
| `myA8k` header | 型号 ID (250) · 录入 (84) · 推理强度 (470, with info) · fill spacer | static | no | — |
| `OM5PH` row | model id, entry-kind pill, tier chips, overflow icon | one model | tiers, overflow | Edit tiers / row menu |
| `p2JwTz` tiers | chips, or 未设置档位 + `+ 添加档位` | `reasoning_efforts[]` `[contract]` FC-03 | yes | Enter edit mode |
| `eVavA` tiers (editing) | removable chips + text input + 回车添加 · 任意文本 | local edit → `PATCH /api/models/sources/<source_id>/models/<model_id>` `[contract]` | yes | Add / remove a tier |
| `nN4TZ` manual row | editable id input, 手动添加 pill, tier affordance, 取消 / 添加 | local draft → `POST /api/models/sources/<source_id>/models` `[contract]` | yes | Commit or discard |
| `Q83BF` add row | 添加模型 + when to use it | — | yes | Append a manual draft row |
| `tF3Bh` footnote | scope of this page; that tiers are yours to type; that the interface type is identified at add time, fixed, and neither shown nor editable here | static | no | — |

**The back icon is named rather than inferred** `[derived]`. `iGcAi` is this page's only
route back to 01 and draws no text, so `sourceDetail.back` carries its accessible name in
both locales — the same treatment `field.apiKey.reveal` gets one frame over and
`shell.gatewayInfo.label` gets on 01, for the same reason: a glyph is not a name, and an
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

**What a row's overflow menu offers is derived, and this file writes the derivation
rather than a roster** `[derived]` `[contract]`. The ruled routes give a Source's models
one delete — `DELETE /api/models/sources/<source_id>/models/<model_id>`, guarded — and it
reaches an entry the user wrote. The reason it stops there is a property of the entry, not
a permission table: a discovered entry exists because the upstream reported it, so
removing it is not a decision the user is in a position to make, and the next successful
重新拉取 would report it again. An entry the user typed has no author but the user, so it
is theirs to withdraw. That is the whole rule, and it is deliberately stated as a rule:
a list of which entry kinds carry which controls would be a second owner of a fact the
row already displays in its 录入 column, and the two drift the first time anything is
added to either. So the menu carries `sourceDetail.row.remove` exactly where the user
authored the entry, and
on every other row it carries no removal item at all — not a disabled one, which would
advertise an operation that can never become available (D-9a).

That derivation is also what makes **G-3** a gap in the record rather than a missing
control on this frame `[contract-gap]`, and the frame agrees: `Qp6FI`'s subtitle reads
「aihub · 只有手动添加的型号能移除」. What the gap is about is the *other* half — a user
who wants a discovered model gone. The refetch rule two paragraphs down is a diff, not a
replacement, so a removal that writes nothing durable is undone by the next successful
重新拉取, and the user watches a row they deleted come back. Closing that means a
retention marker on the discovered-model record, and §0.5 records why three further rules
would have to come with it. No rule in this file requires the control.

**Dropping the control is only not a net loss with one property attached, so the property
is stated here** `[derived]`. Without a per-model retirement, a source that broadcasts a
large catalogue leaves every one of its models on this page. What has to hold is about
reach, not about controls:

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

**The position pill is drawn and the refusal carries no position** `[contract-gap]`
**G-28**. `guard.hop.position` renders 顺序 #{{n}}, frame 11's
`guard.hop.position.removeSource` renders 第 {{n}} 跳, and §0.9 has `{{n}}` always present,
while §4.5's refusal names each hop as `(backend, menu_model, source_id, model_id)` —
four fields and no index among them. The array being ordered is not the answer: it spans
every backend and menu model the change touches and lists only the hops being removed,
so an entry's place in it is not that hop's place in its chain, and reading the index off
it would produce a number that looks right and is wrong exactly when the chain is long
enough for the number to matter. The one read that carries a position is the per-model
chain read, which means one request per distinct `(backend, menu_model)` the refusal
names, issued from inside a confirm the user is already waiting on, each async and each
allowed to fail — the dependence §1.1 refuses for a predicate that only decides what to
hide, arriving here on the surface that has to be right the first time. So each row
renders its model line and its consequence, and **renders no pill until the reference
carries a position**. The key stays in the register because copy is this document's
register and the frame draws the pill; this is G-23 seen from the other side — there the
strings exist and the element does not, here the element exists and the value does not.

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
保存顺序 turns it into anything a guard could refuse. A guard hung on the click would fire
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
| `hint.removeSource` `[frame]` | 一个事务:来源从每个后端的顺序与所有路由链一起移除;空掉的链保留并标「中断」,其余跳顺序不变。 | One transaction removes the source from every backend order and every route chain. Empty chains remain and are marked interrupted; all other hop order stays unchanged. |
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
| `active`, and some backend adopts it `[contract-gap]` G-20 | `$--mint` | `sourceDetail.status.inUse` |
| `active`, adopted by nothing `[contract-gap]` G-20 | `$--muted` | `upstream.state.standby` |
| `standby` | `$--muted` | `upstream.state.standby` |
| `cooldown` | `$--gold` | `upstream.state.unavailableRetry`, or `upstream.state.unavailableDue` once `retry_at` has passed `[derived]` |
| `needs_action` | `#FF6B6B` | the `sourceDetail.status.needsAction.*` row `state.detail_key` selects `[derived]` `[contract]` |
| `error` | `#FF6B6B` | `sourceDetail.status.error` `[derived]` |

The split of `active` is the one place this bar reads a second field: 使用中 claims a backend has
this source configured into a route, which is `adopted_by` (§1.0), not a source state. A
source can be perfectly healthy and be in nobody's chain, and saying 使用中 there would be
the same lie as saying it about a dead credential, in the flattering direction.

What `adopted_by` does **not** answer is whether traffic is flowing through it at this
instant. `api.md` calls it 「the stable Source-card projection of persisted references」,
and stable is the operative word: a reference persists across a cooldown, a revoked
credential and a takeover that routes past this hop entirely. So 使用中 is a statement
about configuration, and the copy is written to be true of a standby hop that a chain
still points at. A card that promised live supply would need the per-chain runnability
read, which is a different projection D-28 keeps separate and G-20 does not deliver here
anyway — and it would go stale the moment a hop it named entered cooldown. `standby` and unadopted `active`
land on one word deliberately — they differ in *why* nothing is drawing from the source,
and this bar is not where that difference is actionable.

**And the second field is one only a creation response carries** `[contract-gap]` G-20.
This bar is the projection's second consumer, and the reading it gets is the one §1.0
already stated for the first: `adopted_by` is absent from `GET /api/models/sources` and
from `source.schema.json`, so on any load that is not the creation terminal the split
above cannot be made. It is then not made. The bar falls back to the vocabulary
`model-hub.md` §4.5 states for the field it does have — `active` reads 使用中, which is
that table's own word for it — and the finer distinction appears only when a response
carried the array. Neither work-around §1.0 rejects is available here either: chains may
not be read backwards into attribution (D-28), and a creation array held in client state
would make this page confidently wrong the moment a chain was edited elsewhere. What this
bar must not do is invent a third reading of its own, which is the whole reason the fact
is registered once and cited twice rather than ruled twice.

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
  all survivors and keeping an empty route configured」, reporting every resulting gap —
  `force` is confirmation, not a claim that the change is interruption-free.

  **This is not the chain changing by itself; it is the only way it changes at all.** No
  path in this product removes a hop without a `409` that names each one and a confirm the
  user answers — that is what S-1 means by the chain being the user's, and §4.6 says the
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
  标出,不得静默跳过。** §4.4's `model_supply` projects `chain_length: 0` into 「无来源可供」
  on the menu, and that projection is reached both by the retained-hop path and by a
  confirmed cascade that empties a route — an emptied route stays an explicit empty
  configuration, so the menu still has something to mark. **No new field, no new mechanism,
  and no stale row on this frame.** The marker renders on the model menu, which no frame in
  this document draws, so **this file states nothing about how it renders** — doing so would
  specify a surface no frame here draws. It is covered by the AC ledger instead.

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
  `/models`, and the price is that the explanation is assembled rather than read. §0.5
  records the ruling; G-3's retirement half stays open and is unaffected by it.
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
| `edit.hint` `[derived]` `[contract]` | 协议已由真实响应证明,不在这里改;改显示名称不会影响型号与路由链。改地址时会先检查供给影响。 | The protocol was proved by a real response and cannot be changed here. Renaming does not affect models or routes; changing the address first checks its supply impact. |
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
| `tiers.empty` | 未设置档位 | No tiers set |
| `tiers.addFirst` | + 添加档位 | + Add tier |
| `tiers.add` | + 档位 | + Tier |
| `tiers.inputHint` | 回车添加 · 任意文本 | Enter to add · any text |
| `addRow.hint` | 拉取不到、或只想接入其中一个时用 | Use this when a model is not discoverable, or when you only want one of them |
| `empty` `[derived]` | 这个来源没有返回型号。可以手动添加,或重新拉取。 | This source returned no models. Add one by hand, or refetch. |
| `emptyNeverFetched` `[derived]` | 还没有成功拉取过这个来源的型号列表。可以按上面状态里写的那条先处理,再拉一次,或者手动添加一个型号。 | This source's model list has never come back. Deal with whatever the status above reports, fetch again, or add a model by hand. |
| `footnote` | 这里只管「这个来源有哪些型号」。型号走哪条路由链,在网关模块里改。档位自己填,两种录入方式都一样。接口类型在添加时认出并固定,页面上不显示、也不能改。 | This page answers only "which models does this source have". Which routing chain a model takes is set in the gateway module. Tiers are yours to type, the same for both entry kinds. The interface type is identified when the source is added and fixed there — it is neither shown on this page nor editable. |

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

**This page shows no protocol at all, and that is the resolution of E-2** `[frame]`.
Earlier revisions of this section specified a quiet badge next to the source name —
「接口由你指定」 — with a tooltip naming the stored protocol and a 「改为…」 action
behind it. Both are gone, and the frame no longer draws either. The ruling took the
subtractive branch of the conflict: the stored shape at `ca45aeb6` carries no
manual/automatic provenance marker (AC-27), so a badge conditioned on 「由你指定」 has
nothing to render *from*; and 「changing protocol requires a new Source」, so an edit
affordance would advertise an operation the API cannot perform: `PATCH
/api/models/sources/<source_id>` `[contract]` has no protocol field to send it in.

What replaced both is one clause in the footnote: 「接口类型在添加时认出并固定,页面上
不显示、也不能改。」 That is a better outcome than either half of the original design,
and the reason generalizes past this frame. A badge that renders on *some* sources
teaches every user that protocols are a thing they may have to think about, in exchange
for a fact that changes nothing they can do — which is the mechanism D-8 spends its
whole argument hiding. Stating the rule once, in the place where a user might go
looking for the control, costs one sentence and leaves nothing conditional to
implement. The 「type is fixed」 half of the rule is also stated at the only moment it
can still be acted on, in 05 state ④'s hint (§1.5).

Consequently no `interfaceBadge*` copy key exists, no `UI-n` quantifies over a badge,
and D-8's 「the user does not perceive the supply mechanism」 now holds on frame 06
without an exception clause.

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
unavailable *for a recoverable quota/cooldown reason*; violet says 临时改走, and
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

**Display condition** `[derived]`: every backend is in 直连 mode and no source exists.
Both terms are read from current state, and that is deliberate — **this is a repeatable
empty state, not a first-run screen.** The return path is contracted end to end: AC-31's
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
moment one backend switches, the page becomes 01. Specifying it as a route would create
a second address that has to be kept in sync with the first and that users can reach
after it stops being true.

Stated as the rule an implementation has to encode, it is a switch on **two** terms —
*does any backend run through the gateway*, and *does any source exist* — because
`undo.3` (§1.9) promises that switching the last backend back to 直连 keeps the sources
the user added. That promise makes 「all direct, sources non-empty」 a reachable state,
and it is the one state where the two terms disagree.

| Any backend on the gateway | Any source exists | Page title | Tab strip | Body |
| --- | --- | --- | --- | --- |
| **No** | **no** | `oPD53` 「模型」 | **absent** | this frame, and it occupies the whole page |
| **No** | **yes** | 「模型」 (`YkN0P`) | present `[frame]` | 01, every gateway group in its 直连 form |
| **Yes** | either | 「模型」 (`YkN0P` on 01, `VaXos` on 08) | present: 「来源与网关」 · 「用量与额度」 `[frame]` | 01 — this frame is gone as a page |

**The middle row needs no new frame, and this section's own reasoning is what settles
it** `[derived]`. 09 is 01 「in the state where nothing is adopted」, and in the middle
row something is: the sources are still there. That alone would leave the choice open,
so the frame decides it — 09 draws no upstream column, so rendering it here would hide
sources the product just promised to keep, leaving no surface to inspect or delete them
on. So the
page is 01, and every element of it is already specified: the upstream module lists the
retained sources (§1.1), each backend group renders in the 直连 form with 切换到网关 on
its header (`g3Wh0P`) exactly as a partially-adopted page renders its still-direct
groups, and the wire layer draws nothing because there are no supply relations. The run
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

**Two things about that switch are easy to get wrong, and the frames settle both.**
First, the tab strip is *not* chrome that is always there with an empty second tab: 09
draws no `KB3N9` / `ag5OQ` pair at all, because 用量与额度 has nothing to report when
nothing has ever been supplied, and a tab that opens on an empty page is worse than a
tab that does not exist. Second, this frame does not survive as a block inside 01 — but
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

**What the shell drops, and why** `[frame]`. Frame 09 renders the header but **no tab
strip, no three-column `cols` track, no dispatch rail, no wire layer and no legend.**
There is no gateway module to occupy the second column, no supply relations to draw, and
therefore no inks to explain. An empty gateway column with a placeholder would be worse
than its absence: it would assert that a thing exists here and is currently broken,
which is the opposite of the truth.

**The page and the module have different names, and neither is 「模型网关」** `[frame]`.
Measured across the original full-page set: the page title is 「模型」 (`oPD53` here, `YkN0P` on 01,
`VaXos` on 08, and so on), the first tab — the module this document specifies — is
「来源与网关」, and the second is 「用量与额度」. The string 「模型网关」 is **not rendered
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
| Backend row ×3 | Name, 直连 pill, which login it uses | 切换到网关: yes | Open frame 10's confirm for **that backend** |
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

**At zero installed backends the pill does not render, and that absence is what gives
`shell.allDirect` its branch guarantee** `[derived]` `[contract-gap]` G-11. The rows come
from `GET /api/models/agents` → `{agents: AgentSupply[]}` (`api.md`), and the array
carries no `minItems` in `agent-supply.schema.json`, so an empty one is a *legal*
response, not a malformed one. Legal is not the same as reachable, and G-11 is the
distance between them: no property of `AgentSupply` reports whether a backend's CLI is
present, and the array is built from a literal three-backend tuple, so the payload is
length 3 whatever the machine has and an empty one is a response nothing can currently
send. **What follows is specified against the payload closing G-11 would produce, not
against one this build emits** — the reading is inferred from the array's length only
because the gap leaves no flag to read, and it is marked so a reader cannot mistake the
branch for reachable behavior. The
extreme-data rule below omits an uninstalled backend from the list and derives
`{{count}}` from the rows rendered; at zero rows those two rules together select
`shell.allDirect_other` at `count = 0`, and the pill says 「0 个后端都在直连」 / 「All 0
backends are direct」. That sentence is not merely awkward, it is false in the way that
matters: it reports a configuration where there is nothing to configure.

So the page branches once more, and it branches **before** the pill:

| Backends installed | Header pill | Body |
| --- | --- | --- |
| **0** | absent `[derived]` `[contract-gap]` G-11 | Empty state: `empty.title` / `empty.body`, and neither card renders |
| **≥1** | `shell.allDirect`, count = rows rendered | This frame as drawn |

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
- **One backend already on the gateway** ends this frame's display condition; the page
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
| 切换到网关 | Commit | yes | Switch **this backend only**; the page becomes 01 |
| Failure strip `[derived]` | `fail.title` over `fail.detail`, in the Failed state only | no | — |

**The dialog names the exit by location, not by promise** `[frame]`. The second
可以撤回 bullet reads 「回退入口:这一页的 Claude Code 卡片 → 切换到直连」. "You can
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
| `effects.1` `[contract-gap]` G-14 | 你现在的 {{vendor}} 登录成为第一个来源,继续优先使用 | Your current {{vendor}} login becomes the first source and keeps priority |
| `effects.2` | 等你添加了别的来源,这个登录用满时会自动换过去 | Once you add other sources, it moves to them when this login is exhausted |
| `effects.1.opencode` `[derived]` | OpenCode 自己的模型配置原样保留,这次切换不改动它 | OpenCode's own model configuration is kept as it is; this switch does not change it |
| `effects.2.opencode` `[derived]` | 它的型号从此由这一页上的来源供给;还没有来源时,先添加一个 | Its models are supplied from the sources on this page; if there are none yet, add one first |
| `effects.3` | 型号菜单不变 | The model menu does not change |
| `effects.4` | 正在进行的对话不受影响,下一次请求开始生效 | Conversations in progress are unaffected; the change applies from the next request |
| `section.undo` | 可以撤回 | You can undo this |
| `undo.1` | 随时可以切换回直连 | You can switch back to direct at any time |
| `undo.2` | 回退入口:这一页的 {{backend}} 卡片 → 切换到直连 | Where to undo: the {{backend}} card on this page → Switch to direct |
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
Every backend row on 09 opens this same confirm, so its copy has to be true for all three.
`effects.1` / `effects.2` are true for Claude Code and Codex because each has exactly one
sanctioned CLI login that becomes that backend's singleton `native_cli` Source
`[contract]`. OpenCode has no such login — 09's own row says 「用它自己的模型配置」, and
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

**The Failed state has a rendering, and it is not a new one** `[derived]`. The failure
renders as one strip at the top of `dbody` `PtmwS`, above 会发生什么 — the same place the
consequences are read from, because a failure is the consequence that actually happened.
Its ink is §1.5's error treatment, cited rather than re-specified: fill `#FF6B6B14`,
stroke `#FF6B6B40`, a `circle-x` in `#FF8A8A`, title Inter 12 / 600 in `#FF8A8A`, and the
machine detail under it in JetBrains Mono 10.5 `#9BA3B8B3`. Nothing else in the dialog
changes — the bullets stay, 取消 stays, and 切换到网关 stays in its full mint treatment.
That last point is the one place this differs from §1.5's dimmed-重试 rule, and the
difference is real rather than an exception: there, a retry with no new hint re-runs an
identical probe and is guaranteed to reproduce the screen; here the input was never the
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

**One primary, three steps, and only two of them can be quoted** `[derived]`
`[contract-gap]` G-10. 安装并切换 promises install → start → switch, and the failure line
this dialog otherwise renders is `fail.detail`, `{{request}} · {{status}} · {{reason}}` —
a shape built for a request, on a page where every other failure is one. The install step
is the one thing on this confirm that is not: G-10 is precisely that no route installs
anything, so a failed install has no `METHOD path` to put in `{{request}}` and no HTTP
status to put beside it. Routing it through this string anyway leaves an implementer
inventing a request that was never sent — `POST /api/models/runtime/start` is the nearest
plausible lie, and it names the step that did *not* fail.

So the detail is selected by step, and the sentence for the install step is the one that
already exists: `install.fail.detail`,
这次安装没有完成,组件可能处于未完成状态。重试会重新装一遍。 It is the same operation failing that 01's *Install failed* reports, and it needs no
evidence shape because a client-side operation with no request has nothing to quote —
what the user needs to know is what state the failure left behind and that the button
works again, which is what that sentence says.

*It says 「可能处于未完成状态」 and not 「什么都没装上」, and the difference is a claim this
UI cannot make.* An earlier wording read 没有任何东西被装上,可以重试。 — a promise about
the disk, asserted by a surface that never watched it. The managed-runtime install is not
atomic from the user's side: it stages bytes, removes any existing install directory, and
only then moves the staged copy into place, so a failure can land with partial bytes
written *or* with a previously working install already destroyed. A string that says
nothing was installed is false in both of those readings, and false in the direction that
matters — it tells a user not to look at a component that may now be broken. The rewritten
sentence is cause-neutral on purpose: it does not guess which of the two happened, because
this dialog has no evidence either way (G-10 is exactly that absence), and it states the
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
| Edit head | `sourceDetail.edit.title` plus kind, credential owner and proved protocol | selected source | close | Dismiss unchanged |
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
delete is reconciled by that exact Source being absent, M2 runs before Source gone. Those reads
cannot recover `removed_hops` or `interrupted`, so both members are marked unavailable rather
than empty and no impact report is invented. If any report-free read fails, C9 enters
Committed projection stale with exactly the returned or inferred write evidence and the last
good dependent projections; its only recovery action retries the same M1/M2 read and cannot
repeat the edit or deletion.

The frame draws position pills and still inherits G-28 until the refusal carries those
positions. It also inherits G-23 for a non-empty `would_interrupt`, because the registered
dialog has the hop block and summary sentence but no second body block that names protected
menu models and Agents. Neither open gap is filled from inference here.

**States** — §0.8, rows marked §1.10. The dialogs trap focus; Tab stays inside. DP-1
reversible states make Escape and the head close equivalent to 取消 and restore focus to
the source overflow trigger. DP-2 disables every dismissal path while Saving source or
Removing source owns a request. DP-4 committed reports instead make close, Escape and an
outside press equivalent to 完成; Committed projection stale exposes only its read Retry.
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

### 1.11 Frame 12 `qQvkP` — Needs-action source cards

**The question it answers:** *when a source has stopped supplying because its credential
needs a person, what action is available at the point where the cause is visible?* Frame
12 registers two card deltas over frame 01: an OAuth source with 重新授权 and an API-key
source with 更换 Key `[frame]`.

The card is not the only credential-repair producer (C1). The healthy-source overflow in
§1.10 keeps the same capability-gated 重新授权 / 更换 Key actions, and both entry points reuse
the lifecycle below. What differs is only the held origin and focus return target: the
needs-action card when repair was inline, the source overflow control when it was elective.

**This is a component exhibit, not a coherent page snapshot** `[frame]`. The unchanged
gateway groups behind the two cards are authoring context and do not claim that broken
sources remain current or that downstream supply stays healthy. Implement only the two
card variants from this frame; derive downstream rollups from the normal §1.1 payloads.

**Element inventory**

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| OAuth needs-action card | Existing source identity and detail; rose dot; canonical `sourceDetail.status.needsAction.*` cause + `upstream.state.supplyStopped` | source | card + 重新授权 | Card → 06; 重新授权 → the shared acknowledgement, with channel-specific warning copy |
| API-key needs-action card | Existing source identity and detail; rose dot; canonical cause + `upstream.state.supplyStopped` | source | card + 更换 Key | Card → 06; 更换 Key → credential replacement entry |
| Repair action | One action selected by credential capability and cause; the same credential producers may also come from the healthy-source overflow | source + §1.4 static vendor register for the two vendor-directed causes | yes when a registered action exists | Starts the matching shared credential repair, or opens the registered external destination; it never also opens 06 |
| Self-managed fallback | `upstream.repair.contactProvider` after either vendor-directed cause on an `api_key` Source | source kind | no | — |

**Geometry** `[frame]`

| Element | Metric |
| --- | --- |
| Subscription card | Fill width, 108 high, `padding [0,12]`, `gap 10`, `#080812`, `#FFFFFF14` border, radius 10 |
| API-key card | Fill width, 106 high; otherwise the same shell |
| Identity tile | 34×34, radius 9; mint-soft fill |
| Name / detail | Inter 12.5 / 700; detail JetBrains Mono 10.5 / 400 `#9BA3B8CC` |
| Needs-action state | 5px `#FF6B6B` dot + Inter 10.5 / 600 `#FF6B6B` |
| Repair button | `padding [5,12]`, `gap 6`, `#FF6B6B1A`, `#FF6B6B59` border, radius 7; label 11 / 600 `#FF6B6B` |

**The copy register wins over the exhibit's abbreviated causes** `[frame]` `[contract]`.
The PNG reads 授权已过期 and 凭据无效; §1.6 already registers the contract's four total
`detail_key` strings and §1.1 requires cards to reuse them. Frame 12 therefore contributes
the two action keys and rose geometry, not a second cause vocabulary. The first example
renders 需要重新登录; the second renders 凭据被吊销. Both append the frame's
literal `upstream.state.supplyStopped`, so the full line still states the drawn supply
consequence without replacing the canonical cause `[frame]` `[contract]`.

**Action selection is the contract's classification × credential-capability mapping**
`[contract]`: refresh-capable auth reauthorizes; a static API key is replaced; balance is
topped up; a banned account goes to the vendor. Frame 12 draws the first two producers.
It does not turn the two drawn buttons into remedies for every `needs_action` cause.

The other two branches consume §1.4's static subscription-vendor register rather than an
OAuth-flow field `[derived]`. On a subscription Source, `balance_exhausted` renders
`upstream.repair.topUp` and opens that vendor's top-up destination;
`account_banned` renders `upstream.repair.contactVendor` and opens its support/appeal
destination. Both reuse the drawn repair-button shell, open in a new browser context and
leave the card in `needs_action`; returning from a vendor page is not evidence that the
vendor changed the account. A later source payload decides the state, and 06's contracted
重新拉取 remains the explicit recovery test after the user acts only when §1.6's
action-capability row renders it for a Hub Source.

An `api_key` Source takes neither link branch, including one created from an official
compatibility preset. Its `vendor` says which protocol family was configured, not who
operates the account. For `balance_exhausted` or `account_banned` it therefore renders the
non-interactive `upstream.repair.contactProvider` fallback and keeps the card target to 06;
it never sends that user to Anthropic or OpenAI on an identity the payload did not prove.

Both Hub and `native_cli` 重新授权 first open the same registered acknowledgement phase
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
  keep the non-null `auth_url` as a visible same-flow fallback. `expects` selects the branch,
  and PD-4 renders a resolved `instructions_key` or the expects-specific fallback when it
  is null or unresolved; G-33 remains only the missing Form B `device_code` output. Submit,
  the evidence-class matrix, 2s polling, timeout, cancel and the F1–F5 treatments are
  otherwise §1.4 verbatim. In particular, E2 transport/`engine_down` evidence keeps polling,
  E3a/E3b distinguish action-required from accepted paste input, E8 `flow_not_found` first
  runs the held Source/attempt-scope read, and only E6's `discovery_failed` /
  `migration_item_conflict` stops immediately and refreshes M3's complete model surface;
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
Agents/source orders and Route chains together. A failed read keeps the report and never
questions the credential write. Only a non-blocked empty-impact success may take the
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
workflow milestone each non-terminal flow state can prove. None can be inferred from the
reversible no-op rule.

**States** — §0.8, rows marked §1.11. Within a card, Tab reaches the card target and then
the repair button; Enter or Space activates the focused target. Activating the nested
repair button does not bubble into the card's open-detail action. Idle confirmations trap
focus; Escape is cancel; Enter activates only the focused control. Pre-flow Reauthorizing
and Replacing key disable dismissal while their mutation owns the response; once a flow is
held, §1.4's explicit cancel path owns its late answer and RR-10 reread (C4). Every impact
report and unresolved result keeps focus inside; close, Escape and outside press invoke the
same committed exit as 完成 and never restore the invoking origin. Committed projection
stale instead focuses its
read-only Retry and exposes no dismissal path back to that origin (C6/C9/DP-4) `[derived]`.

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

## 2. Interaction decisions, and why

Each rule is one line, with one line of why. The why is not commentary: when two
rules collide in code, the reason is the only thing that tells a lane which one to
keep. Without it, the rule that is easier to write wins.

**D-1 — "Relay station" is not a category.** A relay, an aggregator and a
self-hosted endpoint are all *an API key with a custom base URL*.
*Why:* the official/unofficial split is unanswerable for compatible endpoints, and
a category the product cannot adjudicate becomes a label that lies. `[spec §3]`

**D-2 — The user never picks the interface protocol.** The add action performs one
real upstream request and identifies it.
*Why:* it is derivable from evidence the product can obtain in one round trip. A
field the product can answer itself is a field the user can only get wrong.

**D-3 — When identification fails, ask for a hint instead of guessing.** State ④ is
the single protocol selector in the product; nothing is pre-selected, the pick reorders
the probe rather than answering for it, and 重试 stays dimmed until a hint exists.
*Why:* **guessing stores an unverifiable value that fails later, at request time,
far from the moment the user could have fixed it in one click.** One question now
is cheaper than a wrong value forever. This is also why the *picture* enforces it:
an unselected control and a dimmed button cannot silently default. Asking for a hint
rather than an answer is what keeps this compatible with D-4 — the user narrows the
search, the upstream still supplies the fact.

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

**D-7 — A collapse never swallows an active state.** Every non-nominal model row
is visible even if that pushes the group past three rows. §1.1 defines non-nominal as
one thing — a structurally empty Route, `chain_length: 0` — and defines it there rather
than here because that is where the payload it has to be readable from is stated.
*Why:* a collapse exists to hide the boring. If it can hide the one row that needs
attention, the compression has inverted its own purpose. The row that needs attention is
the one that recovers from nothing: a taken-over or cooling row is the system doing its
job, while a model no chain can serve stays that way until somebody configures something.

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

**D-9 — The source order is a placement default that belongs to one backend, and it is
never read at runtime.** One order per gateway-mode backend. It decides where a source
lands in the chains built from the moment it is saved; execution reads the stored chain
and nothing else (S-1), and no later state of this list reaches a chain that already
exists unless the user acts on that chain.
*Why:* N sources × M models of hand-wiring is a configuration surface nobody can hold in
their head, so the order carries the default and the chain carries the decision. Keeping
the two apart is what lets a user edit either one without wondering what the other did
behind them: a reordered list that silently rewrote live chains would make every edit a
change of unknown blast radius. Per-backend rather than global because eligibility
already differs per backend: a global list would have to render entries that cannot
apply, and a user cannot form an opinion about an ordering whose members are conditional.

**D-9a — A backend in 直连 mode exposes no order surface at all.** No 来源顺序 button
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
切换到网关 action and nothing else, a 网关 backend has 来源顺序, model rows and
切换到直连.
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
mint = 使用中 / 正常, gold = 降级 / 暂不可用 / 冷却 / 暂时全部在冷却, rose = 需处理 /
异常 / 无可用来源, muted = 备用 / 未选型号, cyan = 原生 provenance only, violet-tint
`#7C5BFFCC` = a takeover hop label. The group-status vocabulary (§1.1) is assigned here
in full — 正常 mint, 降级 gold, 暂时全部在冷却 gold, 无可用来源 rose, 未选型号 muted — and
the split worth stating is §4.5's: a wait that heals itself takes the same gold as every
other wait, one that does not takes rose, and the fifth is not a fault at all, because
nothing is pinned and no supply's health is being reported.
*Why:* a wire describes a *relation between two things*; state text describes *one
thing's condition*. Collapsing them into one legend forces both to be wrong somewhere —
gold as a relation means supply stopped, gold as a condition means degraded, and those
are not the same claim. §1.0's ink table is the single place both are written down.

**D-22 — A group head's status line is `<mode> · <status>` on the gateway and the mode
word alone in Direct, and the two words are read from two different fields.** 网关 · 正常,
网关 · 降级, 网关 · 暂时全部在冷却, 网关 · 无可用来源, 网关 · 未选型号 — and bare 直连, because a
direct backend arbitrates nothing and so has no supply whose health could be reported.
§1.0's C-6 is the total mapping, `supply_status` `null` included, and no other surface
derives a status line of its own.
*Why:* mode and health are independently variable and users confuse them constantly —
"is it on the gateway" and "is it working" are different questions, and a single word
answers whichever one the reader happened to be asking. `null` is where that separation
earns itself, because it arrives for two unrelated reasons: Direct, which arbitrates
nothing, and a gateway with nothing pinned yet. Splitting on the field that actually
differs is what keeps 未选型号 inside 网关 — an earlier revision read the mode word off
the null instead and rendered 直连 for a backend whose `mode` says 网关, and one before
that rendered 直连 · 正常, inventing a health verdict about a supply path that does not
exist. Reporting 正常 for something the product is not doing is how a status line stops
being evidence.

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

**D-27 — A saved source always has a protocol that was proved by an observed response;
every path that cannot obtain that proof produces "not added".**
> 已保存的来源恒有一个被观测证明过的协议;凡拿不到证明的路径,产物都是「没有添加成功」。

*Why:* this is the property that makes 05's four exits derivable instead of negotiable.
③ and ④ refuse because the protocol is unproved (E-3); ⑤ may save because it *is*
proved and only the inventory is missing (E-5) — and an unknown inventory is a health
fact a source can carry, while an unknown protocol is a value every later request would
have to guess. Stated as a property rather than as a permission, it also decides cases
nobody has drawn yet: any future add path inherits the same test.

**D-28 — A backend-order surface reads `AgentSupply.sources`, that backend's stored Source
order; a source card reads `adopted_by`. Neither substitutes for the other.** §1.3 owns
the first reading — it is the list `PUT /api/models/agents/<backend>/sources` writes and
re-echoes `{agent: AgentSupply}` — and §1.0 owns the second; this decision is only about
the prohibition on deriving either from the other.
*Why:* the two diverge on an ordinary page, not only in edge cases, and they diverge in
**both** directions. `adopted_by` names backends some persisted Route chain references,
de-duplicated by backend and carrying no position. The Source order names the sources a
user placed under a backend, in a sequence, and `agent-supply.schema.json` calls it
「stored configuration and an Add-time placement input」. Those are different sets in each
direction: a source can sit in a backend's order while no chain on that backend names it —
the order is a default for placement, and §1.2 edits chains independently of it — and a
source can be adopted by a backend whose order does not contain it, because a chain may
name any source and 移出 does not rewrite chains (§1.3 says exactly this: 「a source outside
this backend's order is still a source」). Read the order into the card and it reports
adoption that nothing routes; read `adopted_by` into the drawer and every held-out source
and every ordered-but-unreferenced source vanishes from the list the drawer exists to edit,
while the field's missing position makes an order impossible to reconstruct at all.

*And neither is the stored chain, which is a third projection with its own owner.* §1.2's
route-chain editor is where hop order lives, at per-model grain. The prohibition covers
that pair too: a card that read a chain would have to pick one hop — first, serving, any —
and each answer is a different sentence that changes under the user with no edit and no
notification. **Naming the Source order here does not make it runtime state.**
`model-hub.md` §4.2 is explicit that it is 「a visible Gateway default for Add-time
placement, not a runtime capability filter」 and that 「the only order runtime can execute is
the exact hop order stored for that model」; D-9 says the same from this side, which is why
reordering the drawer reaches no existing chain and needs no guard. This decision assigns
each surface the projection it displays; it does not promote any of the three into an input
for another.

Neither projection reports live supply, and the card must not be written as though one
did. `api.md` calls `adopted_by` 「the stable Source-card projection of persisted
references」, and *stable* is the whole property: the array is unchanged by a cooldown, a
revoked credential, or a takeover routing past this hop. So the card's question is 「who
has this configured」, the drawer's is 「in what order」, and 「who is drawing from it right
now」 is a third question **neither field answers** — it needs the per-chain runnability
read, at row grain, on a payload this page does not fetch. Naming it as a third question
rather than assigning it to `adopted_by` is the point of this decision: the earlier
wording claimed the field 「combines runnability」, which the contract does not say and the
word *stable* rules out, and a card built on that reads 使用中 beside a source whose
credential died an hour ago.

*This decision was re-derived at `ca45aeb6`, and the field it originally named is gone.*
It used to read 「a backend-order surface reads `order_enrolled_by`」, with a divergence
argued from enrolment-without-adoption. S-1 deletes enrolment, and `order_enrolled_by`
goes with it, so the old sentence named a field no implementation can read. The decision
survives because what it actually governs is *which projection answers which question*,
and the projections that answer them still exist — `AgentSupply.sources` and `adopted_by`
— but it survives as a re-derivation, not as a citation that quietly kept pointing at a
deleted field. The first re-derivation replaced the deleted field with 「the stored
chain」, which was the wrong substitute for a second time: §1.3 saves and reads back
`AgentSupply`, never a chain, so the rule named a projection its own owning section does
not fetch. The correction is recorded here rather than applied silently, because the
failure mode is the interesting part — a re-derivation done in one pass over the decision
and not over the section it governs. Worth recording, because nothing in the review
flagged it: a citation to a deleted name reads exactly like a citation to a live one.

**D-29 — The page is 「模型」, the module is 「来源与网关」, and 「模型网关」 is never
rendered.** The project's name for this work is not a string in the product.
*Why:* the plan documents call the whole effort 模型网关, and carrying that onto a
surface produces two visible strings a level apart sharing a word — a page named 模型网关
containing a tab named 模型网关 — which reads as though the tab *is* the page. The
frames already do this correctly; the rule is written down so a lane reading the plan
files does not "fix" the page title to match them. Note that the design file's frame
names do contain 模型网关: they are canvas labels for the author, and D-17 already says
a frame's shell is not the shipped shell.

**D-30 — 切换到直连 commits on the press; only 切换到网关 gets a confirm.** Adopting the
gateway opens frame 10 (§1.9); leaving it sends `PATCH /api/models/agents/<backend>/mode`
straight from the group head with no intermediate surface, and F1 in place is the whole
of its failure handling.
*Why:* a confirm is owed where an action is hard to take back, and these two directions
are not symmetric. Adopting changes where a backend's traffic goes and is the step frame
10 exists to explain, down to where the undo lives (`undo.1`, `undo.2`). Leaving
destroys nothing: the same dialog's third line already promises
that the sources stay and only stop supplying that backend `undo.3`, and §1.8's
*Retained sources, all direct* is the state that promise lands in. Putting a confirm on
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
§1.0's *Starting*, *Impaired*, *Unreachable* and *Partial* all hand back to the dispatch
*Loading* performs, rather than to Ready.
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

**D-35 — The collapse row is the chain read's re-read control.** Collapsing a group and
expanding it re-issues the per-model chain read for every row in it. §1.1's *Chain
unresolved* names it as the repair, and no per-row 重试 is added to the frame.
*Why:* that read is per row, allowed to fail, and drawn nowhere, so a row whose read
failed had `—` in three columns and nothing that would ask again — a dead end, which is
worse than the dead control D-9a rules out and is not licensed by it. A control already
on the frame, already meaning 「show me this group's rows」, re-reads them at no cost in
surface. The two alternatives were a per-row button on a surface with one row per model,
and a poll cadence this file has no basis to choose; the frame rules out the first and
「no exit keys on elapsed time」 rules out the second.

**D-36 — A lost response is reconcilable exactly when the client already holds its
subject's identifier.** F1 leaves a mutation's outcome unknown and the repair is always
a read; what decides whether that read is a *reconciliation* or merely a refresh is
whether the client can name what it is asking about. §1.6's *Refetch failed* holds the
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
§1.5's ⑦ and §1.4's *Start failed* hold nothing the server
can be asked about — the `id` and the `flow_id` were both born in the response that died
— and those become G-29 and G-30 rather than exits this file could write.
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
| **How a request executes a stored chain — the sole authority** | `model-hub.md` §4.3 — *The only normative configured-chain execution algorithm* |
| Whether eligibility is client- or server-decided | `model-hub.md` §4.4 — *Configuration eligibility is server-authoritative (v3)* |
| Source states, self-healing classes, `detail_key` vocabulary | `model-hub.md` §4.5 — *State taxonomy — classified by "does it heal itself"* |
| How a configured chain is stored and mutated | `model-hub.md` §4.6 — *Configured-chain storage and mutation* |
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
