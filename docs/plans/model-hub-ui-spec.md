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
| 05 | `GDErR` | 模型网关 05 — 添加 API Key |
| 06 | `wItw4` | 模型网关 06 — 来源详情 · 型号管理 |
| 08 | `Doqav` | 模型网关 08 — 故障实况(网关接管中) |
| 09 | `UVR97` | 模型网关 09 — 直连态首屏(升级后第一屏) |
| 10 | `g7MOA4` | 模型网关 10 — 为单个后端启用网关(动作与后果) |

There is no 07: it was removed during the design pass and the remaining frames
were deliberately **not** renumbered, so that every existing reference to "08"
keeps pointing at the same picture.

All nine frames are 1440×1100 Dark. Light and mobile variants are not drawn yet, so
every geometric statement in this file is a statement about Dark desktop until they are.

**These frames do not draw a navigation path, and none may be inferred from them.**
The nine frames were composed to make the model *legible* — the shell around them is
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
was checked against `docs/model-hub-v3-local-gateway` @ **`ca45aeb6`** — the current head
of the spec lane's PR #1215 — **not** against `master`, whose §3, §4.1, §4.2, §4.6
and §5 have all been superseded there. A reader on `master` will find some anchors
missing; that is the expected state until #1215 lands, and this file must not merge
before it does.

