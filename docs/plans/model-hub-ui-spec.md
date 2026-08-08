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
| 03 | `qZhJ3` | 模型网关 03 — 全局顺序抽屉 |
| 04 | `XvCC4` | 模型网关 04 — 添加订阅 |
| 05 | `GDErR` | 模型网关 05 — 添加 API Key |
| 06 | `wItw4` | 模型网关 06 — 来源详情 · 型号管理 |
| 08 | `Doqav` | 模型网关 08 — 故障实况(网关接管中) |

There is no 07: it was removed during the design pass and the remaining frames
were deliberately **not** renumbered, so that every existing reference to "08"
keeps pointing at the same picture.

All seven frames are 1440×1100 Dark. Light and mobile variants are not drawn yet;
§3 states which acceptance items therefore cannot be checked yet.

### 0.2 Authority order

1. `model-hub.md` — behaviour, vocabulary, resolution. **§4.3 is the sole
   authority for the routing algorithm.** On any conflict, it wins and this file
   is the defect.
2. `model-hub-implementation.md` §8 — the behaviour acceptance ledger (AC-1…AC-21).
   §3 below does not duplicate, restate, or extend it.
3. This file — layout, copy, state reachability, interaction feedback.
4. `design.pen` — the pixels. Where this file and the frame disagree on a number,
   the frame is right and this file must be corrected, *unless* the number is
   marked `[derived]`.

This file **references anchors and never restates spec content**. If you want to
know what a chain is, read §4.6 there; this file only says where it is drawn.

### 0.3 Provenance markers

Every statement below carries one of three markers, because "the design says so"
and "somebody decided so while writing this" are not the same kind of fact and a
lane needs to know which one it is holding:

- `[frame]` — measured off the design file. Changing it is a design change.
- `[derived]` — not drawn; decided here because the frame does not cover the case
  (empty lists, overflow, keyboard paths). Changing it is a spec change to *this*
  file and needs no design pass.
- `[spec]` — owned by `model-hub.md`; repeated here only as a pointer.

### 0.4 Not in scope

Behaviour invariants (persistence, event fan-out, schema constraints, resolver
precedence). Those belong to §8 of the implementation plan. Where drawing the
frames surfaced a *missing* behaviour invariant, it is listed in the PR
description under 「建议移交 AC 账本」 for routing — not written here, and not
written into §8 by this lane.

---

## 1. Per-frame specification

### 1.0 Shared shell (present on all seven frames)

Six frames render the same chrome, and 06 renders a breadcrumb variant of it.
Specifying it once is not a shortcut: a shell duplicated across seven sections is
a shell that will drift in six of them.

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
| Run pill | Engine liveness | engine status | no | — |
| Tabs ×4 | Section nav | route | yes | Navigate; active tab gets the mint underline |
| Upstream module | Source inventory | `GET /api/models/sources` `[spec]` | rows: yes | Open 06 for that source |
| Dispatch rail | That upstream feeds gateway | derived, decorative | no | — |
| Gateway module | One group per backend, each with model rows | per-backend supply + chains `[spec]` | rows, collapse, 「全局顺序」 | Open 02 / expand / open 03 |
| Legend | Colour → meaning | static, but see UI-10 | no | — |

**Shared state machine**

| State | Entry | Exit |
| --- | --- | --- |
| Loading | Route entered, first payload outstanding | Payload arrives → Ready, or fails → Unreachable |
| Ready | Payload arrives | Any mutation re-renders in place `[derived]` |
| Empty (no sources) | `sources == []` | First source added → Ready |
| Unreachable (engine down) | Status request fails or reports stopped | Recovery → Ready |
| Partial | Sources load, per-backend supply does not | Retry succeeds → Ready |

Empty, Unreachable and Partial are **not drawn** `[derived]`. Required behaviour:

- Empty: upstream module keeps its head and footer and shows one line —
  「还没有来源。先添加一个订阅或 API Key。」 The gateway module shows its backend
  groups with 「没有可用来源」 per group rather than vanishing; a backend that
  exists is a fact independent of whether anything can supply it.
- Unreachable: the run pill flips to 「网关未运行」 and every derived column
  (current source, chain, takeover) renders `—`, **not** a stale last-known value.
  See D-3 and UI-20: a surface that cannot prove a fact must say so.
- Partial: only the sub-tree that failed degrades. A failed supply payload must
  not blank the source inventory, which loaded fine.

**Shared copy** — namespace `models.hub.shell.*` / `.upstream.*` / `.gateway.*` /
`.legend.*`. Both `ui/src/i18n/zh.json` and `en.json` must carry every key (they
are currently at exact parity, 3534 keys each; UI-11 keeps it that way).

