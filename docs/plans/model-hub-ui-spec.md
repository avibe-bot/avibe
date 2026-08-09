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

All nine frames are 1440×1100 Dark. Light and mobile variants are not drawn yet;
§3 states which acceptance items therefore cannot be checked yet.

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
   (AC-1…AC-31) and the `FC-01…FC-14` final-contract handoff. §3 below does not
   duplicate, restate, or extend either.
3. `model-hub-contracts/` — the frozen wire shapes the two above are landed as.
4. This file — layout, copy, state reachability, interaction feedback.
5. `design.pen` — the pixels. Where this file and the frame disagree on a number,
   the frame is right and this file must be corrected, *unless* the number is
   marked `[derived]`.

This file **references anchors and never restates spec content**. If you want to
know what a chain is, read §4.3 there; this file only says where it is drawn.

**Verification basis.** Every anchor and every `[spec]` / `[contract]` claim below
was checked against `docs/model-hub-v3-local-gateway` @ `176b41b7` — the open head
of the spec lane's PR #1215 — **not** against `master`, whose §3, §4.1, §4.2, §4.6
and §5 have all been superseded there. A reader on `master` will find some anchors
missing; that is the expected state until #1215 lands, and this file must not merge
before it does.

The basis moved during this round: it was `7984aabf`, and five commits later the
ledger runs to **AC-31**. Three of what that added is visible-layer material —
AC-29 is not, but **AC-30** (takeover is derived, and `exhausted` renders none of
takeover's visual semantics) and **AC-31** (Direct is a mode and the first state of
an existing install; Native names a hop and never a mode) both land directly on
frames 08, 09 and 10. Re-reading them is not bookkeeping: two of the three
frame-versus-contract conflicts in §0.6 exist only at `176b41b7` and would have been
invisible at the old basis. A UI spec pinned to a stale head of the document it is
supposed to agree with is the failure it exists to prevent.

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
describes the intended surface, and is **not** an acceptance requirement: no `UI-n`
depends on one, and where a frame draws an affordance that sits on a gap, §3 says so
explicitly rather than quietly requiring it.

| # | Surface | Missing | Verified absent at `176b41b7` |
| --- | --- | --- | --- |
| G-3 | 06 model inventory | a way to retire a *discovered* model from a source's inventory, **and a place to remember that it was retired** | `api.md`'s `DELETE /api/models/custom-models` 「removes only the named manual model」; no other inventory-shrink route is user-initiated, and `source.schema.json`'s `models` carries no per-model retained flag |
| G-7 | 06 model inventory | a marker that survives a reload for a model a chain still references but a **successful** refresh no longer advertises | `api.md` defines a successful refresh as replacement of the discovered model set; `source.schema.json` has no retained/stale field, so the retained row and a currently discovered row are indistinguishable after a reload |

**G-3 has two halves, and the second one is the reason no `UI-n` may require the
first.** A user-initiated retirement needs a route (half one) *and* a durable marker
saying this discovered id stays retired (half two) — otherwise the next inventory
refresh re-adds it and the control reads as broken rather than absent. The contract
represents neither. §1.6 therefore describes the affordance and §3 checks only that
the surface does not *claim* the capability; the retention half is handed to the AC
ledger in §0.7. Requiring half one without half two is how a checklist starts
certifying a control that cannot keep its promise.

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

**G-3's second half and G-7 are one missing field, asked for from opposite
directions.** G-3 wants to remember that a discovered id *stays out* after the user
retired it; G-7 wants to remember that a discovered id *stays in* after upstream
dropped it. Both fail for the same reason — a discovered model has no per-model record
that survives the next refresh, because the refresh replaces the set. Naming them as
one field is the useful escalation: two independent boolean flags bolted onto the model
list would make refresh semantics depend on which flag was written last, whereas one
per-model user-intent record answers both and keeps replacement as the default for
everything nobody touched. That framing is offered to the spec lane, not decided here.

G-3 and G-7 are the two real gaps left, both additive, both listed in §0.7 for routing
into the AC ledger. Neither is decided here. This lane owns the visible layer, and
inventing a persistence model to make a drawn control defensible is exactly the kind of
quiet scope grab that produces two disagreeing authorities.

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
match it, and at `176b41b7` the contract ruled against the control twice over: the UI
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
  is the reason §3 checks only that no surface claims the capability rather than
  requiring the control.
- **A model a chain still references, that a successful refresh stops advertising,
  needs the same missing field.** `api.md` defines a successful refresh as replacement
  of the discovered set, so the row that ought to survive marked-as-stale is
  indistinguishable from a live one after a reload. This is G-7, and §1.6 now states it
  as a gap rather than as a rule. **The recommendation is to answer both with one
  per-model user-intent record**, not two flags: retirement and retention are the same
  question — *did a human take a position on this id?* — and one record keeps
  replacement as the default for every id nobody touched, while two flags make refresh
  semantics depend on write order.

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
  `order_enrolled_by`; source cards read `adopted_by`; **neither field may stand in for
  the other**. D-28 carries the rule and the reason.

One earlier item left the same way: takeover-count agreement across grains landed as
**AC-30** at `176b41b7`, which states takeover is a projection of the chain and requires
a fixture where a chain with no runnable hop renders 「no takeover badge, connector
color, or other takeover visual semantics」. UI-32 cites it instead of standing on its own.

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
| Legend | Colour → meaning | static, but see UI-10 | no | — |

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
  engine had crashed. Activating the pill opens the same install confirm that D-26
  specifies for 切换到网关 — which names the component and its rough duration before
  anything is downloaded — so the two entry points cannot diverge on what installing
  means.
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
  value. See D-3 and UI-20: a surface that cannot prove a fact must say so.
  Recovery offers the same start action as Not started.
- Partial: only the sub-tree that failed degrades. A failed supply payload must
  not blank the source inventory, which loaded fine.

**Shared copy** — namespace `models.hub.shell.*` / `.upstream.*` / `.gateway.*` /
`.legend.*`. Both `ui/src/i18n/zh.json` and `en.json` must carry every key (they
are currently at exact parity, 3534 keys each; UI-11 keeps it that way).

**Count-bearing keys** `[derived]`. Every key interpolating `{{count}}` ships as an
i18next plural family — `<key>_one` and `<key>_other` — in **both** locale files,
never as a single bare key. Two consequences worth stating, because getting either
wrong is invisible until a user hits `count = 1`:

- English needs the distinction (`1 source`, not `1 sources`), and UI-14 tests
  exactly `0 / 1 / 2`. A bare key cannot pass it.
- Chinese has no plural categories, so `zh` never selects `_one`. It still carries
  both variants, with identical values, so that locale parity stays a plain set
  equality. A parity rule with a per-language exemption list is a parity rule that
  stops catching anything.

The count-bearing keys in this file are `shell.allDirect`, `upstream.count`,
`gateway.modelCount`, `gateway.collapse`, `chain.derived.hops`,
`sourceDetail.summary` and `takeover.pill` — seven, all under `models.hub.*`; each
appears below in its `_one` / `_other` form. This list is the right-hand side of
UI-14, so adding a `{{count}}` key anywhere under `models.hub.*` without adding it
here is what that item is built to catch.

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
never explain an ink that is not on screen, and can never omit one that is. UI-4 checks
the equality in both directions.

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
UI-9's partition was undefined on a drawn element. That is the same "partition with a
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
  is the single most consequential distinction on the surface — see D-6, D-21 and UI-8.
  Note the noun: cyan says 原生, a property of the hop, and says nothing about 直连,
  which is a property of the backend and renders only as the subtitle's mode word (E-4).
  A page can draw a cyan wire into a backend whose mode is 网关 — that is a native source
  enrolled in a gateway chain — and the two words must stay separable for that row to be
  readable at all.
- **Mint is dual, and that is fine, because the roles never collide on one
  element.** A tab underline is not claiming an upstream relation, and a wire is not
  claiming to be a control. Forcing mint down to one meaning would need a second
  accent hue for controls, which buys nothing and costs the brand a token.

The honest statement of the rule is therefore a partition, not a whitelist: mint
inking a relation/status element **must** mean gateway supply, and mint inking a
control element **must** mean active/selected/primary. UI-9 checks that partition.

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
restating it — UI-8 and UI-9 both do.

One consequence is worth naming because a fixture depends on it. Claude Code's tile is
cyan *and* Claude Code is native-direct in every frame that draws it, so no frame
separates the two readings for cyan the way OpenCode separates them for violet. That
D-20's tile stays cyan after Claude Code moves to the gateway is therefore an assertion
of D-20, not an observation of any frame `[derived]`. UI-33's all-gateway fixture is
the first artefact that will pin it, and UI-8 is written so that fixture can pass.

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
order and an editor there would edit a list nothing reads. D-9a states the rule and
UI-33 checks it as a set equality over the three groups.

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
cannot see is a source you cannot fix. (UI-19.)

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
to draw. UI-17 claims the message and the preserved shell here, and claims a live add
affordance only for the two lists that actually draw one.

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
is a different order from the one UI-28 checks, and the disagreement was invisible because
the two live in different sections. A concat is not an ordering rule; it is an ordering
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
| Wires | Generated from the supply-relation set, never hand-placed; the frame's four paths are an instance of that generator, not a fixed asset. (UI-30.) |

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
(UI-37.)

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
rule; UI-33 checks it as a set equality.

