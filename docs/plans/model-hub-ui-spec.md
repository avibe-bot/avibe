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
   (AC-1…AC-28) and the `FC-01…FC-14` final-contract handoff. §3 below does not
   duplicate, restate, or extend either.
3. `model-hub-contracts/` — the frozen wire shapes the two above are landed as.
4. This file — layout, copy, state reachability, interaction feedback.
5. `design.pen` — the pixels. Where this file and the frame disagree on a number,
   the frame is right and this file must be corrected, *unless* the number is
   marked `[derived]`.

This file **references anchors and never restates spec content**. If you want to
know what a chain is, read §4.3 there; this file only says where it is drawn.

**Verification basis.** Every anchor and every `[spec]` / `[contract]` claim below
was checked against `docs/model-hub-v3-local-gateway` @ `7984aabf` — the open head
of the spec lane's PR #1215 — **not** against `master`, whose §3, §4.1, §4.2, §4.6
and §5 have all been superseded there. A reader on `master` will find some anchors
missing; that is the expected state until #1215 lands, and this file must not merge
before it does.

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

| # | Surface | Missing | Verified absent at `7984aabf` |
| --- | --- | --- | --- |
| G-2 | 06 protocol edit entry (instructed, undrawn) | any route that changes a stored protocol | AC-27 「changing protocol requires a new Source」; FC-12 PATCH body is `{display_name?, base_url?, force?}` |
| G-3 | 06 model inventory | a way to retire a *discovered* model from a source's inventory | FC-03 model item is `{id, origin, reasoning_efforts, display_name?, discovered_at?}`; FC-12 lists only manual model removal |
| G-4 | 06 quiet badge | provenance recording that a protocol was human-specified | no such field on the source shape |
| G-6 | 10 切换到网关 while the runtime is `not_installed` | whether adoption installs the dependency or refuses | `runtime-dependency.schema.json` `health` enumerates `not_installed`; no AC says what adoption does with it |

**G-1 and G-5 were retired by the frame rebuilds, and their numbers are not reused.**
G-1 was 05 ③'s 仍要添加 needing a durable *saved, explicitly unverified* state; the
rebuilt frame 05 has no 仍要添加 and states identification as a precondition of 添加,
so nothing on the surface requires that state any more (see E-3). G-5 was frame 04's
two-channel 去登录 needing a partial-completion outcome; the rebuilt frame 04 is
single-select and one 去登录 produces exactly one effect, so there is no partial to
define. Both are struck rather than renumbered: a `G-n` that moves is a citation that
silently retargets.

G-3 and G-4 are pure gaps: additive fields plus their mutation, listed in the PR
description for routing into the AC ledger. G-2 is the *visible* half of the one
remaining conflict in §0.6 (E-2) — adding the missing route is only one of the two
possible answers there, and this lane must not present it as the obvious one.

None of the four is decided here. This lane owns the visible layer, and inventing a
persistence model to make a drawn control defensible is exactly the kind of quiet
scope grab that produces two disagreeing authorities.

### 0.6 Open conflicts — escalated, not ruled

One place where the owner-approved frames and the behaviour authority at
`7984aabf` still say different things, plus two that closed. The open one is recorded
here so that a reader is never misled by a confidently-written section, and it is
escalated in the PR description. This lane does not pick a side: it is a conflict
between two owner decisions, and choosing between them is a product call, not an
editorial one.

A conflict is not the same thing as a gap. §0.5's G-2…G-6 are *missing* contract —
something has to be added. The one below is *contradicted* contract — something
has to be retracted. Filing a contradiction as a gap is how a lane talks itself into
implementing the side it happened to draw.

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

**E-2 — Can a stored protocol be changed?** Narrowed, not resolved. 05's
`undetermined.hint` used to say yes; the rebuilt frame says 「保存后不可更改」, so that
half now agrees with AC-27 (「changing protocol requires a new Source」, and FC-12's
PATCH body carries no protocol field). What is left is the standing instruction to put
a protocol-edit entry point at frame 06's quiet badge, plus the badge tooltip written
to match it. No frame draws that entry point, and this lane will not delete an owner
instruction on the strength of a frame it also owns. See G-2 and §1.6's held
entry-point paragraph. Both positions are owner rulings dated 2026-08-09.

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

Until E-2 is answered, treat the affected sections as **descriptive of the frames**
rather than as normative for implementation, and do not build on them.

### 0.7 Behaviour invariants surfaced by this pass

One behaviour the frames imply, which no AC covers and which this lane does **not**
write into any document. It is in the PR description under 「建议移交 AC 账本」 for
the spec lane to route; it is named here only so that a reader of §1.9 can see that the
silence is deliberate:

