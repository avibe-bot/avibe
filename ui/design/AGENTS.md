# Design frames

Designs are static React frames rendered with the product's own components, tokens, and copy, so a design and the shipped UI cannot drift apart. Background and decisions: `docs/plans/design-frames.md`.

## Layout

- `frames/<area>/<name>.design.tsx`: one file per surface; its default export is `frames([...])`, one frame per state. A frame id is `<area>/<name>/<state>`.
- `kit/`: proposed components that are not shipped yet. Use only product tokens (Tailwind classes from `src/index.css`), assets, and `src/components/ui` primitives.
- `shell/`: the providers frames render in (`DesignShell`), fixture builders (`fixtures.ts`), and the network stub.

## Rules

- Draw shipped surfaces with the shipped component (`@/components/...`) and fixtures; never redraw them.
- Draw what does not exist yet in `kit/`. When it ships, switch the frame to the shipped component and set `status: 'shipped'`.
- Frames hold no logic: no handlers, no effects, no fetching. State comes from fixtures (`WorkbenchFixtures`); add a fixture field rather than mocking inside a frame.
- Tokens only: no raw colors, sizes, or shadows that `src/index.css` does not define. Add a token there if one is missing.
- Each frame states its size, its themes (default both), and its status: `proposed`, `approved`, or `shipped`.
- Kit copy is draft copy and may be written inline; it moves to `src/i18n` when the component ships.
- When a shipped component mixes data hooks and markup, draw it whole; to redesign part of it, prefer splitting it into a view (props only) and a container, then draw the view.

## Loop

- `npm run design`: the canvas with every frame in both themes, hot-reloading.
- `npm run design:shot -- <id prefix>`: PNGs in `design/.shots/<id>.<theme>.png` (2x); read them to check your work. One JSON line per shot lists API paths the frame touched without a fixture (`unfixtured`) and console errors; any error exits 1.
- `npm run design:build -- --out <dir>`: the canvas as static files.

## Presenting a design

Show designs in your own Session's Show Page, next to your explanation:

1. `npm run design:export -- <id prefix>` copies the live canvas, a screenshot of each frame, and `frames.json` into `public/design/` of this Session's Show Page, and writes `src/design/DesignFrame.tsx` there.
2. In a page: `import { DesignFrame } from "../design/DesignFrame"`, then `<DesignFrame id="sidebar/bots/three" />`. Each frame runs the real components in an iframe (crisp, scalable, hover and popovers work) and links to its screenshot.
3. Size a frame to include any popover or menu it opens; the iframe clips at the frame's edge.
4. While the Show Page's annotation is on, each frame is drawn as its screenshot so the overlay can reach it; an annotation names the frame (`设计帧 <id>`) and theme. Read it back to the frame file before you change anything.

The repository is the source: never edit designs in a Show Page.
