# Design frames

Avibe's UI designs live here. A frame is a static React render of one surface in one state, built from the product's own components, tokens, assets, and copy, so a design and the shipped UI cannot drift apart. Frames are reviewed and merged like code. The design system and its decisions: `docs/plans/design-frames.md`.

## Layout

- `frames/<area>/<name>.design.tsx`: one file per surface; it default-exports `frames([...])`, one frame per state. A frame id is `<area>/<name>/<state>`.
- `kit/`: components a design proposes that are not shipped yet. They use only product tokens (Tailwind classes from `src/index.css`), assets, and `src/components/ui` primitives.
- `shell/`: the providers frames render in (`DesignShell`), fixture builders (`fixtures.ts`), the fixed clock, frame-local Web Storage, and the network stub.

## Rules

- Draw shipped surfaces with the shipped component (`@/components/...`) and fixtures; never redraw them.
- Draw what does not exist yet in `kit/`. When it ships, the frame switches to the shipped component, the kit copy is deleted, and the status becomes `shipped`.
- Frames hold no logic: no handlers, no effects, no fetching. State comes from fixtures (`WorkbenchFixtures`): a provider that reads the API gets its response from `fixtures.api` or a typed fixture field, a remembered preference comes from `fixtures.storage`, and a view a page reads from its URL comes from the frame's `route` (path and query). Add a fixture field rather than mocking inside a frame.
- Tokens only: no raw colors, sizes, or shadows that `src/index.css` does not define. Add a token there if one is missing.
- Each frame states its size, its themes (default both), and its status: `proposed` (being designed), `approved` (signed off, ready to build), or `shipped` (renders shipped components only). A proposal becomes `approved` in the PR the owner approves.
- Kit copy is draft copy and may be written inline; it moves to `src/i18n` when the component ships.
- When a shipped component mixes data hooks and markup, draw it whole; to redesign part of it, prefer splitting it into a view (props only) and a container, then draw the view.
- Size a frame to include any popover, menu, or card it shows; set `hover` to capture a hover state, `click` (in order; name copy as `{t:<i18n key>}` so it works in both languages) to capture a state only a click opens, and `wait` when a read starts on a timer (a debounced search). Prefer a fixture or the page's URL state over `click` when the product offers one.
- Code that implements a design names its frame id (`ui/design: sidebar/bots/three`), not a description of a drawing.

## Loop

- `npm run design`: the canvas with every frame in both themes, hot-reloading.
- `npm run design:shot -- <id prefix>`: PNGs in `design/.shots/<id>.<theme>.png` (2x); read them to check your work. One JSON line per shot lists API paths the frame touched without a fixture (`unfixtured`) and console errors; a frame not ready within 15 s is reported with the step it stalled in (`load`, `render`, `fonts`, `network`, `wait`). Any error exits 1. CI runs it for every frame.
- `npm run typecheck:design`: frames type-check against the product's current props, so a product change that breaks a frame fails CI.
- `npm run design:build -- --out <dir>`: the canvas as static files.

## Presenting a design

Show designs in your own Session's Show Page, next to your explanation:

1. `npm run design:export -- <id prefix>` copies the live canvas, a screenshot of each frame, and `frames.json` into `public/design/` of this Session's Show Page, and writes `src/design/DesignFrame.tsx` there.
2. In a page: `import { DesignFrame } from "../design/DesignFrame"`, then `<DesignFrame id="sidebar/bots/three" />`. Each frame runs the real components in an iframe (crisp, scalable, hover and popovers work) and links to its screenshot; a frame with `hover` or `click` is shown as its screenshot, which holds that state.
3. While the Show Page's annotation is on, each frame is drawn as its screenshot so the overlay can reach it; an annotation names the frame (`设计帧 <id>`) and theme. Read it back to the frame file before you change anything.

The repository is the source: never edit designs in a Show Page.
