# Model Hub — design-fidelity audit (frames 01 / 02 / 06)

Measured 2026-08-18 against the local Incus regression build at
`/admin/settings/models`, Dark theme. Design authority is `design.pen`; the
surface authority is `model-hub-ui-spec.md`. Per spec §0.2, **where a number in
the spec and a number in the frame disagree, the frame wins**; element
*presence* remains the spec's domain.

Frames compared:

| Frame | Node | Live surface |
| --- | --- | --- |
| 01 总览 | `pWQfB` | overview (`SettingsModelsPage` + `GatewayModule` + `SourcesCard` + `AgentCard`) |
| 02 路由链编辑 | `Q1dkS` | `RouteChainDialog` |
| 06 来源详情·型号管理 | `wItw4` | `SourceDetailPanel` |

Method: html-css export of each frame with inline computed styles, then a
DOM/`getComputedStyle` dump of the live surface at the same grain, compared
box-by-box. Frame viewport is 1440×1100; the live grid is anchored
(`.model-hub-overview-body` 1120×854, `.model-hub-overview-grid` 1120×806 —
both exactly the frame's values), so the measured viewport difference does not
affect the comparison.

## What already matches exactly

Recorded so a later pass does not "fix" a conforming value:

- Shell offsets: main 240, padding 36/40, gap 22, header at 280,36.
- Page title 26px/700 `#F5F1E8`, 52×37; runtime pill 81×23.
- Tabs strip: 1120 wide, gap 4, 1px bottom border, 2px `#5BFFA0` active
  indicator, tab width 114, padding-x 14, gap 7, icon 14, label 13px 600/400.
- Columns 384 / 72 rail / 632, gap 16. Panel radius 14, fill `#0E0E18`,
  border 1px `rgba(255,255,255,.08)`. Panel heads 56px, padding 0 14px.
- Upstream: content padding 12; card 358×80 r10, padding 0 12px, gap 10;
  tile 34 r9; footer 56, padding 0 14px, gap 8, top border.
- Gateway: content padding 8; agent head band 66, padding 0 14px, gap 7;
  agent tile 30 r9; rows padding 8 at 44px pitch. (The band and its 4px inset
  are the merged #1526 fix and are correct.)
- Model row box 596×36, padding 0 12px, gap 10, fill `rgba(255,255,255,.02)`;
  model name 12px/500; current text 10.5px/400 `rgba(155,163,184,.8)`;
  chevron 15.
- Upstream group label 10px/700 `rgba(255,255,255,.35)`; agent name 14px/700.
- Frame 06 table columns: x 299 / 565 / 665 / 1151, widths 250 / 84 / 470,
  gap 16, rows `align-items: center`. Source bar 1120×66 r12, padding
  14px 18px, gap 14; tile 36 r9.
- Frame 02 dialog structure: 520 wide, head with mono subtitle, body label,
  bordered hop container with 添加一跳 as its last row, 按来源顺序重排 mint
  link, persisted-configuration hint, 取消 / 保存 foot. Dialog actions
  54×33 / 52×33 r7 12px match `--model-hub-dialog-action-*`.

## A — Structural

| ID | Finding | Design | Live |
| --- | --- | --- | --- |
| A1 | Source detail replaces the tabs strip instead of rendering below it | frame 06 keeps the tabs strip at y=96 and puts the source bar at y=160; back button in the header at y=41 | `.model-hub-source-detail` starts at y=96, no tabs strip, back button at y=33 — the whole detail view sits ~64px high |
| A2 | Per-row overflow action missing on discovered models | frame 06 draws `···` on **every** table row, including `自动拉取` ones | `SourceDetailPanel.tsx:804` renders `ManualModelMenu` only when `model.origin === 'manual'`; the 230px action column is empty (`230×0`) on discovered rows |
| A3 | 添加一跳 selector is hand-rolled instead of the product's standard anchored selection surface | spec §1.2: "添加一跳 opens the product's standard anchored selection surface without assigning new frame geometry" — that surface is `ui/components/ui/combobox.tsx` (`Popover` + `Command` + `CommandInput` + `CommandGroup`), itself already design-anchored | `RouteChainDialog.tsx:964-1055` builds its own `Popover` + `section`/`p`/`button` list. See A3a–A3e below |
| A4 | Undesigned overview surfaces — resolved 2026-08-23 | frame 01 correctly ends after its graph body and legend | `高级` was deleted; `最近切换` moved out of the overview and into the registered `日志` tab, whose read is lazy and independent of first paint |

A3 consequences, all observed:

- **A3a** No filter for 45 candidates across 2 sources; `Combobox` supplies
  `CommandInput` for exactly this.
- **A3b** Fill is `bg-background` (`#080812`) while the base `PopoverContent`
  uses `bg-panel` (= `--surface`, `#0e0e18`). The panel is therefore *darker*
  than the dialog it floats over.
- **A3c** No elevation: measured `box-shadow` is transparent, so with A3b the
  panel reads as a hole punched through the dialog rather than a layer above it.
- **A3d** The 300px panel flips above its trigger and overshoots the dialog
  (panel top y=148 vs dialog top y=300), overlaying the page title and tabs
  strip. `collisionPadding` bounds it to the *viewport*, not to the dialog.
- **A3e** The confirm button is stranded: the scroll list is 241px inside a
  300px panel, so 添加 floats in ~50px of dead space with no separator or
  footer band. The active candidate's focus ring plus `aria-pressed` mint fill
  reads as a focused text `Input`, not a selected list row.
- **A3f** The list does not scroll — the original 「无法滚动，是定死的」, which
  §I below wrongly cleared. The CSS bound is fine and the box really is
  overflowing; the wheel never reaches it. `@radix-ui/react-dialog` mounts
  `RemoveScroll` on its Overlay with `shards: [contentRef]`, and
  `react-remove-scroll@2.7.2` keeps a module-level `lockStack` whose
  `shouldPrevent` early-returns unless the top lock owns the event. A
  body-portalled **non-modal** `PopoverContent` is in neither the lock nor any
  shard, so every wheel inside it is `preventDefault()`ed by the dialog's lock.
- **A3g** The 来源 column is gone. Frame 02 labels each candidate with its
  source; `2c16c96af` (#1526) dropped that label while restyling the picker, so
  two identically-named models from different upstreams are now
  indistinguishable. A regression introduced by the previous fidelity pass, not
  an original gap.

## B — Radius

The token scale is `ui/src/index.css:99-105`
(`xs 4 / sm 6 / md 8 / lg 12 / xl 16 / 2xl 20 / 3xl 24`).

| ID | Element | Design | Live |
| --- | --- | --- | --- |
| B1 | agent group card | 12 (`rounded-lg`) | 16 (`rounded-xl`) |
| B2 | model row | 8 (`rounded-md`) | 12 (`rounded-lg`) — `AgentCard.tsx` |
| B3 | agent head action button | 8 | 12 |
| B4 | source-detail more button | 33×30 r7 | 32×32 r8 |
| B5 | route-selector confirm | surface token `--model-hub-dialog-action-radius: 7px` | 12 — the rule sets height and font-size but not radius, so the raw `Button` `rounded-lg` survives |

## C — Line-height inflation (systemic; single largest cause)

The root cascades a 1.5-ratio leading, so every element the frame draws with a
tight line box renders 1–4px taller. `modelHubSurface.css` already has the
remedy pattern (`--model-hub-route-hint-line: 17px`,
`--model-hub-agent-head-action-leading: 17px`); it simply has not been applied
to the rest.

| Element | Design box | Live box |
| --- | --- | --- |
| 10.5px pills (upstream count, gateway port, model count, accent pill, mode chip) | 23 | 24 |
| agent name `h2` | 17 | 21 |
| panel `h2` (网关 / 来源) | 23 | 24 |
| upstream card name | 18 | 19 |
| upstream card detail | 14 | 16 |
| table head label | 15 | 16 |
| table row model id | 16 | 18 |
| entry pill | 22 | 23 |
| footnote | 16 | 19 (line-height 18.69) |

**This is the user's 「上上下下，样式比较乱」.** The frame-06 columns, x-positions
and gap match exactly and every cell *is* vertically centered — but each column
carries a different amount of baked-in leading (18 vs 23 vs 26 against the
frame's 16 / 22 / 32), so the ink inside the boxes does not share a rhythm even
though the boxes do.

## D — Fill and colour

| ID | Element | Design | Live |
| --- | --- | --- | --- |
| D1 | 「N 个型号」 pill | `rgba(255,255,255,.04)` | opaque `#0E0E18` |
| D2 | panel footer secondary button | `rgba(255,255,255,.04)` | opaque `#080812` |
| D3 | source-bar 添加模型 | solid `#5BFFA0` with `#080812` ink | mint-ink ghost on `#11111C` |
| D4 | route selector | `bg-panel` (base popover) | `bg-background` — see A3b |

## E — Metrics

| ID | Element | Design | Live |
| --- | --- | --- | --- |
| E1 | gateway content gap (between agent groups) | 10 | 16 |
| E2 | upstream content gap (label↔card, card↔card) | 10 | 8 |
| E3 | agent head action height | 36 | 35 |
| E4 | tab height | 41 (padding 10px 14px + 2px indicator) | 39 |
| E5 | page-title info icon | 13×13 glyph | 26×26 wrapper (the 来源 panel icon is correctly 13×13) |
| E6 | panel footer buttons | 88×30 borderless / 113×32 1px, 11.5px/600 | 90×31 / 115×31, 11.5px/500 |
| E7 | source-bar action label | 12px/600 | 13px/500 |
| E8 | table row height / head padding / add-row padding | 55 / `10px 18px` / `12px 18px` | 54 / `0 18px` / `0 18px` |
| E9 | source-bar identity line gap | 7 | 8 |
| E10 | 来源 panel content region | 430px holding 406px of cards (~24px slack) | 306px holding 194px of cards (~100px void above the footer) |

## F — Alignment

| ID | Finding |
| --- | --- |
| F1 | `.model-hub-model-current` is `min-w-0 flex-1 truncate` with no `text-right`. The frame right-aligns it against the chevron (x=1233, w=111); live leaves it at x=1077, w=268, so short values such as `—` float mid-row. |

## G — Data-state differences and superseded behavior

Verified against the spec and the code; the regression fixture has zero supply
relations and no runnable hops.

- Supply legend absent — `SupplyGraph.tsx:88-89` returns `null` at zero relations.
- Rail wires absent — same cause.
- The captured build showed all 18 model rows because the former G-25/D-7 rule expanded
  every non-runnable row. The 2026-08-23 owner decision supersedes that behavior with a
  strict six-row prefix plus the counted disclosure.
- 「没有可用来源」 line present; `—` in the current-source slot.
- Neutral (not cyan/mint) tiles; 20 table rows against the frame's 12.
- 「未设置档位 + 添加档位」 in the tier column **is** the designed empty state —
  frame 06 draws it on `glm-4.6v` and `ernie-5.0`. Not a defect. The frame's
  32px tier slot vs live's 26px comes from filled rows carrying 24–25px chips.

## H — Spec-vs-frame conflicts needing a decision

Not silent defects: the frame owns numbers, the spec owns element presence, and
these two disagree about presence.

| ID | Conflict | Options |
| --- | --- | --- |
| H1 | `.model-hub-model-mode-chip` (28×24, currently rendering `—`). Frame 01's model row does not draw it; spec §1.1 registers "a chain chip" in `Exx0a`'s inventory. | Drop the chip and correct §1.1, or draw it into the frame. |

## I — Checked and cleared

- **A4 is closed.** The dead 高级 placeholder no longer ships. Switch history is
  registered as the Logs-tab surface instead of extending frame 01 below its designed
  boundary.

- ~~**Route-selector scrolling already works.**~~ **Wrong — withdrawn.** The
  cited evidence was real (`modelHubSurface.css:724-733` bounds the panel and
  puts `overflow-y: auto` on the list; `scrollHeight 1480` vs
  `clientHeight 241`) but it does not support the conclusion. All of it
  describes the *box*; 「无法滚动」 is about the *event*, and no wheel was ever
  dispatched before clearing the item. A box can be perfectly overflowing and
  still refuse to scroll if an ancestor cancels the wheel — which is exactly
  what was happening. Filed as A3f. The general lesson: an event-outcome claim
  is only closed by dispatching the event.
- **「路由链没读到」 on first open did not reproduce.** Seen once, then clean on
  three subsequent opens; `GET /api/models/agents/<backend>/chain` returns 200
  with `chain: []`, which the dialog correctly renders as the empty-but-valid
  Route. Not filed.
- Frame-06 column geometry, and the whole "already matches" list above.

## Convergence order

1. **C** — one pass over `modelHubSurface.css` adding explicit line-height
   tokens. Largest visible win, lowest risk, and it is what the reported table
   complaint actually is.
2. **A1** — restore the tabs strip above the source detail.
3. **A3** — rebuild the add-hop surface on `Combobox`/`Command`.
4. **B**, **D**, **E**, **F** — mechanical token corrections.
5. **A2** — needs the behaviour decision first (frame 06's own dialog says
   「只有手动添加的型号能移除」, so a discovered row's `···` needs a defined menu).
6. **H1**, **H2** — decide, then either correct the spec or change the code.

## Resolution

This pass closed **A3** (including A3f/A3g) and all of **C**, **B**, **D**,
**E**, **F**. Still open: A1, A2, A4, H1, H2.

Two entries were implemented as something other than what the table's Design
column literally says. Both are reinterpretations of the measurement, not
descoping:

- **E6 — footer buttons.** The frame's `88×30` borderless and `113×32` 1px are
  not two different heights: they are one 30px band with the outlined button's
  stroke drawn *outside* it, which is what makes its border-box 2px taller.
  Implemented as exactly that (`--model-hub-footer-action-height: 30px` plus a
  `--outlined` modifier at `+2px`), so the relationship survives a token
  change. The **widths are deliberately left content-driven**: 88 and 113 are
  what the frame's Chinese labels measure, and pinning them would clip every
  other locale.
- **E8 — table head / add-row padding.** The frame's `10px 18px` and
  `12px 18px` describe a 35px head and a 41px add row. Under Tailwind's
  `box-sizing: border-box` preflight, the existing `height` + `align-items:
  center` already produce those exact boxes, so writing the padding would be a
  second spelling of a satisfied constraint. Only the row height was actually
  wrong (54 → 55); `--model-hub-source-table-draft-height` was already 55.

Two ownership consolidations came out of the C pass rather than being listed as
findings:

- `.model-hub-pill` now owns the 10.5px status pill's box (six call sites:
  upstream count, gateway port, model-count badge, source kind, model mode,
  direct kind). Colour stays at the call site. A seventh pill written as a
  utility bundle fails `modelHubStylePolicy.test.ts` instead of costing a
  review round.
- The add-hop picker is now composed from the shared anchored-selection
  primitives — `Popover` plus `Command` / `CommandInput` / `CommandList` /
  `CommandItem` and `Button` — the same set `Combobox` is built from, instead
  of hand-rolled markup. `Combobox` itself cannot be the surface: this picker
  is a two-column source/model grid and commits in two steps (highlight, then
  「添加」), where a combobox commits on select. So what is shared is the
  primitive layer, and A3f is fixed at that layer: `combobox.tsx` was given the
  same `modal` treatment, so every dialog-hosted picker in the product is
  covered by the one diagnosis rather than only this one.

### A3d — where the picker opens

A3d asked for the panel to stay inside its dialog. Two ways of expressing that
were built and measured on the empty-chain fixture (`claude-fable-5`, 1440×801),
and both were worse than the defect:

| Variant | Panel | List | Verdict |
| --- | --- | --- | --- |
| `collisionBoundary` = the dialog | 131px | **18px** over 1272px of content | One visible row. Ships the original complaint in a new form. |
| Viewport boundary, collisions on | 300px | 187px | Flips to `side: "top"` at y=148 — 150px above the dialog's own top, covering the page title and tab strip. |

The dialog is sized by its chain, so an empty chain — exactly when this picker
matters — makes it short, and the same boundary feeds
`--radix-popover-content-available-height`. The flip happens because the room
below the trigger lands a few pixels under the 300px preference.

Resolved by making placement deterministic and the height adaptive instead:
`side="bottom" avoidCollisions={false}` with the existing
`max-height: min(300px, available-height)` cap. Measured: panel 287px attached
6px under its trigger, list 174px over 1272px of content, 45 rows, bottom edge
785px inside an 801px viewport, `wheel` not prevented. A long chain that pushes
the trigger down yields a shorter panel, still scrolling, and the dialog body
scrolls so the trigger can be brought back up. Panel overhang past the dialog's
lower edge is accepted — ordinary floating-layer behaviour, unlike a one-row
list or a panel over the page title.