| Key | 中文 | English |
| --- | --- | --- |
| `shell.title` | 模型网关 | Model Gateway |
| `shell.running` | 网关运行中 | Gateway running |
| `shell.stopped` `[derived]` | 网关未运行 | Gateway not running |
| `shell.tab.hub` | 模型网关 | Model Gateway |
| `shell.tab.usage` | 用量与额度 | Usage & quota |
| `shell.tab.backends` | Agent 后端 | Agent backends |
| `shell.tab.diagnostics` | 诊断 | Diagnostics |
| `upstream.heading` | 上游 | Upstream |
| `upstream.count` | {{count}} 个来源 | {{count}} sources |
| `upstream.group.native` | 本机原生 | Native · on this machine |
| `upstream.group.hub` | 网关持有 | Held by the gateway |
| `upstream.kind.nativeCredential` | 原生 · 本机凭据 | Native · local credential |
| `upstream.kind.subscription` | 订阅 | Subscription |
| `upstream.kind.apiKey` | API Key | API key |
| `upstream.state.supplyingNative` | 正在供给 {{backend}} · 不经网关 | Supplying {{backend}} · not via the gateway |
| `upstream.state.supplying` | 正在供给 {{backends}} | Supplying {{backends}} |
| `upstream.state.standby` | 待用 | Standby |
| `upstream.state.quotaExhausted` | 额度用尽 · 已暂停供给 | Quota exhausted · supply paused |
| `upstream.empty` `[derived]` | 还没有来源。先添加一个订阅或 API Key。 | No sources yet. Add a subscription or an API key first. |
| `upstream.addSubscription` | 添加订阅 | Add subscription |
| `upstream.addApiKey` | 添加 API Key | Add API key |
| `gateway.heading` | 网关 | Gateway |
| `gateway.rail` | 调度 | Dispatch |
| `gateway.globalOrder` | 全局顺序 | Global order |
| `gateway.modelCount` | {{count}} 个型号 | {{count}} models |
| `gateway.supply.nativeDirect` | 原生订阅直供 · 未经网关 | Supplied directly by native subscription · not via the gateway |
| `gateway.supply.viaGateway` | 网关供给 · {{source}} | Gateway supply · {{source}} |
| `gateway.supply.takenOver` | 已接管 · {{source}} | Taken over · {{source}} |
| `gateway.supply.none` `[derived]` | 没有可用来源 | No usable source |
| `gateway.row.followsGlobal` | 跟随全局顺序 | Follows global order |
| `gateway.row.overridden` | 已覆盖 | Overridden |
| `gateway.row.current` | 当前 {{source}} | Now: {{source}} |
| `gateway.row.currentTakeover` | 当前 {{source}}(接管) | Now: {{source}} (takeover) |
| `gateway.collapse` | 还有 {{count}} 个型号 | {{count}} more models |
| `legend.nativeDirect` | 原生直连 · 不经网关 | Native direct · not via the gateway |
| `legend.viaGateway` | 网关供给 | Gateway supply |
| `legend.connectedUnused` | 已接入 · 当前未被使用 | Connected · not currently used |
| `legend.takeover` | 接管中 · 临时改走 | Taken over · temporarily rerouted |
| `legend.note` | 链路按「全局顺序」自动派生;单个型号可单独覆盖 | Chains are derived from the global order; any single model can override it |

**Semantic ink** `[frame]` — four inks, each with exactly one meaning:

| Ink | Meaning | Where |
| --- | --- | --- |
| `$--cyan` `#3FE0E5` | native direct, not via the gateway | wire, card tint `#3FE0E50A` / border `#3FE0E54D`, tile `#3FE0E51A`, status text |
| `$--mint` `#5BFFA0` | gateway supply | wire, rail (`#5BFFA01A` chip / `#5BFFA033` line), order badges, primary buttons |
| `$--gold` `#FFC857` | takeover / temporarily rerouted; also ToS warning | 08 only (wire `#FFC857` @1.75, pills `#FFC8571A`+`#FFC8574D`), 04 note |
| `#FFFFFF26` | connected but not currently used | dim wire only |

Cyan's exclusivity is load-bearing, not decorative — see D-6 and UI-8.

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
| `TLrBM` + `pnYa0` rail | dispatch happens between the columns | decorative | no | — |
| `GLylJ` backend group | backend tile, name, model count, one supply line | per-backend supply | header: no | — |
| `Exx0a` model row | model id (mono 12), mode chip, current-source text | chain head per model | yes | Open 02 for `(backend, model)` |
| `ZM1pm` collapse row | `还有 N 个型号` | count of hidden rows | yes | Expand in place |
| `fwCwQ` 「全局顺序」 | — | — | yes | Open 03 |
| `FZUYI` wire layer | one path per supply relation + endpoint dots | derived supply set | no | — |
| `gzKRI` tooltip | why chains look derived | static | shown on the legend info icon | — |

**Card and row metrics** `[frame]`: upstream card 360×80, `padding [0,12]`,
`gap 10`, `radius 10`; tile 34×34 `radius 9`; name 12.5/700 Inter; detail 10.5
JetBrains Mono `#9BA3B8CC`; status 10.5/600. Backend group 616 wide, `$--background`
fill, `radius 12`; head 66 tall, `padding [0,14]`, `gap 7`, bottom border; backend
name 14/700. `rows` container `padding 8`, `gap 8`; model row 600×36, `radius 8`,
fill `#FFFFFF05`; model id 12 JetBrains Mono; mode chip `padding [3,8]` `radius 999`
fill `#FFFFFF0A`; current text 10.5 Inter `#9BA3B8CC`. Collapse row 600×24 with
**transparent fill and transparent stroke** — it is a row-shaped affordance, not a
card.

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
N = 3                                     # baseline visible rows
mustShow  = { m in models | m.state != nominal }        # hard: never collapsed
ranked    = sort(models - mustShow, by=(
               0 if m.hasOverride else 1,               # overrides outrank
               m.backendMenuIndex))                     # then the backend's own order
visible   = mustShow ++ take(ranked, max(0, N - |mustShow|))
collapsed = models - visible
render collapse row  iff  |collapsed| > 0
collapse label count = |collapsed|
```

Consequences worth spelling out, because each has already been got wrong once:

- `|mustShow| > N` ⇒ the group shows **more** than three rows. The baseline is a
  floor on information, not a cap on rows.
- The count in 「还有 N 个型号」 is `|collapsed|`, never `|models| - 3`. With two
  cooling models the group shows 5 and says `还有 {total-5} 个型号`.
- `|models| <= N` ⇒ **no collapse row at all**, not an empty one.
- Expanding is idempotent and does not re-rank: the order the user saw stays the
  order they get.

Other limits `[derived]`:

| Data | Rule |
| --- | --- |
| Long source name | Single line, ellipsis at the card's inner width (360 − 12×2 − tile 34 − gap 10 = 292). `title` attribute carries the full value. |
| Long base URL / masked key | Mono line truncates **from the middle**, keeping scheme+host and the last 4 key chars — the two ends are what identifies it. |
| Long model id | Mono, ellipsis at the `a` column; full value in `title`. |
| Many sources (> 6) | Upstream module grows to the `cols` track height (806) and then `upContent` scrolls; the head and footer stay pinned. Group labels scroll with the content. |
| Many backends (> 3) | `gwContent` scrolls; the rail line keeps spanning the visible track. |
| Zero supply relations | The wire layer renders nothing — no placeholder path. |
| Wires | Generated from the supply-relation set, never hand-placed; the frame's four paths are an instance of that generator, not a fixed asset. (UI-30.) |

---

### 1.2 Frame 02 `Q1dkS` — Route-chain editor

**The question it answers:** *for this one model, which sources will be tried, in
what order, and is that order mine or the product's?* Two dialogs, side by side,
are the two answers: follow-the-global-order, and I have overridden this model.

**Element inventory**

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| `zmWYg` head | `{model} · 来源链路` + backend name | route params | close icon | Dismiss |
| `UxCia` segmented | 跟随全局顺序 / 自定义此型号 | override presence | yes, 2 options | Switch mode; picking custom **forks** the chain |
| `y9mDvQ` label + `OL7EH` chip | 当前派生结果 / 3 跳 — or 这个型号的链路 / 已覆盖 | derived chain `[spec §4.6]` | no | — |
| `F2sqds` hop | ordinal badge, source name, effective upstream model id (mono), `全局 #n` tag | chain hop | follow: no | — |
| `Fq0MA` hop (custom) | same, minus the tag, plus up / down / remove | chain hop | yes | Reorder / remove |
| `HOQqF` 添加一跳 | — | eligible sources not yet in the chain | yes | Source picker `[derived]` |
| `dv2PI` / `c8E1o0` hint | what this mode implies for future sources | static per mode | no | — |
| `bG5Mc` 恢复跟随全局 | — | — | yes (custom only) | Drop the override |