- **Switching a backend to gateway may require the runtime to be installed, not merely
  started.** `runtime-dependency.schema.json` distinguishes `not_installed` from
  `not_started`; §1.0 renders both, but what the 切换到网关 confirm in frame 10 does
  when the dependency is `not_installed` — install then start, or refuse — is a
  behaviour question.

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
| Run pill | Engine liveness | engine status | **`not_started` / `stopped`: yes; `running` / `not_installed`: no** `[derived]` | Start the engine (`POST /api/models/runtime/start` `[contract]`) |
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
- **Not installed**: the pill reads 「网关组件未安装」 and is **not** an activation
  target `[derived]`. It carries the same idle styling as Not started, for the same
  reason — a missing optional component is not a fault — but it must not offer 点击启动,
  because starting is not the action that resolves it. The runtime contract enumerates
  `not_installed` alongside `not_started` (`runtime-dependency.schema.json`, `health`)
  `[contract]`, so a UI that collapses the two renders a start button that cannot
  succeed and reports the failure as if the engine had crashed. What the 切换到网关
  confirm does from this state — install then start, or refuse — is a behaviour
  question and is filed in §0.7, not answered here.
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
| `shell.notInstalled` `[derived]` | 网关组件未安装 | Gateway component not installed |
| `shell.allDirect` `[frame]` | {{count}} 个后端都在直连 | All {{count}} backends are direct |
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
| `upstream.state.supplyingNative` | 正在供给 {{backend}}(原生) | Supplying {{backend}} (native) |
| `upstream.state.supplying` | 正在供给 {{backends}} | Supplying {{backends}} |
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
| `gateway.group.subtitle` | {{mode}} · {{status}} | {{mode}} · {{status}} |
| `gateway.group.mode.direct` | 直连 | Direct |
| `gateway.group.mode.gateway` | 网关 | Gateway |
| `gateway.group.status.ok` | 正常 | Healthy |
| `gateway.group.status.degraded` | 降级 | Degraded |
| `gateway.group.takenOver` | 接管中 | Taken over |
| `gateway.supply.none` `[derived]` | 没有可用来源 | No usable source |
| `gateway.row.followsOrder` | 跟随来源顺序 | Follows the source order |
| `gateway.row.custom` | 自定义链 | Custom chain |
| `gateway.row.current` | 当前 {{source}} | Now: {{source}} |
| `gateway.row.currentTakeover` | 当前 {{source}}(接管) | Now: {{source}} (takeover) |
| `gateway.collapse_one` | 还有 {{count}} 个型号 | {{count}} more model |
| `gateway.collapse_other` | 还有 {{count}} 个型号 | {{count}} more models |
| `legend.nativeDirect` | 原生直连 | Native direct |
| `legend.viaGateway` | 网关供给 | Gateway supply |
| `legend.connectedUnused` | 已启用 · 当前未被使用 | Enabled · not currently used |
| `legend.takeover` | 接管中 · 临时改走 | Taken over · temporarily rerouted |
| `legend.unavailable` | 暂不可用 · 供给已暂停 | Unavailable · supply paused |
| `legend.note` | 路由链按各后端的来源顺序自动派生;单个型号可改成自定义链 | Route chains are derived from each backend's source order; any single model can be switched to a custom chain |

**The legend is a rendered-relation index, not a fixed asset** `[frame]` `[derived]`.
01 draws three keys, 08 draws five; the two extra keys in 08 are exactly the two
relations 08 adds (a takeover, and a source whose supply is paused). So a key renders
**iff** the page currently draws at least one element in that relation — the legend can
never explain an ink that is not on screen, and can never omit one that is. UI-4 checks
the equality in both directions.

**Semantic ink** `[frame]` — four inks. Meaning is assigned **per element role**, and
the two roles below are disjoint, so every inked element has exactly one reading:

- **Relation / status ink** — the element states a fact about where tokens come
  from: wires, rails, tint washes, status text, supply pills, legend swatches.
- **Control ink** — the element states that a control is active, selected, or
  primary: tab underline, order badges, selected option, input focus ring,
  manual-row wash, primary button.

| Ink | As relation / status ink | As control ink | Where |
| --- | --- | --- | --- |
| `$--cyan` `#3FE0E5` | native direct, not via the gateway | **never** | wire, card tint `#3FE0E50A` / border `#3FE0E54D`, tile `#3FE0E51A`, status text |
| `$--mint` `#5BFFA0` | gateway supply | active / selected / primary | relation: wire, rail (`#5BFFA01A` chip / `#5BFFA033` line), supply text. control: active tab underline `@2`, order badges, selected option card, tier-editor focus ring, manual-row wash `#5BFFA00D`, primary buttons |
| `$--gold` `#FFC857` | takeover / temporarily rerouted | warning emphasis | 08 (wire `#FFC857` @1.75, pills `#FFC8571A`+`#FFC8574D`), 05 state ④ strip, 04 ToS note |
| `#FFFFFF26` | connected but not currently used | — | dim wire only |

Two asymmetries are deliberate and load-bearing:

- **Cyan is exclusive in both roles.** It never inks a control, so cyan anywhere on
  the page means exactly one thing: this supply bypasses the gateway. That is the
  single most consequential distinction on the surface — see D-6 and UI-8.
- **Mint is dual, and that is fine, because the roles never collide on one
  element.** A tab underline is not claiming an upstream relation, and a wire is not
  claiming to be a control. Forcing mint down to one meaning would need a second
  accent hue for controls, which buys nothing and costs the brand a token.

The honest statement of the rule is therefore a partition, not a whitelist: mint
inking a relation/status element **must** mean gateway supply, and mint inking a
control element **must** mean active/selected/primary. UI-9 checks that partition.

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
| Takeover active | Head source unavailable, next one serving | Recovery → Ready (this is frame 08) |
| Group expanded | Collapse row activated | Collapse toggled back |

Credential-invalid is the one worth stating precisely `[derived]`: a
`needs_action` source **stays in the list, in place**, with its status line
replaced by the cause and a one-tap repair action. It is not removed, not moved to
the bottom, and not silently dropped from the chains that name it — a source you
cannot see is a source you cannot fix. (UI-19.)

**Extreme data**

Collapse predicate for a backend group `[frame]` for the shape, `[derived]` for the
ordering rule:

```
N = 3                                     # ADDITIONAL nominal rows, not a total
mustShow  = { m in models | m.state != nominal }        # hard: never collapsed
ranked    = sort(models - mustShow, by=(
               0 if m.hasOverride else 1,               # overrides outrank
               m.backendMenuIndex))                     # then the backend's own order
visible   = mustShow ++ take(ranked, N)
collapsed = models - visible
render collapse row  iff  |collapsed| > 0
collapse label count = |collapsed|
```

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
- Expanding is idempotent and does not re-rank: the order the user saw stays the
  order they get.
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

**The first hop's ↑ is the only disabled control in either dialog** `[frame]` — glyph
`#FFFFFF33` against `$--foreground` on every other icon button, in the same 26×26
`#FFFFFF0A` shell. The shell staying put is the point: the boundary of the list is
shown by dimming the glyph, not by removing the button and re-flowing the row.

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