**Geometry** `[frame]`

| Element | Metric |
| --- | --- |
| Scrim `UA2Q1` | 1440×1100, `#05050BE0` |
| Drawer `hnsO5` | 460 wide, **full 1100 height**, right-anchored, `$--surface`, left border only |
| `head` `qNs0K` | `padding [18,20]`, vertical, `gap 6`, 125 tall `[frame]` — the only overlay head in the product that is not `[16,20]` / `gap 4`, because it stacks a title, a subtitle and the segmented control |
| Title | 15 / 700, `$--foreground`, + 13px info icon `#FFFFFF59` |
| Close `fUvS9` | 15px, `#FFFFFF59` |
| Subtitle | 11.5 / normal, `#FFFFFF73` |
| Segmented `i5B8qF` | `#FFFFFF0A`, radius 9, `padding 3`; segment radius 6 |
| — active segment | `#FFFFFF1A`, label 12 / 600 `$--foreground` |
| — idle segment | transparent, label 12 / 500 `$--muted` |
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
| Segmented 跟随推荐 / 自定义 | Who owns this order | yes | Switch ownership; see the state table |
| Ordered rows | The order, ranked from 1 | drag, and fully by keyboard | Reorder |
| Grip | Drag affordance | drag / Space | Grab and drop |
| 排进来 | Add a held-out source to the order | yes | Append at the end, then focus the moved row `[derived]` |
| 恢复推荐顺序 | Escape from 自定义 back to 跟随推荐 | yes | Discard the custom order; the list re-renders as the recommendation |
| 取消 / 关闭 | Leave without saving | yes | Close, discarding uncommitted moves |
| 保存顺序 | Commit | yes | Persist, close |

**Ownership state machine** `[frame]` + `[spec]`

| State | Entry | What the drawer shows |
| --- | --- | --- |
| 跟随推荐 | Default; no custom order stored for this backend | Server-computed order; segment 跟随推荐 active; no hint row |
| 自定义 | User reorders, or presses 排进来, or selects the segment | The stored order; segment 自定义 active; the hint row appears |
| 自定义 · 有新来源未排入 | A source was added while this backend was 自定义 | As above; the new source sits in the held-out section |

The hint row is a consequence, not a warning `[frame]`: 「顺序已改成「自定义」:新来源不会
自动排进来。」 It states what the user just bought — the recommendation stops maintaining
itself — and offers 恢复推荐顺序 as the way back. A 自定义 order with no escape hatch is
a one-way door, which is the failure D-10a exists to prevent.

**The held-out section is not an exclusion list.** Its label reads
「未排入这条顺序 · 自定义链仍可指名」 `[frame]`. A source outside this backend's order is
still a source: a per-model custom chain (frame 02) can name it directly, and it may be
in another backend's order. The earlier design read this section as 「不参与排序」, which
said something much stronger and false. UI-34 checks that the two sections partition the
eligible sources exactly.

**Keyboard operation** `[derived]`. Drag-and-drop is the drawn affordance; it is not the
specified one, because a reorder surface that only accepts a pointer is unusable by
keyboard and by assistive tech, and this drawer is the only way to express a preference
the resolver reads. Required bindings, on a focused row:

| Key | Effect |
| --- | --- |
| `Space` | Grab the row, or drop a grabbed row at its current position |
| `↑` / `↓` | Grabbed: move the row one position. Not grabbed: move focus between rows |
| `Escape` | Grabbed: cancel the grab and restore the pre-grab order. Not grabbed: close the drawer |
| `Enter` | On 排进来: append that source to the order and move focus onto the moved row |

Ordinals renumber contiguously from 1 after every move, grabbed state is announced
(`aria-grabbed` plus a live-region message naming the new position), and the order a
keyboard produces is byte-identical to the one a drag produces — they must write the
same value through the same commit path, not two paths that agree today. UI-23 checks
all four bindings.

**Copy** — namespace `models.hub.order.*`

| Key | 中文 | English |
| --- | --- | --- |
| `title` | {{backend}} · 来源顺序 | {{backend}} · Source order |
| `subtitle` | 跟随来源顺序的型号,按这个顺序挑第一个能用的来源。 | Models set to follow the source order use the first usable source in this order. |
| `mode.recommended` | 跟随推荐 | Recommended |
| `mode.custom` | 自定义 | Custom |
| `section.ordered` | 排在链里 | In the chain |
| `section.ordered.note` | 拖动排序 | Drag to reorder |
| `section.heldOut` | 未排入这条顺序 · 自定义链仍可指名 | Not in this order · a custom chain can still name it |
| `action.include` | 排进来 | Add to order |
| `empty.noEligible` `[derived]` | 这个后端还没有可用来源。 | No source is available to this backend yet. |
| `empty.ordered` `[derived]` | 这条顺序现在是空的。把下面的来源排进来,或恢复推荐顺序。 | This order is empty. Add a source from below, or restore the recommended order. |
| `custom.hint` | 顺序已改成「自定义」:新来源不会自动排进来。 | This order is now custom: new sources will not be added to it automatically. |
| `action.restore` | 恢复推荐顺序 | Restore recommended order |
| `cancel` | 取消 | Cancel |
| `save` | 保存顺序 | Save order |

**Extreme data** `[derived]`

- **13 rows**: `dbody` scrolls; the head with its segmented control and the foot with
  its two buttons stay pinned. The page behind the scrim does not scroll.
- **Zero eligible sources**: both sections are empty; the drawer shows one line —
  「这个后端还没有可用来源。」 — and 保存顺序 is disabled. The drawer still opens; a
  surface that refuses to open cannot explain why it is empty.
- **Empty order, held-out sources remaining** `[derived]`: the *ordered* section renders
  「这条顺序现在是空的。把下面的来源排进来,或恢复推荐顺序。」 and the held-out rows stay
  listed with their 排进来 buttons; 保存顺序 stays enabled. This is a different emptiness
  from the one above and needs saying, because it is reachable and the repair is already
  on screen: in 自定义 mode a newly added source lands held-out, and if the ordered
  source later stops being eligible for this backend it leaves both sections (UI-34), so
  an order can empty itself while a usable source sits one press away. The two exits are
  排进来 and 恢复推荐顺序 — the second is why 保存顺序 is not disabled here: an empty
  custom order is a real configuration meaning *this backend uses none of these sources*,
  which frame 01 already renders as 没有可用来源, and refusing to save it would trap a
  user who genuinely wants that in a drawer they cannot leave without undoing their work.
- **Exactly one source**: it renders at rank 1 with the mint badge, the grip is present
  but inert, and the drawer is still reachable — the order is trivially satisfied, not
  meaningless.
- **A `needs_action` source already in the order** keeps its rank and shows its cause;
  it is not silently dropped (UI-19).
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
(UI-26.)

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
state. UI-27 checks all three.

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
| `zVU7c` / `V6CtoF` 拉取型号 + hint | an optional early pull, which Add performs anyway | — | yes | Run the pull, render its result in place |
| `S0pOY2` 添加 | — | form validity | yes | Run the add action (connect + identify + fetch) |
| `OT0Xf` state ② | spinner, 连接中…, what is happening, 通常 1–3 秒 | in-flight | 取消 only | Abort |
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
| ① Default | Dialog opened | Add pressed → ②; 拉取型号 → the pull-origin twin of whatever the probe returns (③′ / ④′ / ⑤′), inline model count otherwise |
| ② Adding | Add pressed | Success → dialog closes into 06; classified failure → ③; undetermined interface → ④; identified but no inventory → ⑤; 取消 → ① (transient credential revoked server-side `[contract]` AC-26) |
| ③ Failure, **Add origin** | A probe run *as part of Add* classified the failure | 重试 → ②; 取消 → dismiss |
| ③′ Failure, **Pull origin** `[derived]` | A probe run by 拉取型号 classified the failure | 重试 → **another 拉取型号, not ②**; 取消 → ① |
| ④ Interface undetermined, **Add origin** | Reachable **and** authenticated, response shape matches no known interface | Pick a hint + 重试 → **probe again in the hinted order** → identified: persist and close; still undetermined: back to ④ with the attempt as evidence. 取消 → dismiss |
| ④′ Interface undetermined, **Pull origin** `[derived]` | The same outcome, from 拉取型号 | Pick a hint + 重试 → **probe again in the hinted order, still as a pull** → identified: report the inventory inline in ①, **persisting nothing**; still undetermined: back to ④′. 取消 → ① |
| ⑤ Identified, inventory unavailable, **Add origin** `[frame]` `d6bFlX` | The probe proved the protocol with a real response, **and** the model fetch came back unusable | 重试 → re-run **the fetch only**, not the whole add; 仍要添加 → persist the source with its proved protocol and an empty inventory, close into 06; 取消 → dismiss, nothing persisted |
| ⑤′ Identified, inventory unavailable, **Pull origin** `[derived]` | The same outcome, from 拉取型号 | 重试 → re-run the fetch as a pull; 取消 → ①. **No 仍要添加**: the foot is the ordinary two, because the user did not ask to add anything |
| Empty | — | Not applicable: a form has no empty state |
| Credential-invalid | Auth failure is one of ③'s three causes | As ③ |
| Engine unavailable `[derived]` | Gateway not running | Add is blocked with 「网关没有响应,请重试」; the form keeps its values |