**Metrics** `[frame]`: dialog 520 wide, `$--surface`, `border-strong`, `radius 14`;
head `padding [16,20]` `gap 4`; body `padding 20` `gap 14`; foot 61 tall
`padding [14,20]`, top border, fill `#FFFFFF05`. Follow variant 473 tall, custom
459. Segmented 211×37, `padding 3` `gap 3`, selected seg fill `#FFFFFF1A`
`radius 6`. Hop list 480 wide `padding 8` `gap 6` on `$--background` `radius 10`;
hop 464×52 `radius 8` (follow `#FFFFFF03`, custom `#FFFFFF08`); ordinal 22×22
`radius 6` fill `#5BFFA01A`; source 12/600 `#F5F1E8B3`; effective id 10.5 mono
`#9BA3B8B3`; tag `padding [3,8]` `radius 999`. Icon button 26×26 `radius 6`. Add
row 464×38 `radius 8`. Primary button `padding [8,14]` `radius 7` `$--mint`.

**State machine**

| State | Entry | Exit |
| --- | --- | --- |
| Follow | No override for `(backend, model)` | Any manual edit → Custom (implicit, immediate) |
| Custom | Override exists, or the user edited while in Follow | 恢复跟随全局 → Follow |
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
| `title` | {{model}} · 来源链路 | {{model}} · source chain |
| `mode.follow` | 跟随全局顺序 | Follows global order |
| `mode.custom` | 自定义此型号 | Customize this model |
| `derived.label` | 当前派生结果 | Derived result |
| `derived.hops` | {{count}} 跳 | {{count}} hops |
| `custom.label` | 这个型号的链路 | This model's chain |
| `custom.badge` | 已覆盖 | Overridden |
| `hop.globalRank` | 全局 #{{n}} | Global #{{n}} |
| `hop.add` | 添加一跳 | Add a hop |
| `hint.follow` | 跟着全局顺序走。以后新增的来源会自动排进这条链。 | Follows the global order. Sources you add later join this chain automatically. |
| `hint.custom` | 已脱离全局顺序。新增来源不会自动加入这条链。 | Detached from the global order. New sources will not join this chain. |
| `restore` | 恢复跟随全局 | Restore global order |
| `empty` `[derived]` | 现在没有来源能提供这个型号 | No source can supply this model right now |
| `close` | 关闭 | Close |
| `cancel` | 取消 | Cancel |
| `save` | 保存 | Save |

**Extreme data** `[derived]`: hop list scrolls past 6 hops (`480 × 6×52+5×6+16`);
the mono effective id truncates from the middle; a chain of one hop still shows the
ordinal `1` (the ordinal is the position, not a plurality marker); `全局 #n` is
omitted, not blanked, on hops that entered by override.

---

### 1.3 Frame 03 `qZhJ3` — Global order drawer

**The question it answers:** *when several sources can serve the same model, who
goes first?* One list, one answer, for every model that has not been overridden.

**Element inventory**

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| `qNs0K` head | 全局来源顺序 + one-line explanation | static | info, close | Tooltip / dismiss |
| `L7Tof` 不参与排序 | native-CLI sources, lock icon, 原生 tag | sources with native supply channel `[spec §4.1]` | no | — |
| `mSZ89` 网关来源顺序 | ordered gateway sources, grip, ordinal, name, mono detail, kind tag | the global order | drag, keyboard | Reorder |
| `DIwyc` hint | where new sources land; where to deviate | static | no | — |
| `kSQJO` foot | 取消 / 保存顺序 | dirty state | yes | Discard / persist |

**Metrics** `[frame]`: scrim `#05050BE0` over the full 1440×1100; drawer 460 wide,
full height, `$--surface`, `border-left 1 $--border-strong`; head `padding [18,20]`
`gap 6`; body `padding 20` `gap 18`; section `gap 8`; row 420×58 `radius 9`
`padding [0,12]` `gap 10`. Native row is cyan-tinted (`#3FE0E50A` / `#3FE0E54D`)
with a cyan `原生` tag (`#3FE0E51A` / `#3FE0E54D`); gateway rows are neutral
(`#FFFFFF08` / `$--border`) with a mint ordinal badge 22×22 `#5BFFA01A`. Foot 61
tall, top border, `space_between` → 取消 left of a mint 保存顺序.

**State machine**

| State | Entry | Exit |
| --- | --- | --- |
| Clean | Opened | Any reorder → Dirty |
| Dirty | Order changed locally | 保存顺序 → persisted; 取消 → discarded |
| Dragging | Pointer or keyboard grab active | Drop / Escape (Escape restores the pre-grab order) |
| Saving | 保存顺序 pressed | Success → close; failure → Error |
| Error | Persist rejected | Retry, or 取消 |
| Empty gateway order | No source can supply via the gateway | A source becomes eligible |
| Only native sources | Every source is native-CLI | A gateway-capable source is added |

`[derived]`: with an empty gateway order the section keeps its header and shows
「还没有可排序的来源」. **The 不参与排序 section is never hidden** when native
sources exist — its whole job is to answer "why isn't my Claude subscription in
this list", and hiding it re-raises exactly that question.

**Copy** — `models.hub.order.*`