- **Empty chain**: the hop list is replaced by one line, 「现在没有来源能提供这个
  型号」, and 添加一跳 stays enabled. An empty list with a live add button is
  honest; a disabled dialog is a dead end.
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
| `empty` `[derived]` | 现在没有来源能提供这个型号 | No source can supply this model right now |
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
| `head` | `padding [16,20]`, vertical, `gap 10` |
| Title | 15 / 700, `$--foreground`, + 13px info icon `#FFFFFF59` |
| Close `fUvS9` | 15px, `#FFFFFF59` |
| Subtitle | 11.5 / normal, `#FFFFFF73` |
| Segmented `i5B8qF` | `#FFFFFF0A`, radius 9, `padding 3`; segment radius 6 |
| — active segment | `#FFFFFF1A`, label 12 / 600 `$--foreground` |
| — idle segment | transparent, label 12 / 500 `$--muted` |
| `dbody` | `fill_container` height, `padding 20`, `gap 14`, **the sole scroll owner** `[derived]` |
| Section label | 10.5 / 700, `#FFFFFF73` |
| Ordered row | 58 tall, radius 9, `#FFFFFF08`, `gap 12` |
| Held-out row | 58 tall, radius 9, `#FFFFFF05` |
| Grip icon | 14px — `#FFFFFF4D` on ordered rows, `#FFFFFF33` on held-out rows |
| Ordinal badge | 22×22, radius 6; **#1** `#5BFFA01A` / `$--mint`; **#2+** `#FFFFFF0A` / `$--muted` |
| Source name / meta | 12.5 / 600 `$--foreground` over 10.5 / normal `#9BA3B8B3` |
| Type tag | radius 999, 10 / 600 — the provenance palette in §2 D-19 |
| `foot` | `#FFFFFF05`, 1px top border, buttons radius 7, 12 / 600 |

**The ordinal badge inks the first position, and only the first** `[frame]`. Rank 1 is
mint; every later rank is the muted neutral. That is not decoration — mint on this page
means *gateway supply actually happening*, and rank 1 is the source a
跟随来源顺序 model resolves to right now. Ranks 2+ are configured and idle, which is
the same distinction the wire layer draws with `#FFFFFF26`. If the first eligible
source changes, the mint badge moves with it `[derived]`.

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
| Engine unavailable | Gateway not running and gateway-upstream was chosen | Engine recovers |
| Already bound | This account is already another source `[spec §4.1]` | Choose another account |
| Loading | — | Not applicable: nothing is fetched before the dialog opens |

`[derived]`: choosing 登录为网关来源 while the engine is down must fail **before**
the browser hand-off, with 「网关没有响应,请重试」 — sending someone through an
OAuth flow that has nowhere to land is the most expensive possible way to report
that the engine is down.

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
| `opt.native.desc.claude` | Claude Code 直接用这个订阅,凭据只留在本机,不经网关。 | Claude Code uses this subscription directly; the credential stays on this machine and never goes through the gateway. |
| `opt.native.desc.chatgpt` | Codex 直接用这个 ChatGPT 账号登录,不经网关。 | Codex signs in with this ChatGPT account directly, not through the gateway. |
| `opt.hub` | 登录为网关来源 | Sign in as a gateway source |
| `opt.hub.desc.claude` | 把这个订阅交给网关,供给 Codex、OpenCode 等其他 Agent。 | Hand this subscription to the gateway so it can supply Codex, OpenCode and other Agents. |
| `opt.hub.desc.chatgpt` | 网关把它供给 Codex 和其他 Agent,用量、额度、接管都能看到。 | The gateway supplies it to Codex and other Agents, with usage, quota and takeover all visible. |
| `badge.recommended` | 推荐 | Recommended |
| `badge.secondary` | 次选 | Second choice |
| `badge.supportedNotRecommended` | 支持,不推荐 | Supported, not recommended |
| `tos.claude` | 订阅条款只授权你本人在 Claude 官方客户端里使用。转供其他 Agent 属于超范围使用,账号可能被限制。 | The subscription terms authorize only you, inside Claude's official clients. Supplying it to other Agents is out-of-scope use and the account may be restricted. |
| `hint.claude` | 两条都要也可以,分两次添加。 | You can have both — add them one at a time. |
| `hint.chatgpt` | 不经网关:额度用完不会自动接管,也看不到用量。 | Not through the gateway: nothing takes over when the quota runs out, and usage stays invisible. |
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

Four states are drawn: left column is the happy path (① default → ② adding →
success destination), right column is the two failures (③ unreachable /
unauthenticated / wrong address, ④ connected but the interface is undetermined).

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
| `sqZa9` success note | that the dialog closes straight into 06 | static | no | — |

**Metrics** `[frame]`: dialog 560 wide, height auto in all four states — the frame
sets no fixed height, so a build that pins one is deviating, not matching. Head
`padding [16,20]` `gap 4`; body `padding 20` `gap 14`; field `gap 6`; input 520×36
`radius 8` fill `#FFFFFF08`; field hint 10.5 JetBrains Mono `#9BA3B8B3`. 拉取型号
`padding [8,14]` `gap 6`, neutral. Result strip 520 wide `padding [11,13]` `gap 10`
`radius 9`: red `#FF6B6B14`/`#FF6B6B40` for ③, gold `#FFC85714`/`#FFC85759` for ④,
mint `#5BFFA014`/`#5BFFA040` for the success note. State ④ selector `padding 3`
`gap 3` on `#FFFFFF0A`/`$--border`, **all three segments fill `#00000000`**.
Foot `padding [14,20]` `gap 8` on `#FFFFFF05`, top border; buttons `padding [8,14]`
`gap 6`.