The basis has moved twice while this file was being written: `7984aabf` → `176b41b7`
→ `ca45aeb6`. The first move added AC-29/30/31, and **AC-30** (takeover is derived, and
a chain with no runnable hop renders none of takeover's visual semantics) and **AC-31**
(Direct is a mode and the first state of an existing install; Native names a hop and
never a mode) land directly on frames 08, 09 and 10. The second move is **S-1**, and it
is larger: a configured chain is now stored configuration executed as written, with no
runtime Source/model matching, no `follow | custom` state, and no second projection
derived from a backend order.

Re-reading on each move is not bookkeeping, and this round proves it twice over. Two of
the three frame-versus-contract conflicts in §0.6 existed only at `176b41b7`. And at
`ca45aeb6`, **D-28 turned out to be ruling on `order_enrolled_by`, a field S-1 deletes**
— a decision that still read as a live citation because a citation to a removed name
looks exactly like a citation to a present one. Nothing flags it; only re-reading the
basis does. It is re-derived in §2 with that history attached rather than quietly
repointed.

**What this file has deliberately *not* yet rewritten, and why.** §1.1's legend note and
`gateway.row.followsOrder`, and the whole of §1.2's follow/custom machinery, describe the
derivation S-1 abolishes. They are `[frame]` strings — measured from frames 01 and 02 as
they are drawn today — and the owner has frozen the rewrite of those two frames until the
frozen contract artefact is delivered. Rewriting the spec text first would put this file
ahead of the design file rather than in agreement with it, which is the same defect as
lagging behind it, in the other direction. §1.3 was rewritten this round precisely because
its frame *had* already been rebuilt. The divergence is named here so that it reads as a
scheduled rewrite rather than as a claim this file still stands behind.

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

### 0.4 Not in scope

Behaviour invariants (persistence, event fan-out, schema constraints, resolver
precedence). Those belong to §8 of the implementation plan. Where drawing the
frames surfaced a *missing* behaviour invariant, it is listed in the PR
description under 「建议移交 AC 账本」 for routing — not written here, and not
written into §8 by this lane.

### 0.5 Contract-gap registry

Every `[contract-gap]` in this file, in one place. A `[contract-gap]` statement
describes the intended surface, and is **not** a requirement on the build: where a frame
draws an affordance that sits on a gap, the section that owns that frame says so
explicitly rather than quietly requiring it.

| # | Surface | Missing | Verified absent at `ca45aeb6` |
| --- | --- | --- | --- |
| G-3 | 06 model inventory | a way to retire a *discovered* model from a source's inventory, **and a place to remember that it was retired** | `api.md`'s `DELETE /api/models/custom-models` 「removes only the named manual model」; no other inventory-shrink route is user-initiated, and `source.schema.json`'s `models` carries no per-model retained flag |
| G-8 | 06 tier editing | a route that saves an edited reasoning-effort list, and the field it saves into | FC-12 requires `api.md` to contract 「all-inventory reasoning-list edits」 and FC-03 requires the model entry to carry `reasoning_efforts`; the frozen v2 artefacts carry neither — `api.md` has only `POST` / `DELETE /api/models/custom-models`, and `source.schema.json`'s model entry is `{id, provenance, display_name?, discovered_at?}` |

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

G-3 is the one real gap left, additive, listed in §0.7 for routing into the AC ledger. It
is not decided here. This lane owns the visible layer, and inventing a persistence model
to make a drawn control defensible is exactly the kind of quiet scope grab that produces
two disagreeing authorities.

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
native singleton. The owner ruled for the behaviour spec, and frame 03 was rebuilt as
the per-backend editor now described in §1.3 — a drawer titled for one backend, opened
from that backend's group head, with the follow-versus-custom ownership state, the
「恢复推荐顺序」 escape and the 「新来源不会自动排进来」 hint that a server-computed
recommendation implies. §1.1, §1.2, §1.3, D-9 and D-10 are written to the ruled model;
the `gateway.globalOrder` / `chain.hop.globalRank` / `order.*` keys are gone. It is
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

One behaviour the frames imply, which no AC through AC-31 covers and which this lane
does **not** write into any document. It is in the PR description under 「建议移交 AC
账本」 for the spec lane to route; it is named here only so that a reader of §1.6 can
see that the silence is deliberate:

- **Retiring a discovered model needs somewhere to remember the retirement.**
  `source.schema.json`'s `models` describes what an inventory refresh found; it carries
  no per-model retained flag, and `DELETE /api/models/custom-models` 「removes only the
  named manual model」. So a user-initiated retirement of a *discovered* id has no
  representation that survives the next refresh. This is the second half of G-3, and it
  is the reason this file states only that no surface claims the capability, rather than
  requiring the control.

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
- **Which projection a backend-order surface consumes** — ruled. Order surfaces read the
  stored chain; source cards read `adopted_by`; **neither may stand in for the other**.
  D-28 carries the rule, the reason, and the note that S-1 deleted the field the ruling
  originally named.

One earlier item left the same way: takeover-count agreement across grains landed as
**AC-30**, which at `ca45aeb6` states takeover is 「a projection of visible configuration
plus live runnability, not a stored sibling state」 and requires a fixture where a chain
with no runnable hop renders 「no takeover badge, connector color, or other takeover
visual semantics」, and this file cites that wording instead of restating it.

---

## 1. Per-frame specification

### 1.0 Shared shell

Seven frames render the same chrome, 06 renders a drill-in variant of it whose header
left is a bare back icon, and 04/05/10 render it behind a scrim. Specifying it once is
not a shortcut: a shell
duplicated across nine sections is a shell that will drift in eight of them.

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
| `title` + info icon | Page name | static | icon: hover | Tooltip: what the gateway is `[derived]` |
| Run pill | Engine liveness | engine status | **`not_started` / `stopped` / `not_installed`: yes; `running`: no** `[derived]` | `not_started` / `stopped`: start the engine (`POST /api/models/runtime/start` `[contract]`). `not_installed`: open D-26's install confirm — never the start route |
| Tabs ×2 | Section switch | — | yes | 来源与网关 / 用量与额度; the active one gets the mint underline. **Which route these correspond to is not specified by these frames** (§0.1) |
| Upstream module | Source inventory | `GET /api/models/sources` `[spec]` | rows: yes | Open 06 for that source |
| Dispatch rail | That upstream feeds gateway | derived, decorative | no | — |
| Gateway module | One group per backend, each with model rows | per-backend supply + chains `[spec]` | rows, collapse, 「来源顺序」, mode switch | Open 02 / expand / open 03 for **that backend** / open 10's confirm |
| Legend | Colour → meaning | static; kept in bijection with the inks the page draws | no | — |

**Shared state machine**

| State | Entry | Exit |
| --- | --- | --- |
| Loading | Route entered, first payload outstanding | Payload arrives → Ready, or fails → Unreachable |
| Ready | Payload arrives | Any mutation re-renders in place `[derived]` |
| Empty (no sources) | `sources == []` | First source added → Ready |
| **Not installed** | Runtime status reads `not_installed` `[contract]` | Dependency becomes present → Not started |
| **Not started** | Runtime status reads `not_started` `[contract]` | User activates the run pill → Starting → Ready |
| **Starting** | Start accepted, engine not yet live | Live → Ready; start fails → Unreachable |
| Unreachable (engine down) | Status request fails, or the engine was running and died | Recovery → Ready |
| Partial | Sources load, per-backend supply does not | Retry succeeds → Ready |

Empty, Not installed, Not started, Starting, Unreachable and Partial are **not
drawn** `[derived]`. Required behaviour:

- Empty: upstream module keeps its head and footer and shows one line —
  「还没有来源。先添加一个订阅或 API Key。」 The gateway module shows its backend
  groups with 「没有可用来源」 per group rather than vanishing; a backend that
  exists is a fact independent of whether anything can supply it.
- **Not installed**: the pill reads 「网关组件未安装 · 点击安装」 and **is** an activation
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
- **Not started**: the pill reads 「网关未启动 · 点击启动」 and is the page's start
  affordance. It is styled as an *idle* pill — `$--muted` label on `#FFFFFF0A`,
  **not** the error treatment `[derived]`. The runtime contract classes
  `not_started` as lazy-start idleness rather than an alarm `[contract]`, and a
  page that paints idleness red teaches users to ignore the colour that matters.
  Derived columns render `—` exactly as in Unreachable; supply that has never been
  arbitrated is unknown, not empty.
- **Starting**: the pill reads 「正在启动…」 with the `loader-circle` spinner and
  stops accepting activation, so a second click cannot queue a second start
  `[derived]`.
- Unreachable: the run pill flips to 「网关未运行」 — the error treatment, because an
  engine that *was* running and stopped answering is a fault — and every derived
  column (current source, chain, takeover) renders `—`, **not** a stale last-known
  value. See D-3: a surface that cannot prove a fact must say so.
  Recovery offers the same start action as Not started.
- Partial: only the sub-tree that failed degrades. A failed supply payload must
  not blank the source inventory, which loaded fine.

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

The count-bearing keys in this file are `shell.allDirect`, `upstream.count`,
`gateway.modelCount`, `gateway.collapse`, `chain.derived.hops`,
`sourceDetail.summary` and `takeover.pill` — seven, all under `models.hub.*`; each
appears below in its `_one` / `_other` form. This list is one side of a set equality
— the keys interpolating `{{count}}` and the keys shipping plural families are the same
set — so adding a `{{count}}` key anywhere under `models.hub.*` without adding it here
breaks that equality.

| Key | 中文 | English |
| --- | --- | --- |
| `shell.title` | 模型 | Models |
| `shell.running` | 网关运行中 | Gateway running |
| `shell.stopped` `[derived]` | 网关未运行 | Gateway not running |
| `shell.notStarted` `[derived]` | 网关未启动 · 点击启动 | Gateway not started · click to start |
| `shell.notInstalled` `[derived]` | 网关组件未安装 · 点击安装 | Gateway component not installed · click to install |
| `shell.allDirect_one` `[frame]` | {{count}} 个后端都在直连 | The only backend is direct |
| `shell.allDirect_other` `[frame]` | {{count}} 个后端都在直连 | All {{count}} backends are direct |
| `shell.starting` `[derived]` | 正在启动… | Starting… |
| `install.title` `[derived]` | 安装网关组件 | Install the gateway component |
| `install.subtitle` `[derived]` | 只安装组件,后端保持现在的方式 | Installs the component only; the backends keep working the way they do now |
| `install.section.effects` `[derived]` | 会发生什么 | What will happen |
| `install.effects.1` `[derived]` | 下载并安装 {{component}},约 {{duration}} | {{component}} is downloaded and installed, about {{duration}} |
| `install.effects.2` `[derived]` | 装好后网关自动启动 | The gateway starts automatically once it is installed |
| `install.effects.3` `[derived]` | 没有后端会被切换,型号菜单不变 | No backend is switched and the model menu does not change |
| `install.confirm` `[derived]` | 安装并启动 | Install and start |
| `install.cancel` `[derived]` | 取消 | Cancel |
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
| `upstream.state.unavailableRetry` | 暂不可用 · {{time}} 后自动重试 | Unavailable · retrying automatically after {{time}} |
| `upstream.empty` `[derived]` | 还没有来源。先添加一个订阅或 API Key。 | No sources yet. Add a subscription or an API key first. |
| `upstream.addSubscription` | 添加订阅 | Add subscription |
| `upstream.addApiKey` | 添加 API Key | Add API key |
| `gateway.heading` | 网关 | Gateway |
| `gateway.sourceOrder` | 来源顺序 | Source order |
| `gateway.switchToGateway` | 切换到网关 | Switch to gateway |
| `gateway.switchToDirect` | 切换到直连 | Switch to direct |
| `gateway.modelCount_one` | {{count}} 个型号 | {{count}} model |
| `gateway.modelCount_other` | {{count}} 个型号 | {{count}} models |
| `gateway.group.subtitle.direct` `[frame]` | {{mode}} | {{mode}} |
| `gateway.group.subtitle.gateway` `[frame]` | {{mode}} · {{status}} | {{mode}} · {{status}} |
| `gateway.group.mode.direct` | 直连 | Direct |
| `gateway.group.mode.gateway` | 网关 | Gateway |
| `gateway.group.status.ok` `[contract]` | 正常 | Healthy |
| `gateway.group.status.degraded` `[contract]` | 降级 | Degraded |
| `gateway.group.status.waiting` `[contract]` | 等待重试 | Waiting to retry |
| `gateway.group.status.interrupted` `[contract]` | 已中断 | Interrupted |
| `gateway.group.takenOver` | 接管中 | Taken over |
| `gateway.supply.none` `[derived]` | 没有可用来源 | No usable source |
| `gateway.group.emptyModels` `[derived]` | 这个后端没有可用型号 | This backend has no models |
| `gateway.row.followsOrder` | 跟随来源顺序 | Follows the source order |
| `gateway.row.custom` | 自定义链 | Custom chain |
| `gateway.row.current` | 当前 {{source}} | Now: {{source}} |
| `gateway.row.currentTakeover` | 当前 {{source}}(接管) | Now: {{source}} (takeover) |
| `gateway.collapse_one` | 还有 {{count}} 个型号 | {{count}} more model |
| `gateway.collapse_other` | 还有 {{count}} 个型号 | {{count}} more models |
| `legend.native` `[frame]` | 原生 | Native |
| `legend.viaGateway` | 网关供给 | Gateway supply |
| `legend.connectedUnused` | 已启用 · 当前未被使用 | Enabled · not currently used |
| `legend.takeover` | 接管中 · 临时改走 | Taken over · temporarily rerouted |
| `legend.unavailable` | 暂不可用 · 供给已暂停 | Unavailable · supply paused |
| `legend.note` | 路由链按各后端的来源顺序自动派生;单个型号可改成自定义链 | Route chains are derived from each backend's source order; any single model can be switched to a custom chain |

**The group subtitle is a total rendering of `supply_status`, and Direct mode has no
status word** `[contract]`. `agent-supply.schema.json` enumerates
`ok / degraded / waiting / interrupted / null`, and `null` is what Direct mode produces —
there is no arbitration to report when nothing is being arbitrated. This section owns the
mapping; §1.1, §1.7 and §1.9 render it and state no mapping of their own:

| `supply_status` | Subtitle | Key |
| --- | --- | --- |
| `null` (Direct mode) | 直连 | `subtitle.direct` + `mode.direct` |
| `ok` | 网关 · 正常 | `subtitle.gateway` + `status.ok` |
| `degraded` | 网关 · 降级 | `subtitle.gateway` + `status.degraded` |
| `waiting` | 网关 · 等待重试 | `subtitle.gateway` + `status.waiting` |
| `interrupted` | 网关 · 已中断 | `subtitle.gateway` + `status.interrupted` |

The four non-null values differ in *what a person can do about it*, which is why
collapsing them loses the only thing the word is for: `ok` is serving from the intended
head; `degraded` is serving through a fallback or past blocked members, so requests
still succeed; `waiting` is every member in a cooldown that is not yet retry-ready, so
nothing is being served but the process is fine and time alone fixes it; `interrupted` is
the native CLI being unavailable *in this process*, which no amount of waiting resolves.
A UI that renders only 正常/降级 tells a user in `waiting` to go fix something and a user
in `interrupted` to wait.

**接管中 is not one of these values, and must not be rendered from this field.**
Takeover is a projection of the chain — the current hop is not the first hop, and the
first hop is unavailable for a *recoverable* reason (AC-30, D-21, C-5). A chain whose
hops are all exhausted has no runnable hop and therefore **no takeover**, and must draw
no takeover badge, connector colour or other takeover visual semantics. The two facts
can co-occur (a taken-over backend usually reads `degraded`) and are computed from
different inputs, so a surface that derives one from the other is right by accident until
the first `degraded`-without-reroute payload.

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
| `#FF6B6B` | **needs action — this source cannot serve until a person acts** | **never** | error emphasis | advisory: 05 state ③ strip (`#FF6B6B14` / `#FF6B6B40`). relation/status: no frame draws it `[derived]` — see below |
| `#FFFFFF26` | connected but not currently used | — | — | dim wire `@1` only |

**Violet and gold were previously one row, and merging them was a real error, not a
simplification** `[frame]`. Measured in 08: the takeover wire `AEaxi` is `#7C5BFF`
`@1.75` and the paused-supply wire `gtjOy` is `#FFC857` `@1`. The legend says the same
thing in its own swatches — `LmQFp` `#7C5BFF` labels 「接管中 · 临时改走」 and `oopTe`
`#FFC857` labels 「暂不可用 · 供给已暂停」. The two facts are opposite in valence: violet
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
is `[derived]`, not `[frame]`: no frame draws a source in `needs_action` or `error`, so
no wire, dot or status word is rose anywhere in the nine. D-21 nevertheless assigns
those states this ink, and §1.1 and §1.6 both admit them as states, so the row states
the meaning and marks it as unrendered instead of leaving the ink undefined the first
time an implementation has to draw one. Rose never inks a control, for the same reason
cyan never does: an error is not something you select.

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
| `uf3re` detail | account label, or `host/path · masked key` | source | no | — |
| `YcOFo` status | who it is supplying right now | derived from live supply | no | — |
| `wmROQ` / `Xitl7` footer buttons | Add subscription / Add API key | — | yes | Open 04 / 05 |
| `f8w6Xp` + `pnYa0` rail | dispatch happens between the columns | decorative | no | — |
| `GLylJ` backend group | backend tile, name, model count, head buttons, and one `{{mode}} · {{status}}` line | per-backend mode + supply health | head: buttons only | — |
| `ehGRK` / `bGsC7` 「来源顺序」 | — | — | yes | Open 03 **for that backend** |
| `IyKyp` 「切换到网关」 | — | backend in 直连 | yes | Open the 10 confirm for that backend |
| `z02Ep` / `gbrq2` 「切换到直连」 | — | backend on the gateway | yes | Confirm, then that backend leaves the gateway |
| `Exx0a` model row | model id (mono 12), mode chip, current-source text | chain head per model | yes | Open 02 for `(backend, model)` |
| `ZM1pm` collapse row | `还有 N 个型号` | count of hidden rows | yes | Expand in place |
| `FZUYI` wire layer | one path per supply relation + endpoint dots | derived supply set | no | — |
| `ftWgW` legend info icon | why chains look derived | static | hover / focus | Tooltip |

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

**State machine**

| State | Entry | Exit |
| --- | --- | --- |
| Ready | Sources + per-backend supply both loaded | — |
| Empty | `sources == []` | First source added |
| Loading | First paint | See §1.0 |
| Per-source `cooldown` | Source reports cooling `[spec §4.5]` | `retry_at` passes → previous state |
| Per-source `needs_action` | Credential dead `[spec §4.5]` | User acts → re-validated |
| Per-source `error` | Unclassified failure `[spec §4.5]` | User acts |
| Backend has no usable source | Every candidate filtered out | Any source becomes eligible |
| Backend has no models | The group resolves to zero model rows `[derived]` | A model becomes available to that backend |
| Takeover active | Head source unavailable, next one serving | Recovery → Ready (this is frame 08) |
| Group expanded | Collapse row activated | Collapse toggled back |

Credential-invalid is the one worth stating precisely `[derived]`: a
`needs_action` source **stays in the list, in place**, with its status line
replaced by the cause and a one-tap repair action. It is not removed, not moved to
the bottom, and not silently dropped from the chains that name it — a source you
cannot see is a source you cannot fix.

**A backend with zero model rows is a different emptiness from a backend with no usable
source, and they must not share a message** `[derived]`. 没有可用来源 says *this backend
has models and nothing can serve them* — the fix is a source. 「这个后端没有可用型号」 says
*this backend has no models to serve* — the fix is a model. One message for both spends
the single action the user takes on the strength of it, and spends it on the wrong thing
half the time. The group keeps its header and its `<mode> · <status>` line either way;
only the row area differs.

**This list states the cause and does not offer the fix, and that is a scope fact rather
than an omission** `[derived]`. None of these nine frames draws the surface that edits a
backend's model menu, so there is no add affordance on this list to keep enabled — and
naming where that surface lives would be a navigation path, which §0.1 forbids this file
to draw. The empty state claims the message and the preserved shell here, and claims a
live add affordance only for the lists that actually draw one.

**Extreme data**

Collapse predicate for a backend group `[frame]` for the shape, `[derived]` for the
ordering rule:

```
N = 3                                       # ADDITIONAL nominal rows, not a total

# 1. ORDER — one total order over the whole group, computed before anything is hidden
key(m)    = (0 if m.hasOverride else 1,     # overrides outrank
             m.backendMenuIndex)            # then the backend's own menu order
sorted    = sort(models, by=key)

# 2. SELECT — a filter over `sorted`, which never reorders it
mustShow  = { m in models | m.state != nominal }              # hard: never collapsed
baseline  = take([m in sorted | m.state == nominal], N)       # N ADDITIONAL nominal rows
visible   = [m in sorted | m in mustShow or m in baseline]
collapsed = models - visible

render collapse row  iff  |collapsed| > 0
collapse label count = |collapsed|
```

**`key` is total, and the two steps are separate on purpose.** `backendMenuIndex` is
unique within one backend's menu, so no two models tie and `sorted` is one determinate
sequence — every row on the surface, visible or collapsed, has a position before the
collapse predicate runs. Selection is then a *filter*, so expanding stops hiding rows
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

**The question it answers:** *for this one model, which sources will be tried, in
what order, and is that order the backend's or mine?* Two dialogs, side by side, are
the two answers: follow this backend's source order, or hold a custom chain for this
one model.

The two are drawn as one dialog in two states, not as two screens `[frame]`: same
title, same backend subtitle, same segmented control, same list geometry. Only three
things change — the list's contents and controls, the hint, and the foot. That is
deliberate: switching mode has to read as *this chain is now mine* rather than as
*I have gone somewhere else*.

**Element inventory** — ids are given `follow` / `custom` where the two states have
distinct nodes.

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| `zmWYg` / `YyKOM` head | `{{model}} · 路由链` over the backend name | route params | close icon | Dismiss |
| `UxCia` / `whFKJ` segmented | 跟随来源顺序 / 自定义链 | override presence | yes, 2 options | Switch mode; picking custom **forks** the chain |
| `y9mDvQ` + `OL7EH` | 当前派生结果 / 3 跳 | derived chain `[spec §4.3]` | no | — |
| `G7zW9G` + `l0d5v` | 这个型号的路由链 / 自定义链 | stored override | no | — |
| `F2sqds` hop (follow) | ordinal badge, source name, effective upstream model id (mono), `顺序 #n` tag | chain hop | no | — |
| `Fq0MA` hop (custom) | same, minus the tag, plus up / down / remove | chain hop | yes | Reorder / remove |
| `HOQqF` 添加一跳 | — | eligible sources not yet in the chain | yes | Source picker `[derived]` |
| `dv2PI` / `c8E1o0` hint | what this mode implies for future changes | static per mode | no | — |
| `HAsm6` 关闭 (follow foot) | — | — | yes | Dismiss |
| `bG5Mc` 恢复跟随来源顺序 (custom foot) | — | — | yes | Drop the override |
| `hME6Z` / `RSZsf` 取消 / 保存 (custom foot) | — | — | yes | Discard / persist |

**The foot is the honest tell of which mode owns the chain** `[frame]`. Follow has one
button, 关闭 — there is nothing to save, because the chain is a *view* of the backend's
source order. Custom has three, and 恢复跟随来源顺序 sits apart from 取消 / 保存 because
it is not a third way to dismiss: it deletes the override. A single 保存 in both modes
would quietly imply the derived chain is also stored — the exact misreading D-9 rules
out — and would hide the return path D-10a requires the transfer to keep visible.

**Metrics** `[frame]`: dialog 520 wide, height auto, `$--surface`,
`$--border-strong`, `radius 14`; head `padding [16,20]` `gap 4`, bottom border, title
15/700 Inter over a 10.5 JetBrains Mono `$--muted` backend line, close 15px
`#FFFFFF59`; body `padding 20` `gap 14`; foot `padding [14,20]` `gap 8`, top border,
fill `#FFFFFF05`, buttons `padding [8,14]` `radius 7` (保存 `$--mint`). Segmented
`padding 3` `gap 3` `radius 9` fill `#FFFFFF0A` stroke `$--border`; segment
`padding [7,14]` `radius 6`, selected `#FFFFFF1A` 12/600 `$--foreground`, idle 12/500
`$--muted`. Section label 10.5/700 `#FFFFFF73` + a `padding [3,8]` `radius 999` chip.
Hop list `fill_container` `padding 8` `gap 6` on `$--background` `radius 10`
`$--border`; hop 52 tall `padding [0,10]` `gap 10` `radius 8` `$--border`; ordinal
22×22 `radius 6`, **#1** `#5BFFA01A` / `$--mint`, **#2+** `#FFFFFF0A` / `$--muted`,
number 11/500 mono; source 12/600; effective id 10.5 mono `#9BA3B8B3`; tag
`padding [3,8]` `radius 999` `#FFFFFF0A` / `$--border`, 10/600 `$--muted`. Icon
button 26×26 `radius 6` `#FFFFFF0A` / `$--border`, glyph 13px. Hint: 13px info
`#FFFFFF59` + 11.5 `#FFFFFF73`.

**Three inks separate the two modes, and all three say the same thing** `[frame]`.
Follow renders the hop fill at `#FFFFFF03` and the source name at `#F5F1E8B3`; custom
renders `#FFFFFF08` and `$--foreground`. The state chip follows §2 D-19 exactly —
derived 「3 跳」 is the muted neutral `#FFFFFF0A` / `$--muted`, user-set 「自定义链」 is
the strong neutral `#FFFFFF14` / `$--foreground`. Nothing here is a second accent
hue: the whole distinction is carried by one step of contrast, three times, which is
why it reads as *authored versus derived* rather than as *important versus not*.

**The first hop's ↑ is the only disabled control in this dialog** `[frame]` — glyph
`#FFFFFF33` against `$--foreground` on every other icon button, in the same 26×26
`#FFFFFF0A` shell. The shell staying put is the point: the boundary of the list is
shown by dimming the glyph, not by removing the button and re-flowing the row. This
treatment is local to frame 02: the other reorderable list, frame 03's drawer, has no
per-row arrow buttons to dim (§1.3), so nothing here carries over to it.

**State machine**

| State | Entry | Exit |
| --- | --- | --- |
| Follow | No override for `(backend, model)` | Any manual edit → Custom (implicit, immediate) |
| Custom | Override exists, or the user edited while in Follow | 恢复跟随来源顺序 → Follow |
| Custom · dirty | An edit is pending | 保存 → persisted; 取消 → discarded |
| Empty chain | No source can supply this model | A source becomes eligible |
| Loading | Dialog opened before the chain resolves | Chain arrives |
| Error (save failed) | Persist rejected | Retry, or 取消 |
| Hop references a dead credential | A hop's source is `needs_action` | User repairs it |

Two `[derived]` rules the frame cannot show:

- **Empty chain — two states, not one.** A custom chain with no hops and a model no
  source can supply look identical in the hop list and are opposite in what the user
  should do next, so they get different lines and different exits.
  - **Emptied by editing**, with eligible sources still available: 「这条链现在是空的。
    添加一跳,或恢复跟随来源顺序。」 `[derived]`. 添加一跳 is enabled and its picker has
    candidates. This is the state the old rule was written for, and for it the old rule
    was right.
  - **No source can supply this model** at all: 「现在没有来源能提供这个型号」. 添加一跳
    stays enabled — D-15 keeps exits live — but pressing it opens a picker with nothing
    in it, so **the picker owns its own empty state** and says where the fix is:
    「还没有能提供这个型号的来源。先在「来源」里添加一个。」 `[derived]`, with the
    upstream module as the action.
  An enabled button that opens an empty list is not honesty, it is a dead end with a
  friendly face; the honest version is a button that opens something which tells you
  what to do. Collapsing the two states hid that, because the copy described the state
  the button cannot fix while the justification described the state it can.
- **Dead hop**: it renders in place with its cause, greyed, and *keeps its
  ordinal*. Renumbering around a broken hop hides the fact that the chain is
  shorter than it looks.

**Copy** — `models.hub.chain.*`

| Key | 中文 | English |
| --- | --- | --- |
| `title` | {{model}} · 路由链 | {{model}} · routing chain |
| `subtitle` | {{backend}} | {{backend}} |
| `mode.follow` | 跟随来源顺序 | Follows the source order |
| `mode.custom` | 自定义链 | Custom chain |
| `derived.label` | 当前派生结果 | Derived result |
| `derived.hops_one` | {{count}} 跳 | {{count}} hop |
| `derived.hops_other` | {{count}} 跳 | {{count}} hops |
| `custom.label` | 这个型号的路由链 | This model's routing chain |
| `custom.badge` | 自定义链 | Custom chain |
| `hop.orderRank` | 顺序 #{{n}} | Order #{{n}} |
| `hop.add` | 添加一跳 | Add a hop |
| `hint.follow` | 跟着这个后端的来源顺序走,顺序变了这条链跟着变。 | Follows this backend's source order — change the order and this chain changes with it. |
| `hint.custom` | 已脱离来源顺序。以后来源顺序怎么变,这条链都不变。 | Detached from the source order. However the source order changes later, this chain will not. |
| `restore` | 恢复跟随来源顺序 | Restore the source order |
| `empty.noSource` `[derived]` | 现在没有来源能提供这个型号 | No source can supply this model right now |
| `empty.edited` `[derived]` | 这条链现在是空的。添加一跳,或恢复跟随来源顺序。 | This chain is empty. Add a hop, or restore the source order. |
| `hop.picker.empty` `[derived]` | 还没有能提供这个型号的来源。先在「来源」里添加一个。 | No source provides this model yet. Add one under Sources first. |
| `close` | 关闭 | Close |
| `cancel` | 取消 | Cancel |
| `save` | 保存 | Save |

`mode.*` and `custom.badge` are deliberately the same two strings twice `[frame]`. The
segmented control names the two modes; the chip states which one is in force. Giving
the chip its own vocabulary — 已覆盖, 已自定义 — would invent a third term for a
two-valued fact and force the reader to map it back onto the tab they just pressed.

**Extreme data** `[derived]`: the hop list scrolls once its content exceeds the body,
and the dialog height is content-driven up to that point — nothing in the frame pins a
list height, so the scroll threshold belongs to the implementation, not to this spec.
The mono effective id truncates from the middle. A chain of one hop still shows the
ordinal `1` — the ordinal is the position, not a plurality marker. 顺序 #n is omitted,
not blanked, on hops that entered by override, because such a hop has no position in
the source order to report.

---

### 1.3 Frame 03 `qZhJ3` — Source-order drawer (per backend)

**The question it answers:** *for one backend, when several sources could serve the
same model, who goes first?* One ordered list, scoped to one backend, governing every
model on it that has not been individually overridden.

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
| Close `fUvS9` | 15px, `#FFFFFF59` |
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
ink** (§1.0): it means *first in this order* — the position the resolver starts from —
and it says nothing about which source is carrying traffic at this moment. The badge
moves only when the order is edited `[derived]`.

**This drawer must not claim live supply, and the reason is grain, not caution**
`[derived]`. The order is one list per backend; resolution is per model. A resolver
walking this order skips a source that is cooling, out of quota or unhealthy, and skips
a source whose inventory lacks the requested model — so two models on the same backend
routinely resolve to different sources, and neither has to be rank 1. Frame 08 draws
exactly that: ChatGPT is Codex's first source and is paused, while aihub is the one
actually answering. A backend-level surface that inked rank 1 as 「supplying right now」
would therefore be wrong on the one frame in this set where the distinction is visible,
and wrong silently — the badge would look confident and describe nobody.

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
| 取消 / 关闭 | Leave without saving | yes | Close, discarding uncommitted moves |
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

**Shortening the order is a guarded change, and it reuses the one confirmation this
product has** `[contract]`. The drawer saves the whole order in one request (§4.6's
`{hops:[...]}` `PUT`), so a save that drops sources is not a different kind of change
from any other that would remove hops: it comes back `409 {error, would_remove_hops,
would_interrupt}` and is completed by re-sending with `force`. The surface that renders
that envelope is already drawn, once, in §1.6 — this drawer starts no second one, and
保存顺序 is not disabled to avoid the case. Two confirmation surfaces for one envelope is
how the two begin disagreeing about what `would_interrupt` means.

**There is no mode, and the drawer has no ownership state** `[frame]` `[spec]`. Every
element on this surface is either part of one stored order or an action that edits it:
two sections, the rows in them, and a foot that commits or discards. The order the user
sees is the order that will be walked, and the only way it changes is that somebody
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
at a determinate position by the same transaction that adds the source, and that position
is returned and rendered — so 「新来源不会自动排进来」 is no longer true, and the state it
warned about no longer exists. A user who has just added a source needs to know *where it
landed*, which belongs to the end of the add flow, not to a drawer opened later.

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

**Copy** — namespace `models.hub.order.*`

| Key | 中文 | English |
| --- | --- | --- |
| `title` | {{backend}} · 来源顺序 | {{backend}} · Source order |
| `subtitle` | 从上往下,挑第一个能用的来源。 | Top to bottom — the first usable source is the one that answers. |
| `section.ordered` | 排在链里 | In the chain |
| `section.ordered.note` | 拖动排序 | Drag to reorder |
| `section.heldOut` | 未排入这条顺序 | Not in this order |
| `action.include` | 排进来 | Add to order |
| `action.exclude` | 移出 | Remove from order |
| `empty.noEligible` `[derived]` | 这个后端还没有可用来源。 | No source is available to this backend yet. |
| `empty.ordered` `[derived]` | 这条顺序现在是空的。把下面的来源排进来。 | This order is empty. Add a source from below. |
| `cancel` | 取消 | Cancel |
| `save` | 保存顺序 | Save order |

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
  configuration meaning *this backend uses none of these sources*, which frame 01 already
  renders as 没有可用来源, and refusing to save it would trap a user who genuinely wants
  that in a drawer they cannot leave without undoing their work.
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

**Element inventory**

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| head | `添加 {vendor} 订阅` + `host / plan` | vendor | close | Dismiss |
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
- on a second pass for the same account, the already-added channel stays in place and
  reads as already added rather than disappearing `[derived]` — a dialog that silently
  drops an option looks like a different dialog, and the user was told to come back here.

This is the item most likely to be implemented from the pixels alone, and getting it
backwards in either direction costs something real: checkboxes would promise a
one-press both-channels action the engine never receives, and a radio group whose
second pass hides the taken option would make the hint's instruction unfollowable.

**State machine**

| State | Entry | Exit |
| --- | --- | --- |
| Default | Opened | The recommended option is pre-selected; selecting the other replaces it |
| Second pass `[derived]` | Re-opened for an account that already has one of the two channels | The taken option reads as already added; the remaining one is selectable |
| Awaiting sign-in | 去登录 pressed | OAuth completes → source created; user abandons → Dismissed |
| OAuth failed | Provider or engine failure; classified `needs_action` `[spec §4.5]` | Retry, or 取消 |
| Engine unavailable | Gateway not running and gateway-upstream was chosen | 重试 once the engine recovers; 取消 → dismiss, nothing bound `[derived]` |
| Already bound | This account is already another source `[spec §4.1]` | Sign in with another account; 取消 → dismiss, nothing bound `[derived]` |
| Loading | — | Not applicable: nothing is fetched before the dialog opens |

`[derived]`: choosing 登录为网关来源 while the engine is down must fail **before**
the browser hand-off, with 「网关没有响应,请重试」 — sending someone through an
OAuth flow that has nowhere to land is the most expensive possible way to report
that the engine is down.

**All three failure rows keep the same foot** `[derived]`. The dialog's foot is 取消 /
去登录 (`[frame]`), and a failure replaces the message, not the buttons: 去登录 becomes
重试 and 取消 stays exactly where it was. So each of the three has a way out that binds
nothing — a property worth stating here rather than three times, because the reason is
one reason. The two that are not OAuth failures are the easy ones to forget: an engine
that is down and an account that is already taken are both conditions the *dialog* cannot
fix, and leaving them with only a forward exit would trap a user behind someone else's
state.

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
| `subtitle` | {{host}} / {{plans}} | {{host}} / {{plans}} |
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
| `error.oauthFailed` `[derived]` | 登录没有完成。可以重试。 | Sign-in did not complete. You can retry. |
| `error.engineDown` `[derived]` | 网关没有响应,请重试 | The gateway is not responding. Please retry. |
| `error.alreadyBound` `[derived]` | 这个账号已经是一个来源了。换一个账号登录。 | This account is already a source. Sign in with a different one. |

The four error strings are `[derived]`, not `[spec]`: the failure *classes* are the
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

**Extreme data** `[derived]`: vendor name and plan list are interpolated, so both
locales must survive a long vendor name without wrapping the head into the body;
the ToS note is per-vendor content, not a shared component — a second vendor with
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

**State machine**

| State | Entry | Exit |
| --- | --- | --- |
| ① Default | Dialog opened | Add pressed → ②; 拉取型号 pressed → ②′ |
| ② Adding | Add pressed | Success → dialog closes into 06; classified failure → ③; undetermined interface → ④; identified but no inventory → ⑤ |
| ②′ Pulling, **Pull origin** `[derived]` | 拉取型号 pressed, or 重试 pressed from ③′ / ④′ / ⑤′ | Success → the inline model count in ①; classified failure → ③′; undetermined interface → ④′; identified but no inventory → ⑤′ |
| ③ Failure, **Add origin** | A probe run *as part of Add* classified the failure | 重试 → ② |
| ③′ Failure, **Pull origin** `[derived]` | A probe run by 拉取型号 classified the failure | 重试 → **another 拉取型号, not ②** |
| ④ Interface undetermined, **Add origin** | Reachable **and** authenticated, response shape matches no known interface | Pick a hint + 重试 → **probe again in the hinted order** → identified: persist and close; still undetermined: back to ④ with the attempt as evidence |
| ④′ Interface undetermined, **Pull origin** `[derived]` | The same outcome, from 拉取型号 | Pick a hint + 重试 → **probe again in the hinted order, still as a pull** → identified: report the inventory inline in ①, **persisting nothing**; still undetermined: back to ④′ |
| ⑤ Identified, inventory unavailable, **Add origin** `[frame]` `d6bFlX` | The probe proved the protocol with a real response, **and** the model fetch came back unusable | 重试 → re-run **the fetch only**, not the whole add; 仍要添加 → persist the source with its proved protocol and an empty inventory, close into 06 |
| ⑤′ Identified, inventory unavailable, **Pull origin** `[derived]` | The same outcome, from 拉取型号 | 重试 → re-run the fetch as a pull |
| Empty | — | Not applicable: a form has no empty state |
| Credential-invalid | Auth failure is one of ③'s three causes | As ③ |
| Engine unavailable `[derived]` | Gateway not running | Add is blocked with `fail.engineDown`; the form keeps its values |

**Origin is an axis, not a state, and it is the whole reason this table has primed
twins** `[derived]`. 添加 and 拉取型号 run the *same* probe, so every outcome the probe
can produce is reachable from either button and renders identically. What differs is
never the pixels; it is exactly two things, and they are the same two for every twin:

- **重试 repeats the operation that failed, not a different one.** From a pull, every
  retry is another pull.
- **取消 returns to where the operation started, and this is the only place that says
  so.** A pull is an optional operation *inside* the dialog, so its 取消 returns to ① with
  the form's values intact; an add is what the dialog is for, so its 取消 dismisses. This
  holds for the in-flight states exactly as it does for the outcome states: ② dismisses,
  ②′ returns to ①. A cancelled in-flight add has its transient credential revoked
  server-side (`[contract]` AC-26); a cancelled pull has nothing to revoke, because a
  pull writes no credential.
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
rather than earning keys of its own. What differs is the same two things as every other
twin — 取消 lands on ① instead of dismissing, and nothing is persisted, so there is no
transient credential to revoke on the way out.

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

**Copy** — `models.hub.addKey.*`

| Key | 中文 | English |
| --- | --- | --- |
| `title` | 添加 API Key | Add API key |
| `subtitle` | 「添加」时自动连一次:认出接口 + 拉取型号列表 | Add connects once: it identifies the interface and fetches the model list |
| `field.name` | 名称(可选) | Name (optional) |
| `field.baseUrl` | Base URL | Base URL |
| `field.baseUrl.hint` | 粘贴任何中转 / 聚合 / 自建服务的地址即可,Avibe 会自己认出接口 | Paste any relay, aggregator or self-hosted address — Avibe identifies the interface itself |
| `field.apiKey` | API Key | API key |
| `test` | 拉取型号 | Fetch models |
| `test.hint` | 可选 ·「添加」时会自动拉一次 | Optional · Add fetches once anyway |
| `submit` | 添加 | Add |
| `adding` | 连接中… | Connecting… |
| `adding.detail` | 连上 + 认出接口 + 首次拉取型号列表 · 通常 1–3 秒 | Connect, identify the interface, fetch the model list · usually 1–3s |
| `fail.subtitle` | 认出接口是「添加」的前置条件 · 先修好凭据再重试 | Identifying the interface is a precondition of Add · fix the credential, then retry |
| `fail.auth` | 鉴权失败:401 Unauthorized | Authentication failed: 401 Unauthorized |
| `fail.auth.detail` | 检查 API Key 是否有效 | Check whether the API key is valid |
| `fail.address` `[derived]` | 地址不对:404 Not Found | Wrong address: 404 Not Found |
| `fail.network` `[derived]` | 网络不通:连接超时 | Network unreachable: connection timed out |
| `fail.engineDown` `[derived]` | 网关没有响应,请重试 | The gateway is not responding — try again |
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
| `inventory.reason.rateLimited` | 上游限流,清单没返回 | Upstream rate limit — the list was not returned |
| `addAnyway` | 仍要添加 | Add anyway |
| `success.title` | 成功 → 弹窗关闭,直接进入「来源详情 · 型号管理」 | Done → the dialog closes and you land on Source details · Models |
| `success.detail` | 型号列表、重新拉取、推理强度档位都在那里维护 | The model list, refetch and reasoning tiers are all maintained there |
| `cancel` | 取消 | Cancel |

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
`api.md` freezes the source PATCH body as exactly `{display_name?, base_url?}` —
「Metadata only.」 — with no protocol field. The rebuilt string ends 「保存后不可更改」 — the frame moved onto the
contract's side. **E-2 is closed, and this string is now its whole surface.** The other
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

---

### 1.6 Frame 06 `wItw4` — Source detail · model management

**The question it answers:** *which models does this one source have, which of
them do I actually want, and what reasoning tiers does each accept?* Nothing else
— chain membership is set on 01/02, ordering on 03.

**Element inventory**

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| `iGcAi` back icon | — | route | yes | Return to 01 |
| `sugad` source bar | 36×36 identity tile, source name, **state dot + state label** (使用中 in the drawn state) + 型号列表更新于 {{time}}, mono `host · N 个型号` | source state `[spec §4.5]` | 重新拉取 / 添加模型 | Refetch / append an editable row |
| `myA8k` header | 型号 ID (250) · 录入 (84) · 推理强度 (470, with info) · fill spacer | static | no | — |
| `OM5PH` row | model id, entry-kind pill, tier chips, overflow icon | one model | tiers, overflow | Edit tiers / row menu |
| `p2JwTz` tiers | chips, or 未设置档位 + `+ 添加档位` | `reasoning_efforts[]` `[contract]` FC-03 | yes | Enter edit mode |
| `eVavA` tiers (editing) | removable chips + text input + 回车添加 · 任意文本 | local edit → one saved mutation on the source's whole inventory `[contract-gap]` G-8 | yes | Add / remove a tier |
| `nN4TZ` manual row | editable id input, 手动添加 pill, tier affordance, 取消 / 添加 | local draft | yes | Commit or discard |
| `Q83BF` add row | 添加模型 + when to use it | — | yes | Append a manual draft row |
| `tF3Bh` footnote | scope of this page; that tiers are yours to type; that the interface type is identified at add time, fixed, and neither shown nor editable here | static | no | — |

**There is no per-model on/off on this page, and that is the design** `[frame]`. An
earlier version of this section described an 接入 column with a toggle per row; the
frame has neither — three columns and a fill spacer, no switch anywhere. The reason
is D-9: a model's participation is decided by the routing chain that resolves it, so a second
per-model boolean here would be a second owner of the same fact, and the two would
disagree the first time a chain changed. What the page owns is the *inventory* —
which models this source has, and what tiers each accepts.

That is also what makes **G-3** a gap in the record rather than a missing control on
this frame `[contract-gap]`. **The frame already answers it, and the answer is that a
discovered row carries no retire control at all.** Removal is scoped to manual models,
and the frame says so on the surface that does the removing: `Qp6FI`'s subtitle reads
「aihub · 只有手动添加的型号能移除」. A row's overflow icon opens a menu no frame here
draws, and nothing in this file puts 「移除这个型号」 in it.

The scoping is a decision, and the reason is the consequence rather than the control. A
discovered row exists because a refetch found it, and the refetch rule two paragraphs
down is a diff, not a replacement — so a removal that writes nothing durable is undone by
the next successful 重新拉取, and the user watches a row they deleted come back. Closing
that means a retention marker on the discovered-model record, and §0.5 records why three
further rules would have to come with it. No rule in this file requires the control.

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

That structure is not specific to removing a model, and the surface is not either. It is
the `409 {error, would_remove_hops, would_interrupt}` envelope rendered once: the count
pill and the row list are `would_remove_hops`, the hint line is `would_interrupt`, and
the destructive button is the `force` re-send. Every guarded change renders through it —
including §1.3's whole-order save — with the title naming the operation and the rows
naming the hops. A second surface for the same envelope would be a second reading of what
`would_interrupt` means, and the two would disagree the first time the envelope grew.

**Metrics** `[frame]`: source bar `fill_container` `padding [14,18]` `gap 14`
`radius 12` `$--surface` / `$--border`, identity tile 36×36 `radius 9`, status row =
5px dot + 12/500 (`$--mint` as drawn — the ink is the state's, see below) + 11
`#FFFFFF8C`, mono line 10.5 JetBrains Mono
`#9BA3B8B3`, both actions 95 wide (添加模型 `$--mint`). Table `fill_container`
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
it shows a `$--mint` dot and 使用中. But a source can be on standby, cooling, paused or
`needs_action` when its detail page is opened — `needs_action` most of all, because a
dead credential is *why* someone comes here — and a bar hard-coded to 使用中 tells that
user their key is fine on the one screen they opened to fix it. So the dot and the label
both derive from the source's state, in the vocabulary D-21 already fixes for state
text:

| Source state `[spec §4.5]` | Ink | Key |
| --- | --- | --- |
| Supplying | `$--mint` | `sourceDetail.status.inUse` |
| Standby | `$--muted` | `upstream.state.standby` |
| Cooling | `$--gold` | `upstream.state.unavailableRetry` |
| Supply paused | `$--gold` | `legend.unavailable` |
| `needs_action` | `#FF6B6B` | `sourceDetail.status.credentialInvalid` `[derived]` |
| `error` | `#FF6B6B` | `sourceDetail.status.error` `[derived]` |

Four of the six keys already exist and are reused unchanged, because this bar is a
second rendering of a state the upstream card already words — two vocabularies for one
state is how they drift apart. The two new ones are the two states no frame words
anywhere, and the upstream card renders **these** keys rather than wording them a second
time. The 「· 型号列表更新于 {{time}}」 suffix survives every state untouched: it reports
the inventory's age, not the source's health, and a dead credential does not make an
already-fetched list any older.

**The mapping is total over `state.status`, and `error` is the row that makes it so**
`[spec §4.5]`. §1.1's state table already names a per-source `error` — an unclassified
failure, the residual class the four named ones do not cover — and a source in it can be
opened here like any other. A table that stopped at `needs_action` would leave the bar
undefined on a reachable state, and the drawn state is 使用中, so the undefined case
falls through to *this source is fine* on the one screen the user opened because it is
not. That is the same failure the paragraph above rejects for `needs_action`, one class
further out. `error` therefore takes rose like `needs_action` — both mean *a person has
to act* — but a **different word**, because they need different acts: 凭据失效 tells you
to re-authenticate, while 异常 deliberately claims no cause. Naming a cause the product
did not classify would be worse than admitting it has none; what the user can still do is
drawn either way, since 重新拉取 is enabled in every state.

**No repair control is drawn here, and that is deliberate.** A third source-scoped
action would be exactly the invention the 重新拉取 rule below forbids. Repair has one
owner — the upstream card, where a `needs_action` source must stay visible and
repairable — and `iGcAi` returns there. A stated
cause plus one tap beats a second repair control that can disagree with the first.

**The 录入 pill is a second witness for D-19's neutral pair** `[frame]`. 自动拉取
renders `#FFFFFF0A` / `$--border` / `$--muted`; 手动添加 renders `#FFFFFF14` /
`$--border` / `$--foreground`. Same shell, one step of contrast, and the brighter one
is the one the user put there — identical to 02's 3 跳 versus 自定义链. Neither pill
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

**State machine**

| State | Entry | Exit |
| --- | --- | --- |
| Ready | Source detail loaded | — |
| Empty (no models) | Discovery returned nothing and nothing was added by hand | Manual add, or a successful refetch |
| Refetching | 重新拉取 pressed | New list arrives → Ready (diffed, see below); failure → Error |
| Row · tiers editing | Tier area activated | Enter commits a tier; blur / Escape exits |
| Manual draft | 添加模型 pressed | 添加 commits; 取消 discards |
| Error (refetch failed) | Refetch rejected | The **previous list is kept**; the bar carries the failure |
| Not supplying | Source is `standby` / `cooldown` / paused `[spec §4.5]` | The source starts supplying again — the page's own states are unaffected, only the bar changes |
| Credential-invalid | Source is `needs_action` `[spec §4.5]` | The credential is re-validated. The bar states the cause; **the repair control is not drawn on this frame** — see the bar-state rule above |
| Unclassified error | Source is `error` `[spec §4.5]` | The source leaves `error`. The bar reads 异常 and claims no cause; the table and both actions stay live, exactly as in Credential-invalid |

Five rules:

- **Refetch preserves what the user authored, and a model it stops advertising is
  marked where the user chooses models — never silently skipped** `[derived]`
  `[contract]`. Manually added models survive a refetch, and AC-26 requires rediscovery
  to preserve the user-edited `reasoning_efforts` list plus `display_name` and
  `discovered_at` `[contract]` — all stored fields, so all survive. The harder case is a
  model a chain still *references* that a **successful** refetch no longer returns. This
  table is the discovered set, so its row goes; what must not happen is that the chain
  quietly stops working. The rule, in one sentence: **一条链引用的型号,若在所有来源上
  都不再供应,必须在型号菜单上可见地标出,不得静默跳过。** The contract already carries
  it — §4.6 retains the exact hop and keeps its live non-runnable reason visible until the
  Source recovers or the user changes it, and §4.4's `model_supply` projects
  `chain_length: 0` into 「无来源可供」 on the menu. **No new field, no new mechanism, and
  no stale row on this frame.** The marker itself renders on the model menu, which no
  frame in this document draws, so **this file states nothing about how it renders** —
  doing so would specify a surface no frame here draws. It is covered by the AC ledger
  instead, and nothing about this frame changes.

  *What that costs, said here too because this is the page where it is felt.* The source
  a model came from is exactly what this table used to tell you, and after the refetch it
  no longer can: the row is gone with no trace that it was ever here. A user diagnosing
  「无来源可供」 reads the marker on the menu, opens the model's chain, and finds the
  retained hop naming a source whose inventory no longer lists that id — three surfaces
  to answer a question one page could have answered if the inventory remembered its own
  past. The trade is deliberate: nobody gets silently unconfigured by a vendor changing
  `/models`, and the price is that the explanation is assembled rather than read. §0.5
  records the ruling; G-3's retirement half stays open and is unaffected by it.
- **Empty state** `[derived]` keeps the header row and the add row and shows
  「这个来源没有返回型号。可以手动添加,或重新拉取。」 An empty table with a live
  add row is the shortest path out.
- **Credential-invalid** `[derived]` keeps the whole table visible and
  read-only-ish: you can still see what you had configured. Hiding the inventory
  because the key expired destroys the only copy of the user's intent.
- **重新拉取 is the only source-scoped action drawn here, and it must never absorb the
  recovery test** `[frame]` `[contract]` AC-26. The frame draws 重新拉取 and 添加模型
  and nothing else — no connectivity test. 05's 拉取型号 and 06's 重新拉取 are the same
  operation at two moments, which is why they share a verb and neither is called a test.
  The contract nonetheless keeps two distinct
  source-scoped operations: the recovery test, which `probe-result.schema.json` covers
  for saved Sources explicitly `[contract]` FC-07, and rediscovery, which rewrites the
  inventory. They differ in cost — one tells you whether the endpoint is alive, the
  other can change what is on screen — so if a recovery test is later surfaced on this
  page it takes its own control and its own label. Pressing 重新拉取 to find out
  whether a key still works is the exact conflation AC-26 forbids.
- **The tier control form is this file's call** `[contract]`. AC-26 fixes the data
  (`reasoning_efforts: string[]`, editable for discovered and manual models alike, no
  default item, no prefill, no selected state) and then explicitly defers the control
  form to `design.pen`. So the chips-plus-freetext-input treatment in the metrics
  above is normative, and 「未设置档位」 is the real empty state rather than a
  synthesized default — see D-5.

**Copy** — `models.hub.sourceDetail.*`

| Key | 中文 | English |
| --- | --- | --- |
| `status.inUse` | 使用中 | In use |
| `status.credentialInvalid` `[derived]` | 凭据失效 | Credential invalid |
| `status.error` `[derived]` | 异常 | Error |
| `status.listUpdated` | · 型号列表更新于 {{time}} | · model list updated {{time}} |
| `summary_one` | {{host}} · {{count}} 个型号 | {{host}} · {{count}} model |
| `summary_other` | {{host}} · {{count}} 个型号 | {{host}} · {{count}} models |
| `action.refetch` | 重新拉取 | Refetch |
| `action.addModel` | 添加模型 | Add model |
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
| `footnote` | 这里只管「这个来源有哪些型号」。型号走哪条路由链,在网关模块里改。档位自己填,两种录入方式都一样。接口类型在添加时认出并固定,页面上不显示、也不能改。 | This page answers only "which models does this source have". Which routing chain a model takes is set in the gateway module. Tiers are yours to type, the same for both entry kinds. The interface type is identified when the source is added and fixed there — it is neither shown on this page nor editable. |

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
affordance would advertise an operation the API cannot perform — the source PATCH body
is exactly `{display_name?, base_url?}`, 「Metadata only.」 `[contract]` `api.md`.

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
edge case; tier strings are free text and are neither validated nor
case-normalized (D-5); the count `sourceDetail.summary` interpolates must be plural-safe
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

**State machine**

| State | Entry | Exit |
| --- | --- | --- |
| Nominal | No source is unavailable | A head source becomes unavailable → Takeover |
| Takeover | Head unavailable **and** a next candidate is serving | Recovery → Nominal, on the next turn `[spec §4.3]` |
| Exhausted | Head unavailable and **no** candidate remains | Any candidate recovers |
| Multiple takeovers | More than one backend rerouted | Each recovers independently |
| Loading / Empty / Unreachable | — | As §1.0 |

`[derived]`: **Exhausted is not takeover and must not borrow its ink.** With no
candidate left the group shows 「没有可用来源」 and the wire layer draws **no violet
path** — violet means *rerouted*, and painting it where nothing was rerouted would
report a recovery that did not happen. The gold demotion still applies, because the
head source really is paused; what is absent is the thing that replaced it. The header
pill counts backends in takeover, so at zero it is absent, not `0 处接管中`.
`[contract]` AC-30 fixes the counting rule across grains, and an exhausted chain
contributes zero to it.

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

### 1.8 Frame 09 `UVR97` — Direct-only home (the first screen after upgrading)

**The question it answers:** *I just upgraded and I have never used a gateway — what
is this page, and why would I want one?* It is the same page as 01, in the state where
nothing has been adopted yet.

**Display condition** `[derived]`: every backend is in 直连 mode and no source has been
added. This is a *state of the Models surface*, not a separate onboarding route — the
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
it** `[derived]`. Two sentences already written above decide it. 09 is 01 「in the state
where nothing has been adopted yet」, and the 你会多出三件事 card disappears once 「the
user has now made [that decision] at least once」 — in the retained-source state they
have. And 09 draws no upstream column, so rendering it here would hide sources the
product just promised to keep, leaving no surface to inspect or delete them on. So the
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
onboarding card around for the two backends that have not moved. What is genuinely
first-run-only is the 你会多出三件事 card, and it is correct that it disappears: it
argues for a decision the user has now made at least once.

**What the shell drops, and why** `[frame]`. Frame 09 renders the header but **no tab
strip, no three-column `cols` track, no dispatch rail, no wire layer and no legend.**
There is no gateway module to occupy the second column, no supply relations to draw, and
therefore no inks to explain. An empty gateway column with a placeholder would be worse
than its absence: it would assert that a thing exists here and is currently broken,
which is the opposite of the truth.

**The page and the module have different names, and neither is 「模型网关」** `[frame]`.
Measured across all nine frames: the page title is 「模型」 (`oPD53` here, `YkN0P` on 01,
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
On 09 all three are `#FFFFFF0A` with a muted glyph. Nothing is supplying anything yet,
so there is no relation for a colour to describe, and colouring the tiles would spend
the page's only semantic inks on decoration before the user has learned what they mean.

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

**Copy** — namespace `models.hub.direct.*`

| Key | 中文 | English |
| --- | --- | --- |
| `card.current` | 当前:直连 | Currently: direct |
| `card.current.sub` | 每个 Agent 后端各自用自己的登录,直接连厂商。 | Each agent backend uses its own login and connects to the vendor directly. |
| `pill.direct` | 直连 | Direct |
| `backend.claude.detail` | 用你的 Claude 订阅登录 | Signed in with your Claude subscription |
| `backend.codex.detail` | 用你的 ChatGPT 订阅登录 | Signed in with your ChatGPT subscription |
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

**Copy states outcomes, not architecture** `[frame]`. Each of the three benefits names a
thing that happens to the user (the session survives; one key covers three backends; you
choose per model) rather than a mechanism that makes it happen (failover, a local proxy,
a route table). This frame is where that rule was hardest to hold, because
the honest description of the gateway *is* a mechanism, and the user has no reason to
care about it yet.

**Extreme data** `[derived]`

- **A backend the user does not have installed** is omitted from the list rather than
  shown disabled; the pill count follows the list. `{{count}}` is derived from the rows
  rendered, never hard-coded to 3.
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
| `head` `ojIOL` | `padding [16,20]` `gap 4`; title 15 / 700; close 15px `#FFFFFF59` |
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
| 切换到网关 | Commit | yes | Switch **this backend only**; the page becomes 01 |
| Failure strip `[derived]` | `fail.title` over `fail.detail`, in the Failed state only | no | — |

**The dialog names the exit by location, not by promise** `[frame]`. The second
可以撤回 bullet reads 「回退入口:这一页的 Claude Code 卡片 → 切换到直连」. "You can
change this later" is the standard phrasing and it is nearly useless: it is exactly what
a user hears before spending twenty minutes failing to find the control. Naming the
control and the surface it lives on costs one line and converts a reassurance into an
instruction. This is the same reasoning as D-14 — a way out that cannot be found is not
a way out.

**Copy** — namespace `models.hub.adopt.*`

| Key | 中文 | English |
| --- | --- | --- |
| `title` | 把 {{backend}} 切换到网关 | Switch {{backend}} to the gateway |
| `subtitle` | 只影响 {{backend}},其余后端保持直连 | Affects {{backend}} only; the other backends stay direct |
| `section.effects` | 会发生什么 | What will happen |
| `effects.1` | 你现在的 {{vendor}} 登录成为第一个来源,继续优先使用 | Your current {{vendor}} login becomes the first source and keeps priority |
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

**State machine** `[derived]`

| State | Entry | Exit |
| --- | --- | --- |
| Default | 切换到网关 pressed on a backend row | 取消 → dismiss unchanged; 切换到网关 → Committing |
| Committing | Confirm pressed | Success → dialog closes, page becomes 01 with this backend in 网关 mode; failure → Failed |
| Failed | The mode change did not persist | The dialog stays open, states the failure, keeps 取消 enabled and 切换到网关 retryable |
| Dependency missing `[derived]` D-26 | Runtime `health` is `not_installed` (§1.0) | The confirm gains one line naming the component and roughly how long installing it takes, and the primary becomes 安装并切换; pressing it installs, then starts, then switches — one press, three steps, reported as one outcome. 取消 is unchanged |

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

**Extreme data** `[derived]`: `{{backend}}` and `{{vendor}}` are interpolated in six
places, so the dialog must survive the longest backend name without reflowing its foot;
bullets wrap rather than truncate, because a consequence half-shown is worse than one
that costs a line. The dependency line adds a seventh interpolation (`{{component}}`)
and an eighth (`{{duration}}`), and both are rendered inside the bullet, never as a
separate banner.

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
is visible even if that pushes the group past three rows.
*Why:* a collapse exists to hide the boring. If it can hide the one row that needs
attention, the compression has inverted its own purpose.

**D-8 — The user does not perceive the supply mechanism.** Protocol, channel and
injection are absent from every surface, with **no exception**. The one place a
protocol name is ever rendered is 05 state ④'s selector, and that is not a display of
the mechanism — it is a question asked at the single moment the product cannot answer
it itself.
*Why:* the mechanism is the product's job, and surfacing it invites decisions the
user has no basis to make. Earlier revisions carved out an exception — a quiet badge on
frame 06 for sources whose interface the user had hinted — on the reasoning that hiding
somebody's own decision makes it unfindable. The ruling deleted the badge instead
(§1.6, E-2), and the reasoning it replaced that with is better: the hint was never a
decision the user can revisit, so making it findable buys nothing and teaches everyone
else that protocols are their problem. A rule with no exceptions is also the only kind
that can be checked as a set equality.

**D-9 — Chains are derived, not hand-wired, and the order they derive from belongs to
one backend.** One order per gateway-mode backend, plus a per-model override. E-1 is
closed in favour of the behaviour spec (§0.6) and frame 03 was rebuilt accordingly.
*Why:* N sources × M models of manual wiring is a configuration surface nobody can
hold in their head, and the order is the only part users actually have an opinion
about. Per-backend rather than global because eligibility already differs per backend:
a global list would have to render entries that cannot apply, and a user cannot form an
opinion about an ordering whose members are conditional.

**D-9a — A backend in 直连 mode exposes no order surface at all.** No 来源顺序 button
on its group head, and the drawer is unreachable for it.
*Why:* a direct backend consults no source order, so the editor would edit a list
nothing reads — the most expensive kind of dead control, because it looks like it
worked. This is the same rule as D-16 applied to configuration rather than to display:
do not render a value the system does not hold.

**D-10 — A source outside a backend's order is held out of *that order*, not excluded
from the product.** Frame 03's held-out section reads 未排入这条顺序 · 自定义链仍可指名
and is never hidden while such a source exists.
*Why:* the earlier 不参与排序 phrasing asserted something much stronger and false — the
same source may lead another backend's order, and a per-model custom chain can name it
directly. A label that overstates a scope teaches a wrong model of the system, and this
one taught the exact model E-1 was resolved against. The section still exists to answer
"why isn't this source in the list" *before* it is asked. `[spec §4.1]`

**D-10a — An ownership transfer shows the way back on the surface that takes it.**
Frame 02's per-model chain is the one place a user takes ownership of a derivation:
choosing 自定义 stops the backend's source order from maintaining that model's chain, and
the dialog carries 恢复跟随来源顺序 as a first-class exit, set apart from 取消 / 保存.
*Why:* the cost of taking ownership is invisible and deferred — the chain stops tracking
what changes elsewhere — so it has to be stated where it is incurred, not discovered a
month later. And an ownership transfer with no return path is a one-way door built by
accident. §1.3's order drawer takes no such transfer, so it needs no such exit; what it
lost when S-1 deleted its 自定义 mode is recorded there as a real loss, not answered here.

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

**D-15 — Every failure state keeps 取消.**
*Why:* an error whose only affordance is a mutation forces a decision the user may
not be equipped to make yet. Leaving is always a legitimate answer.

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
/ `$--foreground`) — 06's 自动拉取 versus 手动添加 is the precedent, and 02's 自定义链
follows it.
*Why:* found the hard way. 03's API Key pill and 02's 自定义链 pill had both drifted to
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
a glyph is never a status. Frame 09 is the check: with nothing yet supplying anything,
all three tiles go neutral, because there is no relation for a colour to describe.

**D-21 — The state-text layer and the wire layer are separate vocabularies, and a colour
means different things in each.** Wires: cyan = 原生, mint = 网关供给, violet =
接管, `#FFFFFF26` = 已启用 · 当前未被使用, gold = 暂不可用 / 供给已暂停. State text:
mint = 使用中 / 正常, gold = 降级 / 暂不可用 / 冷却, rose = 需处理 / 异常 / 无可用来源,
muted = 备用, cyan = 原生 provenance only, violet-tint `#7C5BFFCC` = a takeover hop
label.
*Why:* a wire describes a *relation between two things*; state text describes *one
thing's condition*. Collapsing them into one legend forces both to be wrong somewhere —
gold as a relation means supply stopped, gold as a condition means degraded, and those
are not the same claim. §1.0's ink table is the single place both are written down.

**D-22 — A group head's status line is `<mode> · <supply_status>` whenever a supply
status exists, and the mode word alone when none does.** 网关 · 正常, 网关 · 降级,
网关 · 等待重试, 网关 · 已中断 — and bare 直连, because a direct backend has no gateway
supply whose health could be reported. §1.0's C-6 is the total mapping from the
`supply_status` enum, `null` included, and it is the only place the strings are fixed.
*Why:* mode and health are independently variable and users confuse them constantly —
"is it on the gateway" and "is it working" are different questions, and a single word
answers whichever one the reader happened to be asking. The `null` branch is not an
exception to that; it is the same rule reaching a state with nothing to report, and an
earlier revision that rendered 直连 · 正常 was inventing a health verdict about a supply
path that does not exist. Reporting 正常 for something the product is not doing is how
a status line stops being evidence.

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
`mode: direct`, it renders only as the mode word in a group head's status line, and it
is never an ink. 原生 is a property of a **hop**: it means `native_cli`, it is the cyan
relation ink, the legend key 「原生」, and the word in an upstream group or kind label.
The compound 「原生直连」 renders nowhere.
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

**D-28 — A backend-order surface reads the stored chain; a source card reads
`adopted_by`. Neither substitutes for the other.** Frame 03's drawer rows and frame 01's
per-backend ordering read the persisted `hops` array — 「Hop order, Source identity, and
model mapping are user-visible configuration」 `[contract]` — while the Source card's
「供给…」 line consumes `adopted_by`, grouped by backend, de-duplicated, and combined
with the current runnability projection `[contract]` FC-05.
*Why:* the two diverge on an ordinary page, not only in edge cases, and they diverge in
**both** directions. `adopted_by` is de-duplicated by backend and carries no position, so
a backend supplied by the same source at two different menu models collapses to one entry
— read into the drawer, several rows become one and the order the drawer exists to edit is
the first thing lost. In the other direction the chain retains a hop that a later
inventory or process change annotates non-runnable, so reading it into the card would
claim a supply relationship that is not currently happening, which is exactly what
`adopted_by` combines runnability to avoid. Same noun, two projections, and the surface's
own question decides which one is true for it.

*This decision was re-derived at `ca45aeb6`, and the field it originally named is gone.*
It used to read 「a backend-order surface reads `order_enrolled_by`」, with a divergence
argued from enrolment-without-adoption. S-1 deletes enrolment: `order_enrolled_by` and
Source order are both in the contract's explicit absence list now, so the old sentence
named a field no implementation can read. The decision survives because what it actually
governs is *which projection answers which question*, and both projections still exist
under new names — but it survives as a re-derivation, not as a citation that quietly kept
pointing at a deleted field. Worth recording, because nothing in the review flagged it:
a citation to a deleted name reads exactly like a citation to a live one.

**D-29 — The page is 「模型」, the module is 「来源与网关」, and 「模型网关」 is never
rendered.** The project's name for this work is not a string in the product.
*Why:* the plan documents call the whole effort 模型网关, and carrying that onto a
surface produces two visible strings a level apart sharing a word — a page named 模型网关
containing a tab named 模型网关 — which reads as though the tab *is* the page. The
frames already do this correctly; the rule is written down so a lane reading the plan
files does not "fix" the page title to match them. Note that the design file's frame
names do contain 模型网关: they are canvas labels for the author, and D-17 already says
a frame's shell is not the shipped shell.

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