| Key | 中文 | English |
| --- | --- | --- |
| `title` | 全局来源顺序 | Global source order |
| `subtitle` | 每个型号默认按这个顺序,挑第一个能用的来源。 | By default every model walks this order and takes the first usable source. |
| `section.excluded` | 不参与排序 | Not part of the order |
| `section.gateway` | 网关来源顺序 | Gateway source order |
| `section.gateway.badge` | 拖动排序 | Drag to reorder |
| `tag.native` | 原生 | Native |
| `native.reason` | 只供 {{backend}} · 凭据在本机 | {{backend}} only · credential stays on this machine |
| `hint` | 以后新增的来源默认排在最后。想让某个型号走别的顺序,到该型号里单独覆盖。 | Sources you add later go to the end. To give one model a different order, override it on that model. |
| `empty` `[derived]` | 还没有可排序的来源 | No sources to order yet |
| `save` | 保存顺序 | Save order |
| `cancel` | 取消 | Cancel |

**Extreme data** `[derived]`: the body scrolls past ~13 rows; ordinals renumber
contiguously from 1 after every reorder and never show gaps; a source in
`needs_action` keeps its position (removing it from the order as a side effect of a
dead credential would silently rewrite the user's decision — see D-10's reasoning
and UI-19); drag has a keyboard equivalent (UI-22).

---

### 1.4 Frame 04 `XvCC4` — Add subscription

**The question it answers:** *I have a paid subscription — do I let the CLI use it
directly, or hand it to the gateway?* Two dialogs, because the recommended answer
is **different per vendor** and for a reason that is legal, not technical.

**Element inventory**

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| head | `添加 {vendor} 订阅` + `host / plan` | vendor | close | Dismiss |
| `gI9r5` / `Fs6bj` option | radio, name, info icon, recommendation badge, one-line consequence | static per vendor | yes | Select |
| `uzelE` ToS note | why gateway-supplying a Claude subscription is out of scope | static, Claude only | no | — |
| `iF4LZ` hint | that both can be selected, and what that means | static per vendor | no | — |
| foot | 取消 / 去登录 | selection | yes | Dismiss / start OAuth `[spec §4.5]` |

**Metrics** `[frame]`: dialog 620 wide, `radius 14`, `$--surface`,
`border-strong`; Claude 424 tall, ChatGPT 365. Option card 580 wide `padding 14`
`gap 12` `radius 10`; **selected** = `#5BFFA00F` fill + `#5BFFA059` border; radio
16×16 `radius 999` `stroke $--mint @1.5`. Badge `padding [3,8]` `radius 999`
`#5BFFA01A` / `#5BFFA04D`. ToS note 524 wide `padding [9,11]` `radius 8`, gold
(`#FFC8571A` / `#FFC8574D`), `triangle-alert` icon. Foot 61 tall, mint 去登录 with
`arrow-right`.

The recommendation flips per vendor `[frame]`: Claude = 原生使用 **推荐** /
登录为网关上游 **次选**; ChatGPT = 登录为网关上游 **推荐** / 原生使用
**支持,不推荐**. Frame order follows the recommendation — the recommended option
is first in both dialogs.

**State machine**

| State | Entry | Exit |
| --- | --- | --- |
| Default | Opened | — (the recommended option is pre-selected) |
| Both selected | User selects the second option too | Deselect one |
| Awaiting sign-in | 去登录 pressed | OAuth completes → source created; user abandons → Dismissed |
| OAuth failed | Provider or engine failure `[spec models.oauth.*]` | Retry, or 取消 |
| Engine unavailable | Gateway not running and gateway-upstream was chosen | Engine recovers |
| Already bound | This account is already another source `[spec models.oauth.binding_failed]` | Choose another account |
| Loading | — | Not applicable: nothing is fetched before the dialog opens |

`[derived]`: choosing 登录为网关上游 while the engine is down must fail **before**
the browser hand-off, with 「网关没有响应,请重试」 — sending someone through an
OAuth flow that has nowhere to land is the most expensive possible way to report
that the engine is down.

**Copy** — `models.hub.addSub.*`

| Key | 中文 | English |
| --- | --- | --- |
| `title` | 添加 {{vendor}} 订阅 | Add {{vendor}} subscription |
| `subtitle` | {{host}} / {{plans}} | {{host}} / {{plans}} |
| `opt.native` | 原生使用 | Use natively |
| `opt.native.desc.claude` | Claude Code 直接用这个订阅,凭据只留在本机,不经过网关。 | Claude Code uses this subscription directly; the credential stays on this machine and never goes through the gateway. |
| `opt.native.desc.chatgpt` | Codex 直接用这个 ChatGPT 账号登录,不经过网关。 | Codex signs in with this ChatGPT account directly, not through the gateway. |
| `opt.hub` | 登录为网关上游 | Sign in as a gateway upstream |
| `opt.hub.desc.claude` | 把这个订阅交给网关,供给 Codex、OpenCode 等其他 Agent。 | Hand this subscription to the gateway so it can supply Codex, OpenCode and other Agents. |
| `opt.hub.desc.chatgpt` | 网关把它供给 Codex 和其他 Agent,用量、额度、接管都能看到。 | The gateway supplies it to Codex and other Agents, with usage, quota and takeover all visible. |
| `badge.recommended` | 推荐 | Recommended |
| `badge.secondary` | 次选 | Second choice |
| `badge.supportedNotRecommended` | 支持,不推荐 | Supported, not recommended |
| `tos.claude` | 订阅条款只授权你本人在 Claude 官方客户端里使用。转供其他 Agent 属于超范围使用,账号可能被限制。 | The subscription terms authorize only you, inside Claude's official clients. Supplying it to other Agents is out-of-scope use and the account may be restricted. |
| `hint.claude` | 两个都选也可以:原生优先用,额度用完自动走网关。 | You can pick both: native goes first, and the gateway takes over when the quota runs out. |
| `hint.chatgpt` | 原生登录不走网关:额度用完不会自动接管,也看不到用量。 | A native sign-in bypasses the gateway: nothing takes over when the quota runs out, and usage stays invisible. |
| `signIn` | 去登录 | Sign in |
| `cancel` | 取消 | Cancel |

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
| `zVU7c` 测试连通 + hint | optional probe, explicitly not a prerequisite | — | yes | Run the probe, render its result in place |
| `S0pOY2` 添加 | — | form validity | yes | Run the add action (connect + identify + fetch) |
| `OT0Xf` state ② | spinner, 连接中…, what is happening, 通常 1–3 秒 | in-flight | 取消 only | Abort |
| `C72yS` state ③ strip | classified cause, then request evidence | probe result | no | — |
| ③ foot | 取消 / 仍要添加 / 重试 | — | yes | Dismiss / persist unverified / re-probe |
| `vKiIo` state ④ strip | connected + authenticated, interface undetermined, with evidence | probe result | no | — |
| `WZyA8` selector | the three interface types | static | yes, **nothing pre-selected** | Select one; enables 仍要添加 |
| ④ foot | 取消 / 仍要添加 (disabled until a choice) | selection | yes | — |
| `sqZa9` success note | that the dialog closes straight into 06 | static | no | — |