**Two of those fills are the design carrying a product rule, not styling.** Every
segment in ④'s selector is transparent — nothing is pre-selected — and ④'s primary
`LrUsk` 重试 is `#5BFFA059`, the same dimmed mint that ② uses for its in-flight
primary. Read together they say: with no hint chosen there is nothing new to try, so
the retry is not offered. ③'s primary, by contrast, is full `$--mint` — a credential
failure is retryable immediately, because fixing the field *is* the new information.
A build that pre-selects a segment, or that enables ④'s 重试 before a pick, is not
deviating cosmetically; it has implemented the opposite decision.

**State machine**

| State | Entry | Exit |
| --- | --- | --- |
| ① Default | Dialog opened | Add pressed → ②; 拉取型号 → ③′ on failure, inline model count otherwise |
| ② Adding | Add pressed | Success → dialog closes into 06; classified failure → ③; undetermined interface → ④; 取消 → ① (transient credential revoked server-side `[contract]` AC-26) |
| ③ Failure, **Add origin** | A probe run *as part of Add* classified the failure | 重试 → ②; 取消 → dismiss |
| ③′ Failure, **Pull origin** `[derived]` | A probe run by 拉取型号 classified the failure | 重试 → **another 拉取型号, not ②**; 取消 → ① |
| ④ Interface undetermined | Reachable **and** authenticated, response shape matches no known interface | Pick a hint + 重试 → **probe again in the hinted order** → identified: persist and close; still undetermined: back to ④ with the attempt as evidence. 取消 → dismiss |
| Empty | — | Not applicable: a form has no empty state |
| Credential-invalid | Auth failure is one of ③'s three causes | As ③ |
| Engine unavailable `[derived]` | Gateway not running | Add is blocked with 「网关没有响应,请重试」; the form keeps its values |

**The failure state carries its origin, and 重试 repeats the operation that failed —
not a different one** `[derived]`. Both 添加 and 拉取型号 run the same probe and classify
the same three causes, so both land on the same strip, with the same foot. That does not
make them the same state. 拉取型号 is labelled optional — 「可选 ·「添加」时会自动拉一次」
— precisely so that it can be pressed without committing anything; a 重试 that ran 添加
instead would mean a user who pressed the optional button, fixed their key, and pressed
重试 got a source they never asked to create. So ③′ differs from ③ in exactly one way,
and it is invisible: 重试 re-runs the pull rather than the add. Everything drawn is
identical — strip, classified cause, request evidence, both buttons. UI-35 checks the
pair, and it is worth stating plainly that this is a state distinction with **no visual
carrier**: the only way to get it right is to keep the origin in state, and the only way
to get it wrong is to reconstruct it from what is on screen.

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
frame's own caption. UI-12 keeps that uniqueness checkable.

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
| `success.title` | 成功 → 弹窗关闭,直接进入「来源详情 · 型号管理」 | Done → the dialog closes and you land on Source details · Models |
| `success.detail` | 型号列表、重新拉取、推理强度档位都在那里维护 | The model list, refetch and reasoning tiers are all maintained there |
| `cancel` | 取消 | Cancel |

The three protocol strings above are the **only** protocol names anywhere in the
product surface (UI-12). They are identifiers, identical in both locales, and they
are exactly the three transports the protocol enum admits `[contract]` AC-28 — the
label 「OpenAI Chat Completions」 maps to `openai_chat`.

**`undetermined.hint` used to contradict AC-27, and now states it.** The string the
frame drew previously — 「选一种才会保存 · 之后可在来源详情里改」 — promised the choice
was changeable later. At `7984aabf`, AC-27 says the opposite: after Save the stored
protocol is preserved byte-for-byte through retest, discovery, refresh, credential and
Base-URL replacement and restart, and 「changing protocol requires a new Source」; FC-12
confirms the source PATCH body is exactly `{display_name?, base_url?, force?}`, with no
protocol field. The rebuilt string ends 「保存后不可更改」 — the frame moved onto the
contract's side. What remains of **E-2** is therefore only the standing instruction to
put a protocol-edit entry point at frame 06's badge, which no frame draws and which
§1.6 keeps held; the 05 half of that conflict is closed.

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
| `sugad` source bar | 36×36 identity tile, source name, mint dot + 使用中 + 型号列表更新于 {{time}}, mono `host · N 个型号` | source | 重新拉取 / 添加模型 | Refetch / append an editable row |
| `myA8k` header | 型号 ID (250) · 录入 (84) · 推理强度 (470, with info) · fill spacer | static | no | — |
| `OM5PH` row | model id, entry-kind pill, tier chips, overflow icon | one model | tiers, overflow | Edit tiers / row menu |
| `p2JwTz` tiers | chips, or 未设置档位 + `+ 添加档位` | `reasoning_efforts[]` `[contract]` FC-03 | yes | Enter edit mode |
| `eVavA` tiers (editing) | removable chips + text input + 回车添加 · 任意文本 | local edit → `PATCH /api/models/custom-models` `[contract]` AC-26 | yes | Add / remove a tier |
| `nN4TZ` manual row | editable id input, 手动添加 pill, tier affordance, 取消 / 添加 | local draft | yes | Commit or discard |
| `Q83BF` add row | 添加模型 + when to use it | — | yes | Append a manual draft row |
| `tF3Bh` footnote | scope of this page; that tiers are yours to type; that the interface type is identified at add time and not shown | static | no | — |
| Quiet badge `[derived]` | that this source's interface was specified by hand | source provenance | hover | Tooltip naming the interface |

**There is no per-model on/off on this page, and that is the design** `[frame]`. An
earlier version of this section described an 接入 column with a toggle per row; the
frame has neither — three columns and a fill spacer, no switch anywhere. The reason
is D-9: a model's participation is decided by the routing chain that resolves it, so a second
per-model boolean here would be a second owner of the same fact, and the two would
disagree the first time a chain changed. What the page owns is the *inventory* —
which models this source has, and what tiers each accepts. `[contract-gap]` G-3 is
therefore about the discovered-model record, not about a control on this frame.

