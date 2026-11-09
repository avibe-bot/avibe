# Elevation, edges, state, and glow: one system for the Web UI

Status: approved by the owner on 2026-11-08 (option C, no glass); shipped 2026-11-09 (#3407, #3414,
#3425, #3426, #3431, and the other fixes below).
Frames: `system/elevation/scale` and `system/elevation/edges` draw the system with the shipped tokens
and primitives; every surface it touched is drawn by its own shipped frame. Tokens (`--elevation-*`,
`--edge-border`, `--field*`, `--composer-shadow*`, `--dot-glow-*`, `--glow-soft`, `--unread-shadow`,
`--decor-light`, `--mark-light`, and the `wash` colours) are in `ui/src/index.css`.

## Background

The owner asked to sort out card shadows: some feel off, some are too heavy, some carry the wrong
meaning, and they are not consistent. A full visual overhaul was rejected before; this is a restrained,
consistent pass that keeps the chat bubbles and the overall identity.

`ui/src` uses about 45 distinct shadow values in TSX and about 10 more in component CSS:
Tailwind's `shadow-sm|md|lg|xl|2xl` (52 uses), 24 arbitrary `shadow-[…]` literals, the three card
washes (`shadow-mint-card*`, 22 uses), 33 glow tokens (`shadow-glow-*`, ~120 uses), and Model Hub's
own menu/dialog/drawer variables.

## Audit (by what the shadow is for)

| Role | Today | Problem |
| --- | --- | --- |
| Resting card, panel, input | Mostly none; but Home "Getting started", setup wizard, mobile onboarding, and platform config cards wear the mint card wash (22); IM channel cards a heavy `0 18px 40px -30px rgba(0,0,0,.8)` (5); Doctor and user list hand-written washes; every Input/Select/Textarea `shadow-sm` (`field.ts`); outline button `0 1px 2px .05` | Coloured light used as height; cards on one page differ; the "inset field" is drawn raised |
| Hover / press | No shadow change (good), except the sidebar logo glow growing on hover | — |
| Selected / active | ~22 sites signal state with a glow (`glow-xs/sm/md/lg`): sidebar nav and rail, Agents/Skills/MCP rows, agent graph node and trigger, Inbox and Logs filters, a switch that is on, shortcut recording, Apps launcher open, annotate toggle, expanded config cards; `ModeSelection` uses `shadow-md`. `SegmentedRadio` and other rows use fill + border only | A glow as state; two drawings for the same control |
| Unread / attention | Inbox unread card `glow-md-mint` (a page of unread is a page of glow); unread count badges `glow-xs-cyan` (4) | The signal fades as it multiplies; in Light it reads as a pale green haze |
| Popover, menu, hover card, tooltip | Seven values: `shadow-md` (Popover primitive), `shadow-lg` (ResponsiveMenu, tooltips), `shadow-xl` (account, language, version, backend menus), `rgba(0,0,0,.6)` ×3 (Bot menu, Bot hover card, Inbox popover), `.7` (context menu, quote toolbar), Model Hub variable. Seven fills (`bg-panel/card/popover/surface/surface-2/surface-3/background`); radii md/lg/2xl/10px | Light: .6–.7 black is a dirty smudge. Dark: Tailwind's .1 black is invisible. Context menu on `surface-3` is darker than the page in Dark and grey in Light |
| Toast | `shadow-lg` on a 10% tint | Text under it shows through |
| Dialog, sheet, drawer | Dialog primitive `shadow-xl`; bespoke dialogs `.6/.7/.75/.8/.85` black plus one violet glow; Model Hub variable; panels, image viewer, drawer `shadow-2xl`; phone bottom sheet `shadow-2xl` (casts downward, off screen); Apps drawer `.55` upward | Ten-plus values; heavy in Light; sheet shadow points the wrong way |
| Floating control | Composer upward `.5`; jump-to-latest `shadow-lg`; the phone tab bar's new-session button stacks a `.6` neon drop under the brand glow; Dock `.65`; windows `.7/.6`; switch thumb; segmented thumb | Same role, different values; a doubled glow |
| Status dot | `glow-dot-*` (19) | Dark neon colour in Light too |
| Brand and decoration | Page icon tiles, sidebar logo, eyebrow badges, Apps launcher, welcome/summary marks, onboarding halo, brand CTA, Model Hub wires | Dark neon over white reads as blur |

## The approved system

### Elevation: a shadow says only how high a surface sits

| Level | For | Light | Dark |
| --- | --- | --- | --- |
| 0 flat | cards, rows, panels, inputs | the edge below | the edge below |
| 1 raised | composer, segmented thumb, switch thumb, setup panel | `ring ink/.04, 0 1px 3px tint/.08` | `top highlight, ring white/.06, 0 1px 2px black/.45` |
| 2 floating | popover, menu, hover card, tooltip, toast, Dock, jump-to-latest, unfocused window | `ring ink/.07, 0 2px 6px -2px tint/.08, 0 12px 32px -8px tint/.16` | `inset 0 1px 0 white/.07, ring white/.1, 0 2px 6px -2px black/.5, 0 12px 32px -8px black/.6` |
| 3 overlay | dialog, search palette, drawer, image viewer, focused window | `ring ink/.07, 0 4px 12px -4px tint/.1, 0 28px 64px -16px tint/.24` | `inset 0 1px 0 white/.07, ring white/.1, 0 4px 12px -4px black/.5, 0 24px 64px -12px black/.7` |
| 3 up | a sheet rising from the bottom edge | `0 -1px 0 ink/.07, 0 -12px 32px -12px tint/.16` | `inset 0 1px 0 white/.07, 0 -2px 8px -4px black/.4, 0 -16px 40px -12px black/.6` |

`ink` is `rgb(8 8 18)`, the colour `--border` mixes from; `tint` is the brand's deep teal
`rgb(10 52 44)`, so Light shadows read as the brand rather than grey; "ring" is a `0 0 0 1px` shadow.
Floating and overlay surfaces are opaque `bg-popover` with no border: their ring is in the level's
shadow. Dark lifts `--popover` one step (`#11111c` → `#161622`) so the surface, not a black smear,
separates a menu from the card under it. Tokens are themed runtime variables behind `@theme inline`
entries, the same pattern as `--card-wash`.

### Edges: no opaque grey border on a resting surface

| Surface | Edge |
| --- | --- |
| Card, panel, row | Transparent border. Light: `ring ink/.04, 0 1px 2px tint/.05, 0 4px 14px -6px tint/.08`. Dark: `inset 0 1px 0 white/.05, ring white/.06`. A card drawn on the page colour takes `--surface`. |
| Input, select, textarea, search | Filled: `ink/.045` Light, `white/.05` Dark; no border, no shadow; the fill deepens on hover; focus is the ring. |
| Outline/secondary button, small bordered action, chip | The same fill, no outline. |
| Kept | Selected, focus, error, the dashed "add", table and list dividers. |

Borders stay in the DOM as transparent, so forced-colours mode still paints them, and
`prefers-contrast: more` brings them back (`foreground` at 55% on inputs, 30% on cards). The Light
focus ring is the brand mint, opaque (`#10b981`), because it is now the main focus cue on a field.

### State: never a shadow, never a glow

Selected rows use fill + border (navigation uses `--nav-selected-bg` / `--nav-selected-border`);
unread keeps its mint wash and count, its edge goes to mint 45%, and the Inbox popover's unread rows
lose their left stripe; counts are flat; focus is one ring.

### Light: a glow means something emits light

Kept as approved before: running/attention/error dots, a live microphone, the brand CTA, the
selected-Bot avatar, the onboarding halo, Model Hub wires; dots and the microphone take the theme's
own accent (dots: 6px at 60% in Light). Brand decoration (icon tiles, logo, eyebrow) keeps its glow
in Dark; in Light it becomes a same-hue soft shadow (`0 6px 16px -8px currentColor/.45`), as does an
unread card (`0 10px 26px -12px mint/.45`). Removed: every state glow, the card washes, the
new-session button's extra drop shadow.

### Freshness

The composer wears a gradient hairline (mint → cyan → violet) and a soft edge; on focus a mint ring
appears outside it; while an Agent works, a light sweeps its edge (2.8 s a turn, still under
`prefers-reduced-motion`). No glass anywhere.

## Other fixes

| # | Fix | Evidence | Size |
| --- | --- | --- | --- |
| 1 | Hover is invisible in Light on white surfaces | `hover:bg-surface-2` ×31 (ghost button variant, Agents rows, vault rows); Light `surface`, `surface-2`, `card`, `popover` are all `#fff`; the outline button hovers lighter than it rests | small |
| 2 | Queued-row hover never shows | `hover:bg-surface-1` in ChatPage: no such token | 1 line |
| 3 | One hover and one press wash | eight alphas in use (`foreground/0.02…0.10`); keep `/[0.04]` hover, `/[0.06]` press | medium |
| 4 | One focus ring | `ring-ring`, `ring-cyan/60`, `ring-mint/60`, `/40`, `/30`; `focus:` (draws on click) mixed with `focus-visible:` | medium |
| 5 | One segmented control | Inbox filter (glow) and Diagnostics tabs (raised thumb) hand-roll what `SegmentedRadio` already draws | small |
| 6 | One radius for floating surfaces | Popover md, menus lg, bespoke popovers 2xl, Model Hub 10px → `rounded-xl`; dialogs already mostly `rounded-xl` | small |
| 7 | Use or delete dead tokens (lands in part 2) | `--shadow-glow-nav-mint`, `--nav-selected-*`, `--nav-hover-bg` | small |
| 8 | Legacy colour aliases | `bg-panel`, `text-text`, `bg-text`, `text-bg`, `bg-bg` ×33, including the Popover and Command primitives | small |
| 9 | One active-tab indicator | Editor tabs: cyan top line; Terminal tabs: mint bottom line | small |
| 10 | Snap arbitrary radii outside approved designs | `rounded-[10px]` ×39, `[9px]` ×12, `[4px]`/`[5px]` ×14, `[14px]` ×6 | medium |
| 11 | Two border strengths per accent | mint borders at 20/25/30/35/40/45/50/60% | wide, low priority |
| 12 | A selected Bot's halo is cut off on the right | in the sidebar strip, when the last Bot is selected, its halo ends in a straight edge beside Add (the strip's scroll box clipped it; fixed in #3433) | small |

Chosen and shipped: 1 and 3 (#3440), 2 (#3432), 4 (#3439), 5 (#3437), 7 (with part 2, #3426),
12 (#3433).

## Implementation

Each part landed from the latest `master` with light/dark, desktop/phone screenshots, deleted the
approved review frames its shipped frames then matched, and took its rules out of the review kit
(`ui/design/kit/elevation/`, removed at the end).

1. Primitives (shipped): Dark `--popover`; Light `--ring`; Card/Panel, `field.ts` inputs, outline/secondary buttons, Popover,
   ResponsiveMenu, ContextMenu, Dialog, Toast, switch, segmented control, the composer.
2. Call sites and state (shipped): hand-written `rounded + border-border + bg-surface` cards and bespoke
   shadow literals onto the primitives or tokens; state glows to fill/border; card washes to the
   edge; navigation onto `--nav-selected-*` (fix 7). The dead tokens go with it:
   `--shadow-mint-card*`, `--shadow-glow-nav-mint`, `--nav-hover-bg` (the nav hovers with the
   shared `foreground/[0.04]` wash), and the desktop and connection overlay shadows.
   `--card-wash` stays for the onboarding provider cards. Deliberately unchanged: the Agents
   "Run" button keeps its mint tint; chat bubbles; the new-Agent dialog's `border-2` choice tiles;
   the Model Hub drawer's directional shadow; the onboarding stylesheet.
3. Glow theming (shipped): every glow role but `cta` mixes its hue from the theme's accent (Dark
   unchanged), and dots are 6px at 60% in Light. Brand decoration (icon tiles, eyebrow badges,
   success seals) draws `shadow-decor-*`: the glow in Dark, `--glow-soft` in Light. A brand mark (the
   logo image, the Apps launcher) draws `shadow-mark-*`: the glow in Dark, nothing in Light. An unread
   card draws `shadow-unread`; a listening voice card draws `shadow-elevation-2-live`; the phone tab
   bar's new-session button keeps only the brand button's glow. Where no approved frame drew a case,
   the text above decides: the mint eyebrow, the success seals, and the Doctor tile take the soft
   shadow like every other decoration, and the press ring (a live microphone) takes the theme's mint.
4. One small PR per chosen fix (1 and 3 share their call sites and landed together): the hover and
   press wash is `bg-wash` / `bg-wash-press` (`bg-surface-wash*` on an element resting on
   `bg-surface`); hovers that step up from their own resting tint keep their step.