**Metrics** `[frame]`: dialog 560 wide; ① 441 tall, ③ 514, fragment ② 110,
fragment ④ 246. Head `padding [16,20]` `gap 4`; body `padding 20` `gap 14`; field
`gap 6`; input 520×36 `radius 8` fill `#FFFFFF08`; field hint 10.5 JetBrains Mono
`#9BA3B8B3`. Test button 95×33 `radius 7` neutral. Result strip 520 wide
`padding [11,13]` `gap 10` `radius 9`: red `#FF6B6B14`/`#FF6B6B40` for ③, gold
`#FFC85714`/`#FFC85759` for ④, mint `#5BFFA014`/`#5BFFA040` for the success note.
State ④ selector 458×34 `padding 3` `gap 3`, **all three segments fill
`#00000000`**; 仍要添加 in ④ is `#5BFFA059` — the dimmed-primary disabled style.
Foot 61 tall, top border.

Two of those numbers are the design carrying a product rule, not styling: an
unselected segmented control and a dimmed primary button are how the picture
itself refuses to guess. A build that pre-selects a segment is not a cosmetic
deviation; it has implemented the opposite decision.

**State machine**

| State | Entry | Exit |
| --- | --- | --- |
| ① Default | Dialog opened | Add pressed → ②; 测试连通 → probe result inline |
| ② Adding | Add pressed | Success → dialog closes into 06; classified failure → ③; undetermined interface → ④; 取消 → ① |
| ③ Failure (address / auth / network) | Probe classified the failure | 重试 → ②; 仍要添加 → persist unverified, close; 取消 → dismiss |
| ④ Interface undetermined | Reachable **and** authenticated, response shape matches no known interface | Pick one + 仍要添加 → persist with a user-specified interface; 取消 → dismiss |
| Empty | — | Not applicable: a form has no empty state |
| Credential-invalid | Auth failure is one of ③'s three causes | As ③ |
| Engine unavailable `[derived]` | Gateway not running | Add is blocked with 「网关没有响应,请重试」; the form keeps its values |

`[derived]` for ④'s exit: until a segment is chosen, 仍要添加 is **disabled** —
this is the only place in the product where a failure state withholds its escape
hatch, and the reason is that "add anyway" here would have to write a guess. ③'s
仍要添加 is always live because there the unknown is *the endpoint's health*, which
the product can honestly record as unverified; ④'s unknown is *a value it would
have to invent*. (D-3, D-4, UI-20.)

**Copy** — `models.hub.addKey.*`

| Key | 中文 | English |
| --- | --- | --- |
| `title` | 添加 API Key | Add API key |
| `subtitle` | 「添加」时自动连一次:认出接口 + 拉取型号列表 | Add connects once: it identifies the interface and fetches the model list |
| `field.name` | 名称(可选) | Name (optional) |
| `field.baseUrl` | Base URL | Base URL |
| `field.baseUrl.hint` | 粘贴任何中转 / 聚合 / 自建服务的地址即可,Avibe 会自己认出接口 | Paste any relay, aggregator or self-hosted address — Avibe identifies the interface itself |
| `field.apiKey` | API Key | API key |
| `test` | 测试连通 | Test connection |
| `test.hint` | 可选 · 不是「添加」的前置条件 | Optional · not a prerequisite for Add |
| `submit` | 添加 | Add |
| `adding` | 连接中… | Connecting… |
| `adding.detail` | 连上 + 认出接口 + 首次拉取型号列表 · 通常 1–3 秒 | Connect, identify the interface, fetch the model list · usually 1–3s |
| `fail.subtitle` | 校验失败不阻止添加 · 你可以自担风险保存 | A failed check does not block Add · you can save at your own risk |
| `fail.auth` | 鉴权失败:401 Unauthorized | Authentication failed: 401 Unauthorized |
| `fail.auth.detail` | 检查 API Key 是否有效 | Check whether the API key is valid |
| `fail.address` `[derived]` | 地址不对:404 Not Found | Wrong address: 404 Not Found |
| `fail.network` `[derived]` | 网络不通:连接超时 | Network unreachable: connection timed out |
| `addAnyway` | 仍要添加 | Add anyway |
| `retry` | 重试 | Retry |
| `undetermined.title` | 连上了、也通过了鉴权 —— 但认不出它说哪种接口 | Connected and authenticated — but we cannot tell which interface it speaks |
| `undetermined.detail` | {{request}} · {{status}} · 返回结构对不上任何一种已知接口 | {{request}} · {{status}} · the response shape matches no interface we know |
| `undetermined.label` | 接口类型 · 这一次由你指定 | Interface type · you specify it this once |
| `undetermined.hint` | 选一种才会保存 · 之后可在来源详情里改 | Pick one to save · you can change it later in source details |
| `protocol.anthropicMessages` | Anthropic Messages | Anthropic Messages |
| `protocol.openaiResponses` | OpenAI Responses | OpenAI Responses |
| `protocol.openaiChatCompletions` | OpenAI Chat Completions | OpenAI Chat Completions |
| `success.title` | 成功 → 弹窗关闭,直接进入「来源详情 · 型号管理」 | Done → the dialog closes and you land on Source details · Models |
| `success.detail` | 型号列表、重新拉取、推理强度档位都在那里维护 | The model list, refetch and reasoning tiers are all maintained there |
| `cancel` | 取消 | Cancel |

The three protocol strings above are the **only** protocol names anywhere in the
product surface (UI-12). They are identifiers, identical in both locales.