**Metrics** `[frame]`: source bar `fill_container` `padding [14,18]` `gap 14`
`radius 12` `$--surface` / `$--border`, identity tile 36×36 `radius 9`, status row =
5px dot + 12/500 `$--mint` + 11 `#FFFFFF8C`, mono line 10.5 JetBrains Mono
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
| Credential-invalid | Source is `needs_action` `[spec §4.5]` | Repair, from the source bar `[derived]` — the frame draws the healthy state only |
| Interface specified by hand | Source provenance says so | Never — the badge is permanent while true |

Five rules:

- **Refetch is a diff, not a replacement** `[derived]`. Manually added models
  survive it; removed-upstream models are marked, not deleted, when they are
  connected — deleting something the user switched on, because a vendor's `/models`
  changed shape, silently unconfigures them. AC-26 already requires rediscovery to
  preserve user tier edits `[contract]`; this is the same principle applied to the
  row itself.
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
| `status.listUpdated` | · 型号列表更新于 {{time}} | · model list updated {{time}} |
| `summary_one` | {{host}} · {{total}} 个型号 | {{host}} · {{total}} model |
| `summary_other` | {{host}} · {{total}} 个型号 | {{host}} · {{total}} models |
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
| `interfaceBadge` `[derived]` | 接口由你指定 | Interface set by you |
| `interfaceBadge.tooltip` `[derived]` `[contract-gap]` G-2 | 添加时没能自动认出,当前按「{{protocol}}」处理。可以改。 | Could not be identified automatically at add time; currently handled as "{{protocol}}". You can change it. |
| `interfaceBadge.tooltip.immutable` `[derived]` | 添加时没能自动认出,当前按「{{protocol}}」处理。 | Could not be identified automatically at add time; currently handled as "{{protocol}}". |
| `interfaceBadge.change` `[derived]` `[contract-gap]` G-2 | 改为… | Change to… |
| `footnote` | 这里只管「这个来源有哪些型号」。型号走哪条路由链,在网关模块里改。档位自己填,两种录入方式都一样。接口类型添加时自动认出、页面不显示;只有当初没认出、由你提示过的来源,标题旁才带一枚安静徽标。 | This page answers only "which models does this source have". Which routing chain a model takes is set in the gateway module. Tiers are yours to type, the same for both entry kinds. The interface type is identified when the source is added and is not shown here; only a source whose interface you had to hint at yourself carries a quiet badge next to its title. |

**The status line reports the inventory's age, not a probe** `[frame]`. 使用中 ·
型号列表更新于 16:02 says when this table was last refreshed; it deliberately does not
carry latency or a last-checked timestamp, because nothing on this page performs a
probe. A freshness stamp next to a refetch button is a closed loop the user can
act on. A latency figure next to a refetch button would invite exactly the conflation
the previous rule forbids.

The quiet badge is the one exception to "the user does not perceive the supply
mechanism" (D-8), and 06 does **not** get a second header state drawn for it: it
is one element in an existing row, so the footnote carries the rule instead. That
was a cost decision, made explicit here so nobody reads the absence of a frame as
the absence of the requirement. The badge's existence needs a provenance field that
does not exist yet — `[contract-gap]` G-4.

**The protocol-edit entry point at the badge is specified but held** `[contract-gap]`
G-2. The intended shape, so that it is not re-derived later: the badge itself is the
affordance — activating it opens a small popover carrying the current interface name
and a 「改为…」 action with the same three-value selector as 05 state ④, and changing
the value re-verifies with the new adapter before it takes effect, exactly as 05 ④
does. It is deliberately *not* a persistent visible control: it is meaningful only
for the small set of sources whose interface could not be identified automatically,
and putting a protocol control on every source's detail page would re-teach the whole
user base a mechanism D-8 spends its entire argument hiding.