**Origin is an axis, not a state, and it is the whole reason this table has primed
twins** `[derived]`. 添加 and 拉取型号 run the *same* probe, so every outcome the probe
can produce is reachable from either button and renders identically. What differs is
never the pixels; it is exactly two things, and they are the same two for every twin:

- **重试 repeats the operation that failed, not a different one.** From a pull, every
  retry is another pull.
- **取消 returns to where the operation started** — ① for a pull, dismissed for an add —
  and **a pull-origin state can never persist**. Where a state's Add-origin form offers
  a persisting exit, the pull-origin form does not offer it at all: ④′ reports its
  result inline instead of saving, and ⑤′ drops 仍要添加 and keeps the ordinary
  two-button foot.

Stating it as an axis rather than as a list of special cases is the point. An earlier
version primed ③ alone, which left ④ and ⑤ silently shared between the two origins —
and both of their success paths persist a source and close the dialog. A user who
pressed the optional button, picked a hint, and pressed 重试 would then have created a
source they never asked for, or lost the form to 取消; the same hole existed twice
because the fix had been written as a row rather than as a rule. 拉取型号 is labelled
可选 (D-4), and the promise that word makes is *nothing you do here commits anything* —
which is a property of the whole pull branch, not of one failure.

**The distinction has no visual carrier**, and that is what makes it worth stating so
precisely: the only way to get it right is to keep the origin in state, and the only way
to get it wrong is to reconstruct it from what is on screen. UI-35 checks the ③/③′ pair
and UI-39 checks that the non-persistence property holds across the whole branch.

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
no button. (D-3, UI-20.)

**This is the one screen in the product where the user supplies a fact the product
normally derives, and the frame goes out of its way to bound it** — one hint, affecting
one attempt, not stored as an answer. 「全产品唯一一处让你提示接口类型的地方」 is the
frame's own caption. UI-12 keeps that uniqueness checkable. At `176b41b7` the ledger
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
did not arrive. AC-27 at `176b41b7` puts the same thing from the contract's side:
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

**`UI-38` is the acceptance item this state produces**, and it checks the property
rather than the pixels: the set of dialog exits that persist a source equals the set
whose protocol came from an observed response. A build that lets ③ or ④ save, or
that refuses ⑤, fails the same item — which is the point of writing it as an equality.

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
product surface (UI-12). They are identifiers, identical in both locales, and they
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
was changeable later. At `176b41b7`, AC-27 says the opposite: after Save the stored
protocol is preserved byte-for-byte through retest, discovery, refresh, credential and
Base-URL replacement and restart, and 「changing protocol requires a new Source」; FC-12
confirms the source PATCH body is exactly `{display_name?, base_url?, force?}`, with no
protocol field. The rebuilt string ends 「保存后不可更改」 — the frame moved onto the
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
| `eVavA` tiers (editing) | removable chips + text input + 回车添加 · 任意文本 | local edit → `PATCH /api/models/custom-models` `[contract]` AC-26 | yes | Add / remove a tier |
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
this frame `[contract-gap]`. A row carries an overflow icon, so there is already a
place to put 「移除这个型号」; what there is no place to put is the *consequence*. A
discovered row exists because a refetch found it, and the refetch rule two paragraphs
down is a diff, not a replacement — so a removal that writes nothing durable is undone
by the next successful 重新拉取, and the user watches a row they deleted come back. The
missing piece is a retention marker on the discovered-model record: a per-model,
per-source fact that says 「this one was retired by hand」 and that rediscovery reads
before re-adding. Both halves are one gap because either half alone is worse than
neither — a control with no marker lies, and a marker with no control is unreachable.
G-3 is therefore stated in §0.5 as a single additive request, and no `UI-n` requires
the control without the marker.

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
owner — the upstream card, where UI-19 requires it — and `iGcAi` returns there. A stated
cause plus one tap beats a second repair control that can disagree with the first.

**The 录入 pill is a second witness for D-19's neutral pair** `[frame]`. 自动拉取
renders `#FFFFFF0A` / `$--border` / `$--muted`; 手动添加 renders `#FFFFFF14` /
`$--border` / `$--foreground`. Same shell, one step of contrast, and the brighter one
is the one the user put there — identical to 02's 3 跳 versus 自定义链. Neither pill
takes an accent hue, because 录入 records *where a row came from*, not whether
anything is wrong with it. UI-37 checks the two pairs together, which is the point of
checking it as a rule rather than per frame: one shared meaning, two independent
renderings, and a mismatch in either one is a defect in both.

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

- **Refetch preserves what the user authored; whether it preserves what the user
  *referenced* is not representable today** `[derived]` `[contract-gap]` **G-7**.
  Manually added models survive a refetch, and AC-26 already requires rediscovery to
  preserve user tier edits `[contract]` — both are stored fields, so both survive. The
  case this file cannot require is the other one: a model that a chain still references
  and that a *successful* refetch stops advertising. Keeping the row and marking it
  stale is the behaviour that does not silently unconfigure someone because a vendor's
  `/models` changed shape — but `api.md` defines a successful refresh as **replacing**
  the discovered set, and `source.schema.json` carries no per-model retained/stale
  marker, so after a reload nothing distinguishes a retained row from a currently
  discovered one. §3 therefore requires only the representable half; the marker is
  handed to the AC ledger in §0.7 with G-3's second half, which asks the same contract
  for the same missing field from the opposite direction.
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
subtractive branch of the conflict: the stored shape at `176b41b7` carries no
manual/automatic provenance marker (AC-27), so a badge conditioned on 「由你指定」 has
nothing to render *from*; and 「changing protocol requires a new Source」, so an edit
affordance would advertise an operation the API cannot perform — the source PATCH body
is exactly `{display_name?, base_url?, force?}` `[contract]` FC-12.

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
case-normalized (D-5); `{{total}}` must be plural-safe in English at 0 and 1 (UI-14).

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
pill counts backends in takeover, so at zero it is absent, not `0 处接管中` (UI-14).
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

Stated as the rule an implementation has to encode, it is a two-branch switch on one
predicate — *does any backend run through the gateway?*

| Predicate | Page title | Tab strip | Body |
| --- | --- | --- | --- |
| **No** backend is on the gateway | `oPD53` 「模型」 | **absent** | this frame, and it occupies the whole page |
| **At least one** is | 「模型」 (`YkN0P` on 01, `VaXos` on 08) | present: 「来源与网关」 · 「用量与额度」 `[frame]` | 01 — this frame is gone as a page |

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
a route table). UI-15 is the check; this frame is where it was hardest to hold, because
the honest description of the gateway *is* a mechanism, and the user has no reason to
care about it yet.

**Extreme data** `[derived]`

- **A backend the user does not have installed** is omitted from the list rather than
  shown disabled; the pill count follows the list. `{{count}}` is derived from the rows
  rendered, never hard-coded to 3 (UI-32).
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
(1440×1100, `#05050BE0`) plus `dialog_UkQqY`. UI-21's method applies here too (UI-36).

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
| 会发生什么 ×4 | The consequences of adopting | no | — |
| 可以撤回 ×3 | That it is reversible, and precisely where the exit is | no | — |
| 取消 | Leave unchanged | yes | Dismiss; nothing is written |
| 切换到网关 | Commit | yes | Switch **this backend only**; the page becomes 01 |

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
UI-15 can check as a set equality.

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

**D-10a — 自定义 is an ownership transfer, and it always shows the way back.** Choosing
自定义 stops the recommendation from maintaining the order; the drawer says so and
offers 恢复推荐顺序.
*Why:* the cost of going custom is invisible and deferred — new sources silently stop
being added — so it has to be stated at the moment it is incurred, not discovered a
month later. And an ownership transfer with no return path is a one-way door built by
accident.

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
UI-4 admits exactly this one deviation and no other.

**D-24 — 原生 and 直连 are two different properties of two different things, and neither
may appear in the other's sentence.** 直连 is a property of a **backend**: it means
`mode: direct`, it renders only as the mode word in a group head's status line, and it
is never an ink. 原生 is a property of a **hop**: it means `native_cli`, it is the cyan
relation ink, the legend key 「原生」, and the word in an upstream group or kind label.
The compound 「原生直连」 renders nowhere.
*Why:* they are independently variable, and the compound quietly asserts they are not.
A native source can be enrolled in a gateway backend's chain — cyan wire into a 网关
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

**D-28 — A backend-order surface reads `order_enrolled_by`; a source card reads
`adopted_by`. Neither substitutes for the other.** Frame 03's drawer rows and frame 01's
per-backend ordering read enrolment; the Source card's attribution line reads adoption,
de-duplicated by backend and combined with live runnability `[contract]` AC-22, FC-05.
*Why:* the two diverge on an ordinary page, not only in edge cases — a source enrolled
in a backend's order that wins no route because of capability filtering is enrolled but
not adopted. Reading adoption into an order surface would make a source the user
deliberately ranked vanish from the list they ranked it in; reading enrolment into a
source card would claim a supply relationship that is not happening. Same noun, two
projections, and the surface's own question decides which one is true for it.

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

## 3. UI acceptance checklist

**How to read this list.** Each item is a **property that must hold**, not a
prohibition. A blacklist ("X must not appear") is never complete but always looks
complete, so it certifies nothing; a property either holds or names its own
counterexample. Each item is checkable by someone who has never seen this project,
in minutes, with the stated action and the stated criterion.