**Extreme data** `[derived]`: the evidence line is mono and truncates from the
middle, keeping method+host and the status code; a base URL with no scheme is
normalized to `https://` before the probe and the normalized form is what the
field shows afterwards (so the user can see what was actually tried); an empty
名称 falls back to the URL host as the display name, and that fallback is applied
at render time, never written into the name field.

---

### 1.6 Frame 06 `wItw4` — Source detail · model management

**The question it answers:** *which models does this one source have, which of
them do I actually want, and what reasoning tiers does each accept?* Nothing else
— chain membership is set on 01/02, ordering on 03.

**Element inventory**

| Element | Displays | Data source | Interactive | On activate |
| --- | --- | --- | --- | --- |
| `VV7jc` breadcrumb | 模型网关 / 上游 → source name + kind pill | route | back icon | Return to 01 |
| `sugad` source bar | health dot, 连通正常, latency + last check, mono `host · N models, M connected` | source + last probe | 测试连通 / 重新拉取 / 添加模型 | Probe / refetch / append an editable row |
| `myA8k` header | 型号 ID (250) · 录入 (84) · 推理强度 (470, with info) · 接入 (110) | static | no | — |
| `OM5PH` row | model id, entry-kind pill, tier chips, toggle + label, overflow icon | one model | tiers, toggle, overflow | Edit tiers / connect / row menu |
| `p2JwTz` tiers | chips, or 未设置档位 + `+ 添加档位` | per-model tier list | yes | Enter edit mode |
| `eVavA` tiers (editing) | removable chips + text input + 回车添加 · 任意文本 | local edit | yes | Add / remove a tier |
| `MdjR0` toggle | connected or not | per-model | yes | Connect / disconnect |
| `nN4TZ` manual row | editable id input, 手动添加 pill, tier affordance, 取消 / 添加 | local draft | yes | Commit or discard |
| `Q83BF` add row | 添加模型 + when to use it | — | yes | Append a manual draft row |
| `tF3Bh` footnote | scope of this page; that tiers are yours to type; that the interface type is identified at add time and not shown | static | no | — |
| Quiet badge `[derived]` | that this source's interface was specified by hand | source provenance | hover | Tooltip naming the interface |

**Metrics** `[frame]`: source bar 1120×64 `padding [14,18]` `gap 14` `radius 12`.
Table 1120 wide `radius 12`; header 36 tall `padding [10,18]` `gap 16` fill
`#FFFFFF05`, labels 11/600 `#FFFFFF73`; row 54 tall `padding [11,18]` `gap 16`
with a bottom border; column widths 250 / 84 / 470 / 110 and they must match the
header exactly. Tier chip `padding [4,9]` `radius 999` fill `#FFFFFF0F`; empty
label 11 `#FFFFFF59`; editing input `radius 999` fill `#5BFFA00F` stroke
`$--mint`; toggle 34×19 `radius 999` (`$--mint` when on); label 11
`#FFFFFF8C`. Manual draft row is mint-washed (`#5BFFA00D`, `#5BFFA033` top and
bottom). Footnote 11.5 `#FFFFFF8C`, one line at 1026 within the 1120 track.

**State machine**

| State | Entry | Exit |
| --- | --- | --- |
| Ready | Source detail loaded | — |
| Empty (no models) | Discovery returned nothing and nothing was added by hand | Manual add, or a successful refetch |
| Refetching | 重新拉取 pressed | New list arrives → Ready (diffed, see below); failure → Error |
| Probing | 测试连通 pressed | Result replaces the source-bar status line |
| Row · tiers editing | Tier area activated | Enter commits a tier; blur / Escape exits |
| Row · toggling | Toggle pressed | Success → new position; failure → reverts, with the direction it failed |
| Manual draft | 添加模型 pressed | 添加 commits; 取消 discards |
| Error (refetch failed) | Refetch rejected | The **previous list is kept**; the bar carries the failure |
| Credential-invalid | Source is `needs_action` `[spec §4.5]` | Repair from the source bar |
| Interface specified by hand | Source provenance says so | Never — the badge is permanent while true |

Three `[derived]` rules:

- **Refetch is a diff, not a replacement.** Manually added models survive it;
  removed-upstream models are marked, not deleted, when they are connected —
  deleting something the user switched on, because a vendor's `/models` changed
  shape, silently unconfigures them.
- **Empty state** keeps the header row and the add row and shows
  「这个来源没有返回型号。可以手动添加,或重新拉取。」 An empty table with a live
  add row is the shortest path out.
- **Credential-invalid** keeps the whole table visible and read-only-ish: you can
  still see what you had configured. Hiding the inventory because the key expired
  destroys the only copy of the user's intent.

**Copy** — `models.hub.sourceDetail.*`

| Key | 中文 | English |
| --- | --- | --- |
| `breadcrumb` | 模型网关 / 上游 | Model Gateway / Upstream |
| `status.healthy` | 连通正常 | Connection healthy |
| `status.detail` | · 延迟 {{latency}} · 最近校验 {{time}} | · {{latency}} · last checked {{time}} |
| `summary` | {{host}} · {{total}} 个型号,已接入 {{connected}} 个 | {{host}} · {{total}} models, {{connected}} connected |
| `action.test` | 测试连通 | Test connection |
| `action.refetch` | 重新拉取 | Refetch |
| `action.addModel` | 添加模型 | Add model |
| `col.id` | 型号 ID | Model ID |
| `col.entry` | 录入 | Entry |
| `col.tiers` | 推理强度 | Reasoning tiers |
| `col.connected` | 接入 | Connected |
| `entry.auto` | 自动拉取 | Auto-fetched |
| `entry.manual` | 手动添加 | Added manually |
| `tiers.empty` | 未设置档位 | No tiers set |
| `tiers.addFirst` | + 添加档位 | + Add tier |
| `tiers.add` | + 档位 | + Tier |
| `tiers.inputHint` | 回车添加 · 任意文本 | Enter to add · any text |
| `connected.yes` | 已接入 | Connected |
| `connected.no` | 未接入 | Not connected |
| `addRow.hint` | 拉取不到、或只想接入其中一个时用 | Use this when a model is not discoverable, or when you only want one of them |
| `empty` `[derived]` | 这个来源没有返回型号。可以手动添加,或重新拉取。 | This source returned no models. Add one by hand, or refetch. |
| `interfaceBadge` `[derived]` | 接口由你指定 | Interface set by you |
| `interfaceBadge.tooltip` `[derived]` | 添加时没能自动认出,当前按「{{protocol}}」处理。可以改。 | Could not be identified automatically at add time; currently handled as "{{protocol}}". You can change it. |
| `footnote` | 这里只管「这个来源有哪些型号」。型号走哪条链,到总览页改。档位自己填,两种录入方式都一样。接口类型添加时自动认出、页面不显示;只有当初没认出、由你指定过的来源,标题旁才带一枚安静徽标。 | This page answers only "which models does this source have". Which chain a model takes is set on the overview. Tiers are yours to type, the same for both entry kinds. The interface type is identified when the source is added and is not shown here; only a source whose interface you had to specify yourself carries a quiet badge next to its title. |