This is written down and **not drawn**, because it cannot be built as specified today
and because two owner rulings disagree about whether it should exist at all: the
instruction to add this entry point (and 05's 「之后可在来源详情里改」) versus AC-27's
「changing protocol requires a new Source」. Both are dated 2026-08-09. The escalation
is **E-2** in §0.6 and in the PR description; the design change waits on the answer
rather than baking either reading into the frames.

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
| Header | run pill only | run pill **+** `q4k3s` 「1 处接管中」 (gold, 84×24, `radius 999`) |
| ChatGPT source card | 正在供给 Codex | 额度用尽 · 已暂停供给 |
| aihub source card | 正在供给 OpenCode | 正在供给 Codex、OpenCode |
| Codex group header | — | `bbC4N` 「接管中」 chip (gold, 46×20) |
| Codex supply line | 网关供给 · ChatGPT 订阅 | 已接管 · aihub (gold, 11/600) |
| Codex model rows | 当前 ChatGPT 订阅 | 当前 aihub(接管) |
| Wire layer | 4 paths | **5** — plus `AEaxi` `w_aihub→Codex(接管)`, gold `#FFC857` @1.75 |
| Legend | 3 keys | **4** — plus 接管中 · 临时改走 |

The same fact is stated at three grains: a page-level pill (*is anything wrong*), a
group chip and supply line (*which Agent*), and a per-row current-source suffix
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
candidate left the group shows 「没有可用来源」 and the wire layer draws no gold
path — gold means *rerouted*, and painting it where nothing was rerouted would
report a recovery that did not happen. The header pill counts backends in
takeover, so at zero it is absent, not `0 处接管中` (UI-14).

**Copy** — `models.hub.takeover.*`; every other string is shared (§1.0).

| Key | 中文 | English |
| --- | --- | --- |
| `pill_one` | {{count}} 处接管中 | {{count}} takeover active |
| `pill_other` | {{count}} 处接管中 | {{count}} takeovers active |
| `chip` | 接管中 | Taken over |

**Extreme data** `[derived]`: with N takeovers the pill says N and each affected
group carries its own chip — there is no "and others" summarization, because the
one question a takeover raises is *which one*. Gold paths are generated per
rerouted relation, so overlapping wires must remain individually traceable
(distinct routing, not stacked identical curves).

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

**What the shell drops, and why** `[frame]`. Frame 09 renders the header but **no tab
strip, no three-column `cols` track, no dispatch rail, no wire layer and no legend.**
There is no gateway module to occupy the second column, no supply relations to draw, and
therefore no inks to explain. An empty gateway column with a placeholder would be worse
than its absence: it would assert that a thing exists here and is currently broken,
which is the opposite of the truth.

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
| `head` | `padding [16,20]`; title 15 / 700; close 15px `#FFFFFF59` |
| Subtitle | 11.5 / normal, `#9BA3B8B3` |
| `dbody` | `padding 20`, `gap 18` |
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
| `cancel` | 取消 | Cancel |
| `confirm` | 切换到网关 | Switch to gateway |

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
| Dependency missing `[contract-gap]` G-6 | Runtime `health` is `not_installed` (§1.0) | **Undefined** — install-then-start or refuse is a behaviour question (§0.7) |

**Extreme data** `[derived]`: `{{backend}}` and `{{vendor}}` are interpolated in six
places, so the dialog must survive the longest backend name without reflowing its foot;
bullets wrap rather than truncate, because a consequence half-shown is worse than one
that costs a line.

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
tell what it is talking to.

**D-5 — Reasoning tiers are a user-typed list with no default and no prefill.**
Empty renders as 未设置档位.
*Why:* discovery returns model ids only. A prefilled tier would be a value the
product invented, and the user would read it as a fact about the model.

**D-6 — `$--cyan` means exactly one thing: native direct, not via the gateway.**
*Why:* a colour is readable at a glance only while it has one referent. A second
meaning does not add information, it halves it.

**D-7 — A collapse never swallows an active state.** Every non-nominal model row
is visible even if that pushes the group past three rows.
*Why:* a collapse exists to hide the boring. If it can hide the one row that needs
attention, the compression has inverted its own purpose.

**D-8 — The user does not perceive the supply mechanism.** Protocol, channel and
injection are absent from every surface; the sole exception is the quiet badge on a
source whose interface the user specified themselves.
*Why:* the mechanism is the product's job, and surfacing it invites decisions the
user has no basis to make. The exception holds because there the user *did* decide,
and hiding somebody's own decision makes it unfindable.

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
means different things in each.** Wires: cyan = 原生直连, mint = 网关供给, violet =
接管, `#FFFFFF26` = 已启用 · 当前未被使用, gold = 暂不可用 / 供给已暂停. State text:
mint = 使用中 / 正常, gold = 降级 / 暂不可用 / 冷却, rose = 需处理 / 异常 / 无可用来源,
muted = 备用, cyan = 原生 provenance only, violet-tint `#7C5BFFCC` = a takeover hop
label.
*Why:* a wire describes a *relation between two things*; state text describes *one
thing's condition*. Collapsing them into one legend forces both to be wrong somewhere —
gold as a relation means supply stopped, gold as a condition means degraded, and those
are not the same claim. §1.0's ink table is the single place both are written down.

**D-22 — A group head's status line is `<mode> · <supply_status>`, always both.**
直连 · 正常, 网关 · 正常, 网关 · 降级.
*Why:* mode and health are independently variable and users confuse them constantly —
"is it on the gateway" and "is it working" are different questions, and a single word
answers whichever one the reader happened to be asking.

**D-23 — The legend swatch may deviate from the ink it stands for, only downward in
alpha, and only where the wire's own alpha is below the legibility floor.** The
已启用 · 当前未被使用 wire is `#FFFFFF26` at 1.75px; its 20×1 legend swatch renders
`#FFFFFF33`.
*Why:* a legend that cannot be seen fails at the one job it has. This is a real
exception to "every colour resolves to a declared token", so it is written down and
bounded here rather than left for a reviewer to discover as an unexplained literal —
UI-4 admits exactly this one deviation and no other.

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
`model-hub-implementation.md` §8 (AC-1…AC-28) and are neither duplicated nor
extended here.

**No item depends on a `[contract-gap]`.** Where a frame draws an affordance whose
persistence does not exist yet (§0.5: G-2, G-3, G-4, G-6), the item below says so and
checks only the part that is real — usually that the affordance is absent rather than
present-and-broken. An acceptance list that requires something unbuildable does not
raise the bar; it trains people to sign off on items they could not actually verify.

**No item depends on an open conflict either.** §0.6's one live escalation (E-2,
whether a stored protocol can change) touches §1.5 and §1.6, but no item below asserts
either side: UI-12's protocol-surface equality is stated over what is drawn today.
Nothing here has to be rewritten when the owner answers — only §1's prose. The same
held for the two escalations that have since closed (E-1 the scope of the source order,
E-3 whether an unverified source can be saved): both were ruled against the side the
frames had drawn, both moved the frames, and no acceptance item had to be retracted.

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

The set equalities (UI-9, UI-10, UI-12, UI-14, UI-27, UI-31) get the sharpest version
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
*Claim:* three properties hold across the whole domain — head `padding [16,20]`, a foot
with a 1px top border over `#FFFFFF05`, and a scrim of `#05050BE0` at 1440×1100. Every
other metric is **per container** and is read from that frame's table, not from this
item.
*Check:* measure each of the five; compare the three shared properties here and the rest
against §1.1/§1.3/§1.4/§1.5/§1.9.
*Criterion:* widths are 520 / 460 / 620 / 560 / 560, and 03 alone is full-height with a
left border only. Body metrics differ by container by design — 03's body is
`fill_container` and is the drawer's scroll owner, which a uniform `padding 20 gap 14`
claim would have silently contradicted. That contradiction is why this item is written
per container rather than as one sentence.

**UI-7 — Frame 06's table columns and its header are the same four widths.**
*Check:* measure the header cells and one body row's cells.
*Criterion:* 250 / 84 / 470 / 110 in both, with `gap 16` and `padding [·,18]`; body
cells align with header cells to ±1px.