**Fidelity items are checked geometrically, never by eye.** Method, once, for all
of them:

1. In the design file, read the node's resolved box and style —
   `Get("<nodeId>", (n, ctx) => …)` via the `pencil` MCP `execute` tool. `ctx.bounds`
   is **parent-relative**; accumulate a stack across `ctx.depth` for absolute
   coordinates.
2. In the running UI, read the same element's `getBoundingClientRect()` and
   `getComputedStyle()`.
3. Compare box (±1px), padding, gap, radius, border width, colour, font size and
   font weight.

**Do not compare screenshots for fidelity.** A missing font substitutes metrics
and shows up as drift in size, weight and spacing all at once, so a screenshot
diff will send you hunting a token bug that does not exist. Screenshots are for
overall impression only. Two font families are load-bearing here: Inter for prose,
JetBrains Mono for identifiers, URLs and keys.

**Boundary.** This list covers only what is visible: layout fidelity, copy, state
reachability, interaction feedback. Behaviour invariants — persistence, event
fan-out, resolver precedence, schema constraints — belong to
`model-hub-implementation.md` §8 (AC-1…AC-31) and are neither duplicated nor
extended here.

**No item depends on a `[contract-gap]`.** Where a frame draws an affordance whose
persistence does not exist yet (§0.5: G-3, G-7), the item below says so and
checks only the part that is real — usually that the affordance is absent rather than
present-and-broken. An acceptance list that requires something unbuildable does not
raise the bar; it trains people to sign off on items they could not actually verify.