The quiet badge is the one exception to "the user does not perceive the supply
mechanism" (D-8), and 06 does **not** get a second header state drawn for it: it
is one element in an existing row, so the footnote carries the rule instead. That
was a cost decision, made explicit here so nobody reads the absence of a frame as
the absence of the requirement.

**Extreme data** `[derived]`: the table does not collapse — the whole point of the
page is the full inventory, so it scrolls (the frame's 12 rows are an instance, not
a limit); long model ids truncate at 250 with the full value in `title`; a tier
list wider than 470 wraps to a second line and grows the row rather than
clipping — tiers are user-typed, so an arbitrary count is normal input, not an
edge case; tier strings are free text and are neither validated nor
case-normalized (D-5); `{{total}}`/`{{connected}}` must be plural-safe in English
at 0 and 1 (UI-14).

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
| `pill` | {{count}} 处接管中 | {{count}} takeovers active |
| `chip` | 接管中 | Taken over |

**Extreme data** `[derived]`: with N takeovers the pill says N and each affected
group carries its own chip — there is no "and others" summarization, because the
one question a takeover raises is *which one*. Gold paths are generated per
rerouted relation, so overlapping wires must remain individually traceable
(distinct routing, not stacked identical curves).

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

**D-3 — When identification fails, ask honestly instead of guessing.** State ④ is
the single protocol selector in the product; nothing is pre-selected and
「仍要添加」 stays disabled until a choice exists.
*Why:* **guessing stores an unverifiable value that fails later, at request time,
far from the moment the user could have fixed it in one click.** One question now
is cheaper than a wrong value forever. This is also why the *picture* enforces it:
an unselected control and a dimmed button cannot silently default.

**D-4 — A connectivity check is information, not a gate.** 「仍要添加」 is always
available in state ③, and 测试连通 is labelled as optional.
*Why:* the product cannot distinguish "your key is wrong" from "the vendor is down
this minute". Blocking on a probe converts the product's uncertainty into the
user's dead end. Note this does not weaken D-3: ③'s unknown is the endpoint's
health, which can honestly be recorded as unverified; ④'s unknown is a value that
would have to be invented.

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

**D-9 — Chains are derived, not hand-wired.** One global order, plus a per-model
override.
*Why:* N sources × M models of manual wiring is a configuration surface nobody can
hold in their head, and the order is the only part users actually have an opinion
about.

**D-10 — Native-CLI sources do not participate in the order, and say so where the
order lives.** Frame 03's 不参与排序 section is never hidden while such a source
exists.
*Why:* a native credential can serve only its own sanctioned client, so placing it
in a shared order would promise an arbitration that cannot happen. The section
exists to answer "why isn't my Claude subscription in this list" *before* it is
asked. `[spec §4.1]`

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
`model-hub-implementation.md` §8 (AC-1…AC-21) and are neither duplicated nor
extended here.

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
*Check:* enumerate computed `color`, `background-color`, `border-color` and SVG
`stroke` across the seven surfaces; look each up in §1.0's ink table or the
per-frame metrics.
*Criterion:* every value has a row. An unlisted literal hex is a finding, whatever
it looks like.

**UI-5 — Font role assignment is total.**
*Check:* for every text node, read `font-family`.
*Criterion:* identifiers, URLs, masked keys and request evidence resolve to
JetBrains Mono; all prose resolves to Inter; nothing falls back to a system font.

**UI-6 — Dialog and drawer geometry matches per frame.**
*Check:* measure each container.
*Criterion:* 02 = 520 wide; 03 = 460 wide, full height, left border only; 04 = 620;
05 = 560; all with head `padding [16,20]`, body `padding 20` `gap 14`, foot 61;
scrim `#05050BE0` over 1440×1100.

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

**UI-9 — Mint, gold and `#FFFFFF26` each have exactly one referent, per §1.0's ink
table.**
*Check:* as UI-8, per ink.
*Criterion:* mint ⇒ gateway supply or a primary action; gold ⇒ takeover or a ToS
warning; `#FFFFFF26` ⇒ a connected-but-unused wire.

**UI-10 — The legend keys and the ink classes actually rendered on the page are in
bijection.**
*Check:* on a nominal page and on a page with a takeover, compare the legend's keys
against the distinct inks present in the wire layer.
*Criterion:* nominal = 3 keys; takeover = 4. A legend key with no corresponding
element, or an ink with no key, fails. (This is the item that catches a takeover
shipped without its legend entry.)

### Copy

**UI-11 — Every string on these seven surfaces resolves through an i18n key, and
`zh.json` / `en.json` have identical key sets.**
*Check:* render each surface with the locale forced to `en` and confirm no Chinese
text remains; then diff the two files' key sets.
*Criterion:* zero hardcoded literals in the components; the key-set difference in
both directions is empty (they are at parity today at 3534 keys each).