### Semantic colour

**UI-8 — For every cyan-inked element, its subject is a native-direct supply
relation.**
*Check:* list every element whose computed colour, border or stroke is `#3FE0E5`
or an alpha of it; for each, name the entity it describes.
*Criterion:* every one is a native-direct source, its card, its wire, or its
「原生」 tag. One cyan element describing anything else fails the item.

**UI-9 — Mint, gold and `#FFFFFF26` partition by element role exactly as §1.0's ink
table says.**
*Check:* list every element whose computed colour, border, stroke or fill is one of
those inks or an alpha of it. Classify each as a **relation/status** element (wire,
rail, tint wash, status text, supply pill, legend swatch) or a **control** element
(tab underline, ordinal badge, selection mark, focus ring, row wash, primary
button). Then read its meaning.
*Criterion:* the three roles **partition** the domain — every inked element is exactly
one of relation/status, control, or identity, and the classification is decidable from
form factor alone (a wire or dot or status word; an interactive affordance; a filled
rounded tile containing a glyph). Then: a mint relation/status element means gateway
supply; a mint control means active, selected or primary; gold means takeover on a
relation and warning emphasis on a control; `#FFFFFF26` means a connected-but-unused
wire; and an **identity tile carries its backend's brand colour and asserts nothing**
(D-20). An element that is two roles at once fails, and so does an element in none —
the earlier two-role version had no home for OpenCode's violet identity tile and would
have read it as a takeover that was not happening. That third role is not a loophole;
it is the reason the other two can stay strict.

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
{frame 05 state ④ selector} today, and {frame 05 state ④ selector, frame 06's
quiet-badge tooltip} once G-4 closes.**
*Domain:* the rendered DOM of all nine surfaces, both locales.
*Claim:* the set of surfaces containing any of the three protocol strings equals the set
above — the one-member version until G-4 lands, the two-member version after.
*Check:* search for the three strings; compare the hit set against the applicable
right-hand side.
*Criterion:* exact set equality. **The second member is conditional and the condition is
named**, because 06's quiet badge sits on a `[contract-gap]`: there is no field recording
that a protocol was human-specified, so the badge cannot render today, and an
unconditional two-member equality would fail every correct build until the gap closes.
Per the third global exclusion, a `[contract-gap]` member is in no domain until its gap
closes. Stated as an equality rather than a prohibition, so a *new* surface that leaks a
protocol name fails it too.

**UI-13 — Every product noun rendered on these surfaces has a row in
`model-hub.md` §3's vocabulary table, and uses the term that table marks required.**
*Check:* extract the nouns from the copy tables in §1 and look each up in §3.
*Criterion:* every one has a row, and where §3 marks a term **required**, the copy
uses that term and not a synonym. 网关 / Gateway is the required noun for the local
adaptation and routing module, so 「模型网关」 as the page title, 「网关」 as the module
heading and 「网关供给」 as the supply phrasing all pass; a build that substituted
「Models」 or 「路由」 for them fails. Any noun with no row is a finding against
whichever side is wrong — usually this file, occasionally the table.

**UI-14 — The set of count-bearing keys equals the set of keys shipping i18next
plural variants, and each is grammatically correct in English at 0, 1 and 2.**
*Domain:* keys under the `models.hub.*` namespace in `zh.json` and `en.json` — **not
the whole file**. The rest of the product predates this spec and is not this lane's to
certify; an unscoped grep makes this item fail on strings nobody here wrote, which is
how a real check gets marked flaky and then ignored.
*Claim:* within that namespace, `{keys interpolating {{count}}}` equals `{keys shipping
an i18next plural family}`, and each renders correctly in English at 0, 1 and 2.
*Check:* `grep -o 'models\.hub\.[^"]*' ` both files, filter to keys whose value contains
`{{count}}` — that is the left-hand set; filter to keys with `_one` / `_other` suffixes
(compared on the stem) — that is the right-hand set. Then render each at 0, 1 and 2.
*Criterion:* the two sets are equal — a count-bearing key with no plural family fails,
and so does a plural family nobody interpolates a count into. In `en`, no `1 models` and
no `1 source` mismatch; in `zh`, both variants exist and carry identical values.
`0 takeovers active` never renders because the element is absent at zero, not because
the string handles it. The right-hand side is **§1.0's list, which is the single place
it is maintained**: `shell.allDirect`, `upstream.count`, `gateway.modelCount`,
`gateway.collapse`, `chain.derived.hops`, `sourceDetail.summary`, `takeover.pill` —
seven keys, each `_one` and `_other` in both files, twenty-eight entries. Note
`sourceDetail.summary` is a single key whose *value* interpolates more than one count;
it needs one plural family selected on the model total, and the other counts render as
plain interpolations. A build that splits it into two keys must add both to §1.0's list,
and this item is what catches the omission.

**UI-15 — Copy states consequences, not mechanisms or rationale.**
*Check:* read every string in §1's tables and ask, for each, "does this tell me
what happens to me?"
*Criterion:* no string names an internal mechanism, and no string argues for a
design decision — the arguments live in §2. (This item exists because two strings
failed it during the design pass and had to be rewritten.)

### State reachability

**UI-16 — Every state in §1's state tables is reachable, and each has a named
trigger.**
*Domain:* rows of §1's state tables, **excluding** rows whose exit column reads
「不适用」 / "Not applicable" and rows marked `[contract-gap]`. Per the global exclusions:
a 不适用 row records that a state was considered and ruled out — 04's Loading row and
05's Empty row exist to say *a form fetches nothing* and *a form has no empty state* —
so demanding that they render inverts what they document. Today that exempts 04 Loading,
05 Empty and 10's G-6 dependency row.
*Claim:* every remaining row renders, from the trigger its entry column names.
*Check:* perform each entry condition directly, or serve the payload that produces it.
*Criterion:* every in-domain state renders. An unreachable in-domain state is either a
missing implementation or a spec row that should be deleted; both are findings. A 不适用
row that *does* render is also a finding, in the other direction.

