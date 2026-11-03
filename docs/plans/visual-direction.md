# Visual direction

Status: proposed (2026-11-03). The owner asked for a more modern look, restrained rather than
dramatic, with mint and the Vibey mascot kept. Three directions are drawn as frames in
`ui/design/frames/directions/` (status `proposed`), beside today's surfaces as they ship. This
document records the diagnosis, the rules every direction shares, and the rollout once one is
chosen.

## Diagnosis

Each finding is visible in `directions/today/*`.

- Light pages sit on a pastel wash: mint, cyan, and violet radial gradients (`--gradient-console`).
- Decoration glows: every unread Inbox card has a mint frame and halo; the sidebar logo, the Apps
  launcher, page-header and home icon tiles glow too, so state (selected, running) does not stand out.
- Selection is drawn five ways: a solid ring with a gap around a Bot avatar, a mint fill with frame
  and glow on Agents rows, a violet underline on the Agents tabs, a mint block in the phone tab bar,
  a cyan block on the sidebar's Inbox.
- Unread is cyan on the Bots strip but mint in the sidebar's Session counts, the phone Inbox tab,
  and the Inbox card's open button; cyan also colours project labels, open folders, and the launcher.
- Frames nest: the Inbox card holds a bordered project pill, two bordered buttons, and an
  「AGENT」 eyebrow; the home card holds a glowing tile and three pills; each phone project row
  has a bordered count.
- Section labels use a Latin eyebrow recipe (10px mono, uppercase, 0.18em tracking) that spreads
  CJK characters apart; the Inbox repeats its title as an eyebrow above it.
- Type has no scale: 10px 「伙伴」 above a 20px bold 「项目」 on the phone, 24px bold titles over
  12px subtitles, and over a thousand hand-set sizes, many at 9 to 11px.
- The composer casts a long dark shadow upward that reads as grey haze in Light.
- Both chat bubbles are framed: mint fill and frame for the agent, grey fill and dark frame for the user.
- Light's muted text (#5C6079) is violet-tinted; in Light, cyan (#0891B2) and mint (#10B981) are
  close enough in hue to blur unread and selected.
- The running dot pings outward, which reads as an alarm.

## Shared rules

Every direction applies these; only their values differ.

- A selected object (a Bot, an Agent) lights its own edge: a mint hairline and a soft mint glow,
  with no gap and no second ring. Selected navigation (tabs, nav rows, the tab bar) is a quiet fill
  or ink text and never glows.
- Glow is for state only (selected, running); decoration does not glow.
- Running breathes (a 2.4 s fade) instead of pinging. Unread is a cyan count, and cyan means
  nothing else. Focus keeps today's mint ring.
- Section labels are sentence-case sans; a page has one title and one muted line under it.

## Directions

- **A 细线 (hairline)**, after Linear and Vercel: white and near-black, one hairline per boundary,
  radius 8/10/12, 20px titles, ink primary action, neutral near-black dark theme. Densest and most
  tool-like; mint appears least.
- **B 层次 (layers)**, after the ChatGPT and Claude apps and iOS grouped lists: surfaces separated by
  tone, borderless cards, the agent's reply as plain text, 26px titles and 14px body, today's radius
  and mint primary, a slightly violet dark theme. Least dense; the most component work.
- **C 整理 (cleaned)**: today's palette, borders, and radius with the noise removed. Least risk and
  least change.

Recommendation: B, after the cleanup step below, which every direction needs.

## How the frames draw a direction

`ui/design/kit/directions/directions.css` re-skins the shipped components inside a
`[data-direction]` root (`Direction.tsx`) and on its document's body, which portals render into. It
holds the proposed token values, remaps of utilities whose token `@theme inline` bakes in (radius,
small type), and component rules written against the shipped markup. These values are the proposal
itself, so they live in the kit rather than in `src/index.css` until one direction is approved.

## Rollout

1. **Cleanup** (no direction needed): flat light page backgrounds, a neutral card wash, and a
   selected-edge glow token; the shared selected class for Bots takes that token; decorative glow
   call sites removed (header and home tiles, nav rows, launcher, logo, Inbox cards, badges);
   section labels without tracking or uppercase; the Inbox eyebrow and 「AGENT」 label removed;
   unread counts cyan; Agents tabs in ink; the running dot breathes. Re-shoot `directions/today/*`:
   it should match `directions/c-cleaned/*`.
2. **Direction tokens**: surfaces, borders, muted text, radius, and type in `src/index.css` (both
   Light blocks and Dark), with the ratios `validate:theme` pins updated; a full `design:shot`,
   reading the before and after of the main surfaces.
3. **Direction components**, one PR each: cards (Inbox, project rows, Agents rows), chat bubbles
   (`chatBubble.ts`), the composer shadow, the primary button (`button-variants.ts`), the page header
   (`WorkbenchPageHeader`), the phone tab bar's selected tab. Each PR is done when
   `directions/today/*` matches the chosen direction's frames.
4. **Close-out**: delete `kit/directions/` and the direction frames; fold `directions/today/*` into
   each surface's own shipped frames.