**No item depends on an open conflict either — and there are none left.** All five
E-n in §0.6 are ruled, and the list came through the rulings without a retraction,
which is the property it was written for: E-1 and E-3 moved the frames, E-2 deleted a
surface (06's protocol badge), E-4 rewrote three strings, and E-5 added a state that
was then drawn. Only §1's prose changed. That was not luck — it is what stating items
over *what is drawn* and quantifying over *sets* rather than wordings buys. UI-12 is the
one that came closest: it names the set of surfaces that render a protocol name, and
E-2's ruling changed the set's membership without changing the item.

**Every item names the §1 section that owns each fact it uses, and restates no metric.**
This is the newest rule here and it was bought with four rounds of review. An item that
re-prints a padding, a column width or a string becomes a second specification of the
frame, and two specifications of one frame drift — silently, because each one looks
self-consistent. Every drift found so far had this exact shape: UI-6 carried its own
overlay metrics, UI-7 its own column widths, UI-17 its own message strings, UI-22 its own
control inventory, UI-27 its own failure list. When the frames were rebuilt, §1 was
re-measured and §3 was not, so the checklist began certifying an older design — and a
reviewer running it would have failed a *correct* build. So: an item may name a **domain**
by pointing at a §1 table, and may state a **property** that spans sections, and must read
every number and every string from §1 at check time. Where an item does carry a value, it
is because that value **is** the item's content and no §1 section owns it — UI-1's
384+72+632+16+16 = 1120, UI-28's six fixtures, UI-37's two colour triples — and those are
marked as such below. The test for a new item is one question: *if a frame changed, would
this item become wrong on its own?* If yes, it is restating instead of checking.

**Every item names its domain before it claims anything about it.** This is the
convention the whole list is written to, and it is the one that cost the most to
learn. An acceptance item is a quantified statement — *every* X has property P, or
the set of X *equals* S. Such a statement is only checkable if a reviewer can
mechanically produce the X's. Write the claim without bounding the domain and you
get a predicate that is true of the members you were picturing and undefined on the
rest; it reads as rigorous, and it certifies nothing. Worse, it is unfalsifiable in
the direction that matters: the reviewer who cannot enumerate the domain concludes
the item passed.

So each item below is stated as three parts:

- **Domain** — a mechanical filter that yields the exact members. "Every
  interactive element" is not a domain; "every element that some state table in §1
  assigns a disabled state" is.
- **Claim** — the property or set equality that must hold over that domain.
- **Check** — how a reviewer obtains both sides and compares them.

Three domain-exclusions are global, so no individual item restates them:

1. **Rows a state table marks 「不适用」 / "Not applicable" are in no domain.** They
   document that a state was considered and ruled out; requiring them to render
   inverts their meaning.
2. **A control is in a state's domain only if some state table assigns it that
   state.** Requiring a disabled state for a control nothing ever disables invents an
   unreachable state, and for the sole exit control it would contradict D-15.
3. **A `[contract-gap]` member is in no domain until its gap closes** (§0.5). Where a
   set equality would otherwise name one, the member is written as conditional and the
   condition is the gap id.

The set equalities (UI-9, UI-10, UI-12, UI-14, UI-27, UI-31, UI-33, UI-34) get the sharpest version
of this: the failure mode is not "the set is wrong", it is an equality whose
right-hand side was never enumerated. Where the right-hand side depends on the
fixture, the item **derives** it from the fixture rather than hard-coding a count —
a hard-coded count silently becomes a second, competing specification of the fixture.

### Layout fidelity

**UI-1 — The three-column skeleton has exactly the drawn geometry.**
*Check:* measure `cols` and its three children on the Models page.
*Criterion:* track 1120; upstream 384, rail 72, gateway 632; two gaps of 16;
384+72+632+16+16 = 1120 exactly.

**UI-2 — Page chrome matches the shell metrics.**
*Check:* measure `Main`, `header`, `tabs`, and the active tab.
*Criterion:* `Main` padding 36/40, gap 22; title 26/700 Inter; tabs 39 tall with a
1px `$--border` bottom edge; the active tab has a 2px `$--mint` bottom edge and no
other tab does.

**UI-3 — Every repeated card and row uses the drawn metrics.**
*Check:* measure one instance of each: upstream card, backend group, model row,
collapse row, dialog foot.
*Criterion:* 80 / radius 10; 616 / radius 12; 36 / radius 8; 24 with transparent
fill **and** transparent border; foot 61 with a 1px top border and `#FFFFFF05`
fill.

**UI-4 — Every colour on these surfaces resolves to a declared token, or to an
alpha composite over one, and the composite is named in §1.**
*Domain:* every computed `color`, `background-color`, `border-color` and SVG `stroke`
on the nine surfaces.
*Claim:* each value appears in §1.0's ink table or in that frame's metrics table.
*Check:* enumerate them and look each one up.
*Criterion:* every value has a row. An unlisted literal hex is a finding, whatever it
looks like. **Exactly one deviation is admitted:** the legend swatch for
已启用 · 当前未被使用 renders `#FFFFFF33` where the wire it stands for is `#FFFFFF26`,
per D-23. Any other mismatch between a swatch and its referent fails, and so does a
second exception added without amending D-23.

**UI-5 — Font role assignment is total.**
*Check:* for every text node, read `font-family`.
*Criterion:* identifiers, URLs, masked keys and request evidence resolve to
JetBrains Mono; all prose resolves to Inter; nothing falls back to a system font.

**UI-6 — Every overlay container matches its own geometry table, and the parts that
are shared are shared exactly.**
*Domain:* the five overlay containers §1 gives a geometry table for — 02's dialog, 03's
drawer, 04's dialog, 05's dialog, 10's dialog. Nothing else on the page is an overlay,
and a container with no §1 table is not in this item's domain (it is a §1 gap instead).
*Claim:* exactly three properties hold across the whole domain — body `padding 20`, a
foot at `padding [14,20]` `gap 8` with a 1px top border over `#FFFFFF05`, and a scrim of
`#05050BE0` at 1440×1100. **Head padding and body gap are not among them**, and neither
is any width. Every other metric is per container and is read at check time from that
container's own §1 table: §1.2 for 02, §1.3 for 03, §1.4 for 04, §1.5 for 05, §1.9
for 10.
*Check:* measure each of the five. Compare the three shared properties against this
item; compare everything else against the five §1 tables, opened while checking.
*Criterion:* the three shared properties hold on all five, and each container matches its
own §1 table exactly. 03 is the one full-height container, with a left border only.
*Why the shared set is this small, and why the rest is not reprinted here.* An earlier
version claimed head `padding [16,20]` and body `gap 14` for all five. Measured, 03's
head is `[18,20]` / `gap 6` and its body gap is 18, and 10's body gap is 16 — so a drawer
built to §1.3 failed UI-6 and a drawer built to UI-6 failed §1.3, the item and the frame
each certifying the other wrong. The first repair was to copy the true numbers into a
table here, and that repair failed the *next* round for the same reason the original did:
the copy went stale the moment §1 was re-measured. Three of five is not a majority worth
generalising from, and a private copy of the other two is not a fix — it is the defect
with better initial values. What is genuinely shared is three properties; everything else
has an owner, and this item's job is to send the reviewer to it.

**UI-7 — Frame 06's body rows align to its header on the three fixed columns, and
differ from it by exactly one trailing control.**
*Domain:* header `myA8k` and every `row_*` in the same table.
*Claim:* the three fixed columns hold the widths §1.6 states, identically in header and
body, at the gap and horizontal padding §1.6 states. The fourth cell is a
**`fill_container` spacer, not a fixed width**; body rows carry a fifth 16px `more`
control after it, so the body spacer resolves exactly `32` narrower than the header
spacer — the control plus one gap. Vertical padding differs between the two by one pixel,
per §1.6's two rows.
*Check:* measure `myA8k` and one `row_*`; read §1.6's geometry rows; compare the three
fixed cells to ±1px and compute the spacer difference.
*Criterion:* fixed columns match §1.6 and each other; the spacer difference is 32; the
`more` control is present on body rows and absent from the header.
*The `32` is this item's own content, and the column widths are not.* The difference is a
relationship between two cells that no §1 table states, because §1.6 describes each row
type on its own; it survives any re-measurement of the columns, which is what makes it
safe to write here. The widths are §1.6's, and an earlier version that copied them was
one re-measurement away from failing a correct build.
*Note.* An earlier version also asserted a fixed `110` fourth column in both, which the
rebuilt frame contradicts twice over — the cell is flexible, and header and body do not
resolve to the same width. Replacing `110` with "the remaining fill width" would have
kept the second error, since a single number cannot describe two cells that are designed
to differ.

### Semantic colour

**UI-8 — For every cyan-inked element, its subject is a native-direct supply
relation.**
*Domain:* every element whose computed colour, border or stroke is `#3FE0E5` or an
alpha of it, **minus backend identity tiles** — a filled 30×30 rounded tile carrying a
backend glyph, per §1.0's identity-ink paragraph and D-20. The exclusion is not a
convenience: an identity tile's hue is a per-backend constant, so under the all-gateway
fixture of UI-33 Claude Code's tile is still cyan while nothing on the page is
native-direct. Without the exclusion, UI-8 and UI-33 cannot both pass, and the item
that would fail is the one describing a page that is behaving correctly.
*Check:* enumerate the domain, then for each member name the entity it describes.
*Criterion:* every one is a native-direct source, its card, its wire, or its
「原生」 tag. One cyan element in the domain describing anything else fails the item.
*Also check the exclusion is not hiding a defect:* every excluded tile is 30×30 with a
glyph and sits in a backend group header. A cyan element that merely *looks* like a
tile — a card tint `#3FE0E50A`, a status chip — is in the domain and must still be a
native-direct subject.

**UI-9 — Mint, violet, gold, rose and `#FFFFFF26` partition by element role exactly as
§1.0's ink table says.**
*Domain:* every element whose computed colour, border, stroke or fill is one of those
five inks or an alpha of it. Nothing is excluded — unlike UI-8, this item's whole
content is that the roles cover the domain, so removing a member would beg the
question.
*Check:* classify each member by form factor alone, before reading any meaning:
**relation/status** (a stroke, a 5px dot, a status word, a supply pill, a 20×2 legend
swatch), **control** (an interactive affordance: tab underline, ordinal badge,
selection mark, focus ring, row wash, primary button), **identity** (a filled 30×30
rounded tile carrying a backend glyph), **advisory** (a static bordered note or result
strip that is not actionable: 04's ToS note, and all three of 05's result strips —
③ rose, ④ gold, the mint success note). Then read meaning.
*Criterion:* the four roles **partition** the domain — every inked element is in
exactly one, and no element is in none. Then, per role: a mint relation means gateway
supply, a mint control means active/selected/primary, and a mint advisory means
success; **violet on a relation means taken over and temporarily rerouted**; **gold on
a relation means supply paused because the source is temporarily unavailable, and gold
on an advisory element means warning emphasis**; **rose on an advisory element means
error emphasis, and on the reference fixtures no domain member is a rose relation/status
element** — the nine frames draw no source in `needs_action` or `error`, so a rose dot or
status word *on these fixtures* is a defect rather than a state. That last clause is
scoped to the fixtures on purpose: §1.6 maps two source statuses to rose, and on a fixture
that carries one of them a rose status word is the correct rendering, not a finding. What
UI-9 forbids is rose appearing where no rose-mapped state is in the data — which is
checkable only against the fixture, never against the screen; `#FFFFFF26` means a
connected-but-unused wire; and an identity tile carries its backend's constant ink and
asserts nothing (D-20).
*Why four and not two.* Each of the two additions was forced by an element the frames
require and the earlier partition had no home for. Identity came from OpenCode's violet
tile, which a two-role reading turns into a takeover that is not happening. Advisory
came from 04's ToS note and 05's state-④ strip, both explicitly noninteractive in §1.4
and §1.5: they are not relations and not controls, so under the earlier wording the
item was simply undefined on them — and an item that is undefined on the reference
design passes by default, which is the failure mode this whole section exists to avoid.
Neither role is a loophole; they are the reason the other two can stay strict.

**UI-10 — The legend keys and the ink classes actually rendered on the page are in
bijection.**
*Domain:* the wire layer of whichever frame is under test, on any fixture.
*Claim:* `{distinct strokes present in the wire layer}` equals `{inks named by legend
keys}`, as sets, on every fixture.
*Check:* enumerate the wire layer's distinct `stroke` values; enumerate the legend's
swatch inks; compare. **Derive both sides from the render — do not compare either side
against a number written here.** Per D-23 a swatch may differ from its wire in alpha
only; match on the ink class, not the literal.
*Criterion:* the two sets are equal on every fixture, including zero relations, where
both are empty and **no legend row renders at all**. An earlier version fixed the
nominal side at "3 keys", which is a second specification of the fixture: change the
fixture and a correct build fails, or worse, the count is kept passing by pinning the
legend to a static list — the exact defect this item exists to catch. Two consequences
survive: a takeover shipped without its legend entry fails, and UI-31's zero-relation
state stays satisfiable.

### Copy

**UI-11 — Every string on these nine surfaces resolves through an i18n key, and
`zh.json` / `en.json` have identical key sets.**
*Check:* render each of the nine surfaces with the locale forced to `en` and confirm no Chinese
text remains; then diff the two files' key sets.
*Criterion:* zero hardcoded literals in the components; the key-set difference in
both directions is empty (they are at parity today at 3534 keys each).

**UI-12 — The set of surfaces that renders an interface-protocol name is exactly
{frame 05 state ④ selector}.**
*Domain:* the rendered DOM of all nine surfaces, both locales.
*Claim:* the set of surfaces containing any of the three protocol strings is that
one-member set.
*Check:* search for the three strings; compare the hit set.
*Criterion:* exact set equality. Stated as an equality rather than a prohibition, so a
*new* surface that leaks a protocol name fails it too.
*The set stayed one member through a ruling that could have added one, and the way it
did is the point.* An earlier version wrote 06's quiet protocol badge as a conditional
member 「once G-4 closes」, on the reading that the provenance field was merely unbuilt.
At `176b41b7` AC-27 said the stored shape carries no manual/automatic marker at all —
not silence but a sentence pointing the other way — so the question became **E-2**, and
E-2 was ruled for AC-27: the badge, its tooltip and its edit entry are deleted from
frame 06, and §1.6 now renders no protocol anywhere. This item did not change. That is
the argument for writing a conditional member as an escalation rather than as scheduled
work: 「once G-4 closes」 reads as a commitment, so nobody would have escalated it, and
the equality would have quietly documented a surface that turned out never to be
allowed. The one member is now the whole truth, and a build that reintroduces the badge
fails here rather than passing on a technicality.

**UI-13 — Every product noun rendered on these surfaces has a row in
`model-hub.md` §3's vocabulary table, and uses the term that table marks required.**
*Check:* extract the nouns from the copy tables in §1 and look each up in §3.
*Criterion:* every one has a row, and where §3 marks a term **required**, the copy
uses that term and not a synonym. 网关 / Gateway is the required noun for the local
adaptation and routing module, so 「网关」 as the module heading, 「来源与网关」 as its tab
and 「网关供给」 as the supply phrasing all pass; a build that substituted 「Models」 or
「路由」 for them fails. **「模型网关」 is not one of the passing renderings** — it is the
plan documents' name for the whole effort and D-29 keeps it out of the product, so a
page titled 模型网关 fails this item rather than satisfying it. Any noun with no row is a
finding against whichever side is wrong — usually this file, occasionally the table.

**UI-14 — The set of count-bearing keys equals the set of keys shipping i18next
plural variants, and each is grammatically correct in English at 0, 1 and 2.**
*Domain:* keys under the `models.hub.*` namespace in `zh.json` and `en.json` — **not
the whole file**. The rest of the product predates this spec and is not this lane's to
certify; an unscoped grep makes this item fail on strings nobody here wrote, which is
how a real check gets marked flaky and then ignored.
*Claim:* within that namespace, `{keys interpolating {{count}}}` equals `{keys shipping
an i18next plural family}`, and each renders correctly in English at 0, 1 and 2.
*Check:* **flatten the JSON to dotted paths first, then filter.** Walk each locale file
recursively and emit one `path → value` pair per leaf; keep the pairs whose path starts
with the namespace; strip a trailing `_one` / `_other` to get stems. The left-hand set is
the stems whose value interpolates the count variable; the right-hand set is the stems
that have at least one suffixed sibling. Compare the two stem sets, then render each at
0, 1 and 2.
*The flattening is not a convenience — a text search here cannot work at all.* The locale
files store paths as nested objects, so no line in them contains the namespace as a
literal dotted string, and a raw `grep -o` for it matches nothing. Both sets come back
empty, the equality holds vacuously, and every plural defect in the namespace passes
while the item is marked run. That is worse than having no item: a check whose empty
output is indistinguishable from success removes the reviewer's ability to notice. So
whatever tool is used, **confirm both sets are non-empty before trusting the comparison**
— today each should hold seven stems.
*Criterion:* the two sets are equal — a count-bearing key with no plural family fails,
and so does a plural family nobody interpolates a count into. In `en`, no `1 models` and
no `1 source` mismatch; in `zh`, both variants exist and carry identical values.
`0 takeovers active` never renders because the element is absent at zero, not because
the string handles it. The right-hand side is **§1.0's list, which is the single place
it is maintained**: `shell.allDirect`, `upstream.count`, `gateway.modelCount`,
`gateway.collapse`, `chain.derived.hops`, `sourceDetail.summary`, `takeover.pill` —
seven keys, each `_one` and `_other` in both files, twenty-eight entries.
*The variable name is part of the check, not a detail.* i18next selects a plural form
from the `count` option specifically, so a key that ships `_one` / `_other` while its
value interpolates some other name renders whichever variant the default happens to be
and never varies. `sourceDetail.summary` was written `{{host}} · {{total}} 个型号` and
had exactly that defect: the suffix grep found it, the `{{count}}` grep did not, and the
two sets differed by one member for a reason that looked like a naming preference and
was actually a string that could not pluralise. It is `{{count}}` now. Any future
count-bearing key must interpolate `{{count}}` by that name — a second count in one
string means a second key, not a second variable, because only one of them could ever
drive the selection.

**UI-15 — Explanatory copy states consequences, not mechanisms or rationale.**
*Domain:* strings whose job is to explain or persuade — option descriptions, hints,
notes, benefit bullets, empty states, error sentences. **Not** labels that name a thing
the user must identify, choose between, or repair.
*Claim:* every string in the domain tells the reader what happens to them, and none of
them argues for a design decision — the arguments live in §2.
*Check:* read each string in the domain and ask, "does this tell me what happens to me?"
*Criterion:* no domain string names an internal mechanism as an explanation, and none
argues. Two strings failed this during the design pass and were rewritten; E-4's ruling
deleted a third clause from three more.
*The domain is bounded, and the boundary is the difference between naming a mechanism
and explaining by mechanism.* Applied to every string in §1, this item contradicts the
product: frame 05 state ④ must print the interface type, the probe order and the three
protocol identifiers — UI-12 *requires* that selector to render them — because the user
is being asked to supply a hint, and a hint they cannot read is not a question. Frame
02's chain labels are the same case: 跳, 覆盖, 来源顺序 are the product's own concepts,
the nouns UI-13 checks against the vocabulary table, and a user editing a chain is
manipulating exactly those things. **Copy that lets a person inspect or repair their
configuration is allowed to name the parts** — what UI-15 forbids is answering *why
should I choose this* or *what went wrong* with an internal mechanism, which is the
failure E-4 corrected: 「不经网关:额度用完不会自动接管」 explained a consequence by
naming a subsystem the reader has no stake in, and deleting the clause cost the sentence
nothing.

### State reachability

**UI-16 — Every state in §1's state tables is reachable, and each has a named
trigger.**
*Domain:* rows of §1's state tables, **excluding** rows whose exit column reads
「不适用」 / "Not applicable", rows marked `[contract-gap]`, and rows a §1 table marks
**undrawn**. Per the global exclusions: a 不适用 row records that a state was considered
and ruled out — 04's Loading row and 05's Empty row exist to say *a form fetches nothing*
and *a form has no empty state* — so demanding that they render inverts what they
document. An undrawn row records the opposite situation: the contract asks for a state no
frame draws, so this file names it and refuses to invent its copy. **Today the undrawn
list is empty**: 05's state ⑤ is drawn (`d6bFlX`) since E-5 was ruled, and 10's
dependency row is ruled and specified since G-6 closed into D-26, so both are ordinary
in-domain states now. Only the two 不适用 rows remain exempt. The exemption is itself
checkable in both directions — an undrawn row that *does* render means someone invented
the surface, and a row that stops being undrawn without leaving this list means the
exemption is stale, which is the failure this sentence exists to prevent.
*Claim:* every remaining row renders, from the trigger its entry column names.
*Check:* perform each entry condition directly, or serve the payload that produces it.
*Criterion:* every in-domain state renders. An unreachable in-domain state is either a
missing implementation or a spec row that should be deleted; both are findings. A 不适用
row that *does* render is also a finding, in the other direction.

**UI-17 — Every list has an empty state that keeps its frame, says which emptiness it
is, and offers the exit.**
*Domain:* the five lists these frames draw — the upstream source list, a backend group's
model rows, a source detail page's model table, and frame 03's two drawer sections.
*Claim:* at zero rows each keeps its head and footer and renders the message §1 names
for **that** list. Whether it also keeps a live way out depends on whether §1 draws one,
and the three cases are different:

- **An add control, kept live** — the upstream source list keeps 添加订阅 / 添加 API Key;
  06's model table keeps its header row and 添加模型.
- **An in-place exit, kept live** — frame 03's *ordered* section, when it is empty but
  eligible sources are still held out. §1.3 draws 排进来 on every held-out row, so the
  exit already exists on screen and the item requires it to work: the empty ordered
  section renders its message, the held-out rows stay listed, and 排进来 appends. This is
  the case an earlier version of this item got wrong.
- **Message plus preserved shell, and nothing invented** — a backend group's model rows,
  and frame 03 when **no** eligible source exists at all, so there is nothing to 排进来
  from. A build that invents a button in either place fails this item exactly as one
  that hides the list does.

*Check:* six fixtures, one per row below — zero sources at all; a backend whose every
candidate source is filtered out; a backend that resolves to zero model rows; zero
models on a source detail page; a backend order with an empty ordered section and at
least one held-out source; and a backend order with zero eligible sources.
*Criterion:* nothing vanishes, and the six emptinesses are **pairwise distinguishable** —
each renders the string its own §1 row names, and no two render the same string, because
they have different causes and different fixes:

| Fixture | Key | Where §1 states it |
| --- | --- | --- |
| No sources at all | `upstream.empty` | §1.0 |
| A backend with no source able to supply it | `gateway.group.emptySources` | §1.0 |
| A backend whose menu resolves to zero models | `gateway.group.emptyModels` | §1.0, state in §1.1 |
| A source detail page with zero models | `sourceDetail.empty` | §1.6 |
| A backend order that is empty with sources held out | `sourceOrder.empty.ordered` | §1.3 |
| A backend order with zero eligible sources | `sourceOrder.empty.noEligible` | §1.3 |

*The strings themselves are deliberately not reprinted here* — the keys are the stable
handle, and copying the six sentences into §3 is precisely how this item drifted before.
Two rows are worth the pointer anyway. The third is the one an earlier version left
unnamed: *no source can supply this backend* and *this backend has no models* are
different failures with different repairs, and one shared string sends the user to add a
source that is already there. The fifth is the one the previous round found missing
altogether — an empty order with a live 排进来 one row below it was being checked as
though it had no exit.

**UI-18 — Exactly one flow blocks on a wait, and every other load degrades in
place.**
*Check:* throttle the network and enter each surface; then press 添加 in frame 05.
*Criterion:* 05 state ② is the only modal wait, and it is cancellable; every other
surface renders its shell with per-region placeholders and never a full-page
spinner.

**UI-19 — A `needs_action` source renders as itself, in place, everywhere it
appears.**
*Domain:* the four surfaces that draw a source at all — the upstream card on 01, the
frame 03 order row, any chain hop naming it on 02, and frame 06's `sugad` source bar.
The headline says *everywhere*, so the domain has to be the places a source is drawn,
not a sample of them; 06's bar was the one the earlier three-surface list missed, and
it is the surface a user reaches *because* the credential died.
*Check:* serve one source in `needs_action` and inspect all four.
*Criterion:* present in all four, position unchanged, cause shown on each — including
06's bar, which renders 凭据失效 in `#FF6B6B` and not 使用中. The repair control appears
exactly once, on the upstream card, and is deliberately absent from 06 (§1.6). Absence
anywhere fails — including "helpfully" dropping it from the order.

**UI-20 — Any value the surface cannot currently prove renders as indeterminate.**
*Check:* stop the engine and reload 01; separately, fail a refetch on 06.
*Criterion:* derived columns show `—` and never a stale value; the refetch keeps
the previous list and puts the failure on the source bar; frame 05 state ④ has no
segment pre-selected and its primary button is disabled.

**UI-21 — Frame 08 is a state of frame 01, not a second layout.**
*Check:* diff the computed geometry of 01 and 08 for `cols` and its three children.
*Criterion:* identical boxes; all differences are inks, chips, text and one extra
wire — exactly the delta table in §1.7. Any box shift fails.

### Interaction feedback

**UI-22 — Every interactive element has hover and focus-visible; every element some
state table disables has a disabled style; every mutating control has pending.**
*Domain:* three different domains, which is the whole repair. Hover and focus-visible:
every interactive element on the nine surfaces. Disabled: **only** elements §1 actually
assigns a disabled state — today 05's 重试 in ④ before a hint is picked, 03's 保存顺序
with no eligible sources, and **frame 02's first hop `↑`**, whose dimmed treatment §1.2
states. Three controls, three reachable states. Frame 03 contributes no arrow at all:
§1.3's rows carry a grip and bind `↑`/`↓` on the row itself, so there is no per-row arrow
button to disable — the list boundary is expressed by the key doing nothing, not by a
dimmed control. Frame 04 contributes none either: a radio group has no zero-selected
state, so 去登录 is enabled from the moment the dialog opens. Pending: only controls that
mutate.
*Claim:* each element has the states of the domains it belongs to, and no others are
required.
*Check:* tab through each surface, then hover each control; then reach each of the four
disabled states from its table.
*Criterion:* focus is always visible without a mouse; the four disabled states use the
dimmed-token style (`#5BFFA059` for a dimmed primary; `#FFFFFF33` for a dimmed glyph in
a retained shell); a mutating control shows pending
and cannot be double-fired. **A control nothing ever disables needs no disabled style** —
the earlier universal phrasing demanded one for every control including 取消, which would
have contradicted D-15's requirement that the exit is always enabled. An acceptance list
that contradicts a decision it also contains is worse than a missing item, because it
forces the implementer to guess which one was meant.
*One member was wrong in the other direction, and it is the more instructive error.* An
earlier list named 「01's collapse row at zero hidden models」 as a disabled state. §1.1
renders the collapse row **iff** the hidden count is positive, so that state does not
exist: the item asked a reviewer to reach a screen the spec forbids, and the only way to
pass was to build the row the spec says not to build. Narrowing a domain is not the same
as enumerating it correctly — the narrowed set was still wrong, once by including an
unreachable member and once by omitting a reachable one, and the two errors cancelled in
the count. That is why this item now names the frame and section each member comes from.

**UI-23 — Reordering in frame 03 is fully keyboard-operable, with the bindings §1.3
names.**
*Domain:* frame 03's ordered rows and its 排进来 buttons.
*Claim:* the four bindings in §1.3's keyboard table work as written, and the order a
keyboard produces is identical to the one a drag produces.
*Check:* with no mouse: focus a row; `Space` to grab; `↑`/`↓` to move; `Space` to drop;
then repeat and press `Escape` mid-grab; then `Enter` on 排进来; then save and compare
the persisted order against the same arrangement made by dragging.
*Criterion:* `Space` grabs and drops, `↑`/`↓` move a grabbed row and move focus when not
grabbed, `Escape` cancels a grab and restores the pre-grab order (and closes the drawer
when nothing is grabbed), `Enter` on 排进来 appends and moves focus to the moved row.
Ordinals renumber contiguously from 1 after every move; the grabbed state is announced.
**Naming the keys is the point** — "a documented key moves a row" is not checkable by
someone who has not read the implementation, which is the standard §3 is written to.

**UI-24 — The collapse row is a real control that discloses and never hides an
active state.**
*Check:* activate it by keyboard; then serve a group where 5 of 15 models are
cooling.
*Criterion:* it is focusable and announces expanded/collapsed; the group shows all 5
non-nominal rows **plus 3 nominal baseline rows = 8 visible**, and the label reads
「还有 7 个型号」 — the number actually hidden, never `total − 3`.

**UI-25 — The tier editor accepts arbitrary text, commits on Enter, and never
supplies a value.**
*Check:* on a model with no tiers, open the editor and type an unfamiliar string.
*Criterion:* the empty state reads 未设置档位 with nothing preselected; Enter
commits the string as typed, without validation or case normalization; each chip
removes individually; leaving without typing leaves the list empty.

**UI-26 — Frame 04 is a radio group, and one 去登录 produces exactly one effect.**
*Check:* on the Claude dialog, read the initial selection, select 登录为网关来源,
inspect the accessible roles, then press 去登录 and count the sources created.
*Criterion:* 原生使用 is selected on open; selecting the other option deselects it;
the controls expose `role="radiogroup"` labelled by the dialog title, never checkbox
semantics; 去登录 is enabled throughout and there is no reachable zero-selected state;
exactly one source is created. Two sources from one press fails this item — the
two-channel path is two passes, which is what `hint.claude` says.

**UI-26a — A model's participation has exactly one owner, and frame 06 is not it.**
*Domain:* every control on the source-detail page.
*Claim:* no control on 06 changes whether a model participates in routing; that fact
is owned solely by the routing chain that resolves it (D-9).
*Check:* enumerate 06's controls — 重新拉取, 添加模型, the tier editor, the manual
draft row, the row overflow menu — and for each, name the field it writes.
*Criterion:* every one writes inventory or tier data. A per-model on/off switch, an
接入 column, or an overflow item that means "stop using this model" fails, because it
creates a second owner for participation that the chain does not read. Removing a
*manually added* model passes: that deletes the row itself, not its participation.

**UI-27 — Every failure state offers a way out that is not a mutation.**
*Domain:* enumerate it from §1 at check time — **every row of every §1 state table whose
entry condition is a failure**, with no exception and no list kept here. The domain splits
in two by a property §1 also states: whether the failing surface is a dialog or a page
region.
*Claim:* a failure inside a dialog carries a present, enabled control that leaves without
writing anything — 取消 or 关闭. A failure rendered inline on a page leaves the
surrounding page navigable, and needs no dismiss control, because the surface was never
captured and back-navigation *is* the way out.
*Check:* read the failure rows out of §1.1 … §1.9, then reach each one. In a dialog, look
for the control and press it, then confirm nothing was written. Inline, confirm the page's
own navigation still works and the failure has not blocked it.
*Criterion:* every row satisfies the half of the claim its surface selects. Two failure
modes fail this item, in opposite directions. **Requiring a 取消 button on an inline
failure is a finding, not a pass** — it adds a dismiss control to a page-level error strip,
which either does nothing or hides the error while it is still true; a failed refetch on 06
is the counterexample. And **a dialog failure whose only stated exit is the world
recovering is the other failure** — an engine that is down or an account already bound are
conditions the dialog cannot fix, and a forward-only exit traps the user behind them.
*This item used to carry its own list of nine failures, and the list was short.* It named
04's OAuth failure and neither of 04's other two. Enumerating a domain by hand, in a
document whose §1 already enumerates it, produces exactly this: an item that passes while
the surfaces it forgot go unchecked. Reading the domain out of §1 is not a stylistic
preference — it is the only version of this item that stays correct when a frame gains a
tenth failure state.

### Extreme data

**UI-28 — The collapse predicate is implemented as written in §1.1, including that
`N` is additive.**
*Check:* serve §1.1's six fixtures for one backend, verbatim: (12, 0), (12, 2),
(12, 5), (12, 12), (3, 0), (2, 1) as `(models, non-nominal)`.
*Criterion:* visible rows are 3, 5, 8, 12, 3, 2 and the collapse labels are 「还有 9 /
7 / 4 个型号」, none, none, none. A build that treats `N` as a total row floor produces
3, 3, 5 on the first three fixtures and fails — that is the specific mistake this
fixture set exists to catch. Then check the ordering half separately, because a build can
get every count right and still render them in the wrong sequence: on a fixture with one
overridden nominal model late in the menu and one cooling model early in it, the rendered
sequence must equal §1.1's `sorted` restricted to the visible rows. §1.1 owns that
comparator; this item only asserts that what renders is a *filter* of it, never a
reordering — the specific failure being a build that floats every non-nominal row to the
top of the group.

**UI-29 — Every unbounded string in a single-line identifier field has a stated
truncation rule and keeps its full value reachable.**
*Domain:* fields §1 renders on one line and whose content is an identifier the user did
not compose as prose — source names, base URLs, API keys, model ids. **Explicitly not
in the domain:** anything §1 states as wrapping. §1.6 says a tier list past its column
grows the row rather than clipping, and §1.9 says the consequence bullets wrap rather
than truncate. Those are not exceptions to be tolerated; they are the correct behaviour
for their content, and an item that failed them would be asking for the wrong product.
*Claim:* in the domain, the layout is stable and the value is recoverable.
*Check:* serve a 120-character source name, a 200-character base URL, and an
80-character model id.
*Criterion:* nothing in the domain overflows its container or reflows the layout; URLs
and keys truncate from the middle keeping both ends; the full value is in `title` or a
tooltip. **Truncation is the right answer only when the string is a handle, not a
sentence.** A user scanning a table needs a model id to occupy one predictable row and
be copyable in full; a user deciding whether to hand a subscription to the gateway needs
to read the whole consequence. The earlier phrasing — nothing reflows the layout, with
no domain — demanded truncation of both, which would have clipped exactly the sentences
someone is reading in order to decide.

**UI-30 — Every scrollable region has exactly one declared scroll owner, and its
pinned chrome stays pinned.**
*Check:* serve 20 sources, 6 backends, 60 models on a source detail page, and 13
rows in frame 03.
*Criterion:* the region named in §1 scrolls; module head/footer, table header and
drawer footer stay fixed; the page itself does not gain a second scrollbar.

**UI-31 — The wire layer is generated from the supply-relation set.**
*Check:* serve 0 relations, then 1, then a takeover, then two simultaneous
takeovers.
*Criterion:* path count equals the relation count; 0 relations draws nothing; each
gold path is individually traceable rather than stacked on another; no path is a
fixed asset that survives a relation disappearing.

**UI-32 — Numeric summaries agree with the rows they summarize.**
*Check:* on 06, compare the source bar against the table; on 08, compare the
header pill against the group chips; on 01, compare each group's model count
against its rows plus its collapsed count.
*Criterion:* `total` equals the row count, the pill count equals the number of groups
carrying a takeover chip, and visible + collapsed equals the group's stated model
count. 06's source bar prints one count and one timestamp and nothing else — any
additional derived figure there (a connected count, a latency) fails, because the page
holds no field it could come from (D-16), and AC-26 independently bans 「latency or
『last checked』 field, copy, fixture, or route heuristic」 while allowing exactly the
「型号列表更新于…」 the frame draws `[contract]`.

*And the pill counts a projection, not a stored flag* `[contract]`. AC-30 rules
takeover 「true exactly when the resolved chain's current hop is not its first hop and
the first hop is unavailable for a self-healing quota/cooldown reason」, derived from
§4.3's chain and adding no field. Two consequences are checkable here: a backend whose
chain has **no** runnable hop renders `interrupted` with 「no takeover badge, connector
color, or other takeover visual semantics」, so it must not be counted in the pill; and
a custom chain whose first configured hop is healthy and deliberately not native must
not be counted either, however much its shape resembles a fallback. Exhaustion looks
like takeover in the data and is the opposite fact for the user, which is why AC-30
spends a fixture on it and why counting rows instead of projecting them fails silently.

**UI-33 — The set of backends showing a 来源顺序 control equals the set of backends in
网关 mode.**
*Domain:* every backend group head in the gateway module.
*Claim:* `{heads with a 来源顺序 button}` equals `{backends whose status line begins
网关}`, as sets, on every fixture.
*Check:* on the reference fixture (Claude Code direct, Codex and OpenCode gateway),
then with all three direct, then with all three on the gateway: enumerate both sides.
*Criterion:* set equality every time. A direct backend showing the control fails (D-9a:
it would edit a list nothing reads); a gateway backend missing it fails too. Stated as an
equality rather than "direct backends must not show it", so a fourth backend added later
is covered without amending the item.

**UI-34 — Frame 03's two sections partition the backend's eligible sources exactly.**
*Domain:* for one backend, the sources the server reports as eligible for it.
*Claim:* `{ordered rows}` ∪ `{held-out rows}` equals that set, and the two are disjoint.
*Check:* serve a fixture where a source is eligible for two backends and ordered on only
one of them; open both drawers and enumerate.
*Criterion:* every eligible source appears exactly once in each drawer; a source held out
of backend A's order still appears in backend B's ordered section. Neither section may
silently drop a source — including a `needs_action` one (UI-19). This is the item that
catches a build reading the held-out section as a global exclusion list, which is what
the old 不参与排序 label asserted and D-10 corrects.

**UI-35 — A failed pull and a failed add are distinguishable, and 重试 repeats the
operation that failed.**
*Domain:* frame 05's states ③ and ③′ — the two ways to reach the red strip.
*Claim:* they render identically and differ only in what 重试 does: ③′ re-runs 拉取型号,
③ re-runs 添加.
*Check:* in ①, enter a bad key and press 拉取型号; when it fails, fix the key and press
重试, then inspect whether a source was created. Repeat the same sequence via 添加 and
confirm a source **is** created.
*Criterion:* the pull-origin retry creates **nothing** — the source list is unchanged —
and the add-origin retry does create one. This is the item most likely to be failed by a
build that is otherwise correct, because the two states are pixel-identical: the origin
has no visual carrier, so it has to live in state and cannot be reconstructed from the
screen. A build where the optional pull's retry persists a source fails: 拉取型号 is
labelled 可选 (D-4), and a retry that commits withdraws that promise at the moment the
user is least expecting it.

**UI-36 — Frame 10 is a state of frame 09, not a second layout.**
*Check:* diff the computed geometry of 09 and 10 for `body`, `topRow` and both cards.
*Criterion:* identical boxes; the only differences are the scrim and the dialog. Same
method and same reasoning as UI-21 — a confirm that re-lays-out the page behind it tells
the user they have gone somewhere, when they have not.

**UI-37 — Derived and user-set values of the same field are inked as D-19's neutral
pair, everywhere both appear.**
*Domain:* every pill whose value can be either system-derived or user-set. Two are
drawn: 06's 录入 (自动拉取 / 手动添加) and 02's chain state (N 跳 / 自定义链).
*Claim:* the derived member renders `#FFFFFF0A` / `$--border` / `$--muted` and the
user-set member `#FFFFFF14` / `$--border` / `$--foreground`, in the same shell.
*Check:* read the computed fill, stroke and text colour of all four pills.
*Criterion:* the four values are exactly those two triples. An accent hue on either
member fails — that would classify a provenance fact as a status (D-21) — and so does
a pair that differs only in text, because the contrast step is what makes the
distinction legible without reading. This item exists as a rule rather than as two
per-frame checks because the two renderings are independent implementations of one
meaning, and a divergence between them is the defect D-19 was written after.

**UI-38 — 仍要添加 persists the source with the protocol the probe proved, and with an
empty inventory.**
*Domain:* frame 05 state ⑤, the only place in the product where a source is written after
a step of adding it failed.
*Claim:* the row that lands is a complete source — its protocol is the one the probe
observed, never a guess and never the user's hint — and its inventory is empty rather than
absent, so 06 opens on a source with zero models instead of on a source in an
indeterminate state.
*Check:* reach ⑤ with a key whose probe succeeds and whose model fetch fails; press
仍要添加; then read the stored source and open its detail page.
*Criterion:* the stored protocol equals the one ⑤'s evidence strip named; the model list is
empty and 06 renders §1.6's empty row, not an error; a later 重新拉取 populates it without
any further identification step. **A build that stores the user's ④ hint as the protocol
fails**, and so does one that leaves the protocol unset and re-identifies on first use —
D-27's property is that a saved source always carries a protocol something observed, and ⑤
is the state that tests it, because it is the one place where the temptation to fill the
field from something weaker is real.

**UI-39 — Nothing reached from 拉取型号 ever persists.**
*Domain:* the three pull-origin states in §1.5 — ③′, ④′, ⑤′ — and every exit each of them
offers, including the successful ones.
*Claim:* the entire pull-origin branch is read-only. No exit from it creates a source,
mutates one, or leaves a credential on the engine; the only thing it can produce is
information rendered in ①.
*Check:* for each of the three, take every exit its §1.5 row lists — 重试 to success,
重试 to the same failure, and 取消 — and after each one enumerate the source list and the
stored credentials.
*Criterion:* both are unchanged in all cases, including the case where ④′'s hinted retry
succeeds and reports an inventory: that inventory renders inline and is discarded when the
dialog closes. **This item exists because the branch is defined by an invisible property.**
Origin is an axis §1.5 states, not something on screen — the pull twins are pixel-identical
to their add-origin counterparts — so no amount of looking at the surface can distinguish a
correct build from one that quietly commits, and the check has to be made against the
stored state instead. UI-35 checks the same property for ③′ alone; this item is the version
that covers the axis, which is what the earlier per-row phrasing missed twice.

**Total: 40 items (UI-1 … UI-39, with UI-26a).** Nothing is blocked on another lane.
No item is bounded by a contract gap or by an open conflict: the two gaps left (G-3,
G-7) touch no acceptance claim, and every conflict is now ruled — including E-2, which
UI-12 was written to survive either way and which landed on the side the item already
stated. Per the three global exclusions, none of them
asserts an unbuildable requirement. Light-theme and mobile variants are not drawn, so UI-1…UI-7, UI-21 and
UI-36 are checkable for Dark desktop only until those frames exist.

---

## 4. Anchors into the behaviour spec

This file never restates the behaviour spec. Use these anchors:

All section titles below were read at `176b41b7`, not on `master`.

| Question | Authority |
| --- | --- |
| What the product promises the user | `model-hub.md` §2 — *Product promise, locked 2026-08-07* |
| Which nouns UI copy may use, and which are required | `model-hub.md` §3 — *Vocabulary (v3 recut; UI copy uses only these nouns)* |
| What a source is and what it carries | `model-hub.md` §4.1 — *Supply — Sources (global assets, no ordering)* |
| Per-backend order plus per-model policy | `model-hub.md` §4.2 — *Gateway strategy* |
| **How a request resolves to a source — the sole authority** | `model-hub.md` §4.3 — *The only normative resolution algorithm* |
| Whether eligibility is client- or server-decided | `model-hub.md` §4.4 — *Eligibility is server-authoritative (v3)* |
| Source states, self-healing classes, `detail_key` vocabulary | `model-hub.md` §4.5 — *State taxonomy* |
| How route policy is stored and mutated | `model-hub.md` §4.6 — *Route-policy storage and mutation* |
| Downstream Agents | `model-hub.md` §4.7 |
| OpenCode identifier scheme | `model-hub.md` §4.8 — *locked 07-23, retained in v3* |
| Which module owns which class of configuration | `model-hub.md` §5 — *Surfaces — two modules, one understandable handoff* |
| Modes and onboarding | `model-hub.md` §6 — *Modes & onboarding* |
| Security boundaries | `model-hub.md` §7 |
| Explicit non-goals | `model-hub.md` §9 — *(v3)* |
| Behaviour acceptance criteria | `model-hub-implementation.md` §8 — *AC-1…AC-31, v3 addenda through 2026-08-09; AC-29/30/31 arrived with the `176b41b7` basis and are cited in §0.2, §0.5, §0.6, §0.7 and §3* |
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
- **`model-hub-implementation.md` §8 owns behaviour acceptance; §3 here owns visible
  acceptance.** A candidate item that would pass or fail regardless of what is on
  screen belongs in §8, not here.