**UI-17 — Every list has an empty state that keeps its frame, says which emptiness it
is, and offers the exit.**
*Domain:* the five lists these frames draw — the upstream source list, a backend group's
model rows, a source detail page's model table, and frame 03's two drawer sections.
*Claim:* at zero rows each keeps its head and footer, renders the message §1 names for
**that** list, and keeps its add affordance present and enabled.
*Check:* serve zero sources; zero models on a backend; zero models on a source detail
page; zero eligible sources for a backend's order.
*Criterion:* nothing vanishes, and the four emptinesses are **distinguishable**, because
they have different causes and different fixes:

| Fixture | Message | Where §1 states it |
| --- | --- | --- |
| No sources at all | 还没有来源。先添加一个订阅或 API Key。 | §1.0 Empty |
| A backend with no source able to supply it | 没有可用来源 | §1.0 Empty |
| A backend whose menu resolves to zero models | 这个后端没有可用型号 | §1.1 `[derived]` |
| A source detail page with zero models | §1.6's empty row | §1.6 |
| A backend order with zero eligible sources | 这个后端还没有可用来源。 | §1.3 |

The third row is the one an earlier version left unnamed: *no source can supply this
backend* and *this backend's menu is empty* are different failures with different
repairs, and a single shared 没有可用来源 sends the user to add a source when the source
is already there.

**UI-18 — Exactly one flow blocks on a wait, and every other load degrades in
place.**
*Check:* throttle the network and enter each surface; then press 添加 in frame 05.
*Criterion:* 05 state ② is the only modal wait, and it is cancellable; every other
surface renders its shell with per-region placeholders and never a full-page
spinner.

**UI-19 — A `needs_action` source renders as itself, in place, everywhere it
appears.**
*Check:* serve one source in `needs_action` and inspect the upstream card, the
frame 03 order row, and any chain hop naming it.
*Criterion:* present in all three, position unchanged, cause shown, repair
reachable from the card. Absence anywhere fails — including "helpfully" dropping
it from the order.

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
every interactive element on the nine surfaces. Disabled: **only** elements a §1 state
table actually assigns a disabled state — today 05's 重试 in ④ before a hint is picked,
03's 保存顺序 with no eligible sources, and 01's collapse row at zero hidden models.
Frame 04 contributes none: a radio group has no zero-selected state, so 去登录 is enabled
from the moment the dialog opens. Pending: only controls that mutate.
*Claim:* each element has the states of the domains it belongs to, and no others are
required.
*Check:* tab through each surface, then hover each control; then reach each of the three
disabled states from its table.
*Criterion:* focus is always visible without a mouse; the four disabled states use the
dimmed-token style (`#5BFFA059` for a dimmed primary); a mutating control shows pending
and cannot be double-fired. **A control nothing ever disables needs no disabled style** —
the earlier universal phrasing demanded one for every control including 取消, which would
have contradicted D-15's requirement that the exit is always enabled. An acceptance list
that contradicts a decision it also contains is worse than a missing item, because it
forces the implementer to guess which one was meant.

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
*Domain:* every state a §1 state table marks as a failure. It splits in two, because the
surfaces do: **modal** failures (05 ③, 05 ③′, 05 ④, 04's OAuth failure, 10's Failed, a
failed order save in 03) and **inline** failures (06's failed refetch, 01's Unreachable
and Partial).
*Claim:* modal failures carry a present, enabled 取消 or 关闭. Inline failures leave the
surrounding page navigable — the surface was never captured, so back-navigation *is* the
way out and no dismiss control is required.
*Check:* reach each of the nine; on the modal ones look for the control, on the inline
ones confirm the page's own navigation still works and the failure has not blocked it.
*Criterion:* as claimed. **Requiring a 取消 button on an inline failure would be a
finding, not a pass** — it would add a dismiss control to a page-level error strip, which
either does nothing or hides the error while it is still true. The earlier undivided
phrasing implied one, and a failed refetch on 06 was the counterexample.

### Extreme data

**UI-28 — The collapse predicate is implemented as written in §1.1, including that
`N` is additive.**
*Check:* serve §1.1's six fixtures for one backend, verbatim: (12, 0), (12, 2),
(12, 5), (12, 12), (3, 0), (2, 1) as `(models, non-nominal)`.
*Criterion:* visible rows are 3, 5, 8, 12, 3, 2 and the collapse labels are 「还有 9 /
7 / 4 个型号」, none, none, none. A build that treats `N` as a total row floor produces
3, 3, 5 on the first three fixtures and fails — that is the specific mistake this
fixture set exists to catch. Priority order within the visible set is override-first,
then the backend's own menu order.

**UI-29 — Every unbounded string has a stated truncation rule and keeps its full
value reachable.**
*Check:* serve a 120-character source name, a 200-character base URL, and an
80-character model id.
*Criterion:* nothing overflows its container or reflows the layout; URLs and keys
truncate from the middle keeping both ends; the full value is in `title` or a
tooltip.

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
holds no field it could come from (D-16).

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

**Total: 38 items (UI-1 … UI-37, with UI-26a).** Nothing is blocked on another lane.
Items bounded by a contract gap say so inline and name the gap — UI-12's second set
member on G-4 — and per the third global exclusion none of them asserts an unbuildable
requirement. Light-theme and mobile variants are not drawn, so UI-1…UI-7, UI-21 and
UI-36 are checkable for Dark desktop only until those frames exist.

---

## 4. Anchors into the behaviour spec

This file never restates the behaviour spec. Use these anchors:

All section titles below were read at `7984aabf`, not on `master`.

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
| Behaviour acceptance criteria | `model-hub-implementation.md` §8 — *AC-1…AC-28, v3 addenda through 2026-08-09* |
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