**UI-12 — The set of surfaces that renders an interface-protocol name is exactly
{frame 05 state ④ selector, frame 06's quiet-badge tooltip}.**
*Check:* search the rendered DOM of all seven surfaces for the three protocol
strings, in both locales.
*Criterion:* hits occur only in those two places. Stated as an equality on a set,
so a *new* surface that leaks a protocol name fails it too — which a "must not
appear on 01/02/03/04/06/08" phrasing would not.

**UI-13 — Every product noun rendered on these surfaces has a row in
`model-hub.md` §3's vocabulary table.**
*Check:* extract the nouns from the copy tables in §1 and look each up in §3.
*Criterion:* every one has a row. **Blocked** until the spec lane lands §3's
gateway-arc revision — master's table still bans 网关, which the owner-approved
frames use as their primary noun. Re-run this item after that lands; it is a
cross-lane dependency, not a design defect.

**UI-14 — Every count-bearing string is grammatically correct in English at 0, 1
and 2.**
*Check:* render each interpolated count key at those three values.
*Criterion:* no `1 models`, no `0 takeovers active` where the element should be
absent, no fused numeral+noun that cannot pluralize. Keys to cover:
`upstream.count`, `gateway.modelCount`, `gateway.collapse`, `chain.derived.hops`,
`sourceDetail.summary`, `takeover.pill`.

**UI-15 — Copy states consequences, not mechanisms or rationale.**
*Check:* read every string in §1's tables and ask, for each, "does this tell me
what happens to me?"
*Criterion:* no string names an internal mechanism, and no string argues for a
design decision — the arguments live in §2. (This item exists because two strings
failed it during the design pass and had to be rewritten.)

### State reachability

**UI-16 — Every state in §1's state tables is reachable, and each has a named
trigger.**
*Check:* walk §1's state tables; for each row, perform the entry condition —
directly, or by serving the payload that produces it.
*Criterion:* every state renders. An unreachable state is either a missing
implementation or a spec row that should be deleted; both are findings.

**UI-17 — Every list has an empty state that keeps its frame and offers the exit.**
*Check:* with zero sources, zero models on a backend, and zero models on a source
detail page.
*Criterion:* the module head and footer survive; the message is the one in §1; the
relevant add affordance is present and enabled. A list that vanishes fails.

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

**UI-22 — Every interactive element has hover, focus-visible, disabled and — when
it mutates — pending.**
*Check:* tab through each surface, then hover each control.
*Criterion:* focus is always visible without a mouse; disabled uses the
dimmed-token style (`#5BFFA059` for a dimmed primary); a mutating control shows
pending and cannot be double-fired.

**UI-23 — Reordering in frame 03 is fully keyboard-operable.**
*Check:* with no mouse, focus a row, move it, and confirm the result.
*Criterion:* a documented key moves a row; ordinals renumber contiguously from 1;
Escape during a grab restores the pre-grab order; the same order persists as a
drag would.

**UI-24 — The collapse row is a real control that discloses and never hides an
active state.**
*Check:* activate it by keyboard; then serve a group where 5 of 15 models are
cooling.
*Criterion:* it is focusable and announces expanded/collapsed; the group shows all
5 non-nominal rows plus the baseline, and the label's count equals the number of
rows actually hidden — not `total − 3`.

**UI-25 — The tier editor accepts arbitrary text, commits on Enter, and never
supplies a value.**
*Check:* on a model with no tiers, open the editor and type an unfamiliar string.
*Criterion:* the empty state reads 未设置档位 with nothing preselected; Enter
commits the string as typed, without validation or case normalization; each chip
removes individually; leaving without typing leaves the list empty.

**UI-26 — Frame 06's connect toggle is optimistic and reports its own failure
direction.**
*Check:* force the mutation to fail on connect, then on disconnect.
*Criterion:* the toggle reverts to its prior position and the row states which
direction failed. A silent revert fails the item — it is indistinguishable from
the user's own click not landing.

**UI-27 — Every failure state offers a way out that is not a mutation.**
*Check:* reach frame 05 ③ and ④, a failed order save, and a failed refetch.
*Criterion:* 取消 (or 关闭) is present and enabled in all of them.

### Extreme data

**UI-28 — The collapse predicate is implemented as written in §1.1.**
*Check:* serve four fixtures for one backend: 2 models; 3 models; 15 models all
nominal; 15 models with 5 non-nominal.
*Criterion:* no collapse row at 2 or 3; 3 rows + 「还有 12 个型号」; 5+ rows with the
label reading the true hidden count. Priority order within the visible set is
override-first, then the backend's own menu order.

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
*Criterion:* `total` equals the row count, `connected` equals the toggled-on count,
the pill count equals the number of groups carrying a takeover chip, and
visible + collapsed equals the group's stated model count.

**Total: 32 items (UI-1 … UI-32).** One is blocked on an external dependency
(UI-13, on the spec lane's §3 revision). Light-theme and mobile variants are not
drawn, so UI-1…UI-7 and UI-21 are checkable for Dark desktop only until those
frames exist.

---

## 4. Anchors into the behaviour spec

This file never restates the behaviour spec. Use these anchors:

| Question | Authority |
| --- | --- |
| What the product promises the user | `model-hub.md` §2 |
| Which nouns UI copy may use | `model-hub.md` §3 (pending the gateway-arc revision; see UI-13) |
| What a source is and what it carries | `model-hub.md` §4.1 |
| How supply is consumed per Agent | `model-hub.md` §4.2 |
| **How a request resolves to a source — the sole authority** | `model-hub.md` §4.3 |
| Whether eligibility is client- or server-decided | `model-hub.md` §4.4 |
| Source states, self-healing classes, `detail_key` vocabulary | `model-hub.md` §4.5 |
| What a chain is; capability vs runnable | `model-hub.md` §4.6 |
| OpenCode identifier scheme | `model-hub.md` §4.8 |
| Which frames are the UI source of truth | `model-hub.md` §5 |
| Modes and native-config migration | `model-hub.md` §6 |
| Behaviour acceptance criteria | `model-hub-implementation.md` §8 |
| Frozen wire contracts | `docs/plans/model-hub-contracts/` |
| Probe result shape | `model-hub-contracts/probe-result.schema.json` |
| Source shape | `model-hub-contracts/source.schema.json` |
| Chain shape | `model-hub-contracts/agent-chain.schema.json` |
| Routes | `model-hub-contracts/api.md` |

Two boundaries worth stating explicitly, because they are the two places a lane
would otherwise write the same thing twice:

- `model-hub.md` §5 owns the frame-to-content contract at spec grain (which frame
  is authoritative for which surface). §1 here owns the implementation grain
  (element inventory, states, verbatim copy, extreme data). If §5 and §1 disagree
  about *what a frame contains*, §5 wins and §1 is the defect.
- `model-hub-implementation.md` §8 owns behaviour acceptance. §3 here owns visible
  acceptance. A candidate item that would pass or fail regardless of what is on
  screen belongs in §8, not here.
