# Design frames in the repository

Status: implemented (2026-11-02). Replaces Pencil (`avibe-docs/design.pen`) for new design work.

## Why

- **Fidelity.** A Pencil frame is a drawing of the product; it drifts as the product changes. Its 53 color
  variables mirror `ui/src/index.css` by hand. A frame that renders the shipped component cannot drift: on
  2026-11-01 #3111 renamed a sidebar label, and the frames picked it up while the hand-drawn mockups of the
  same day did not.
- **Collaboration.** `design.pen` is one 25 MB file (220 artboards, about 45,000 nodes) marked binary, so Git
  cannot merge it; concurrent edits need a three-way replay through Pencil (`avibe-docs/docs/design-merge-playbook.md`).
  Frames are small TSX files that merge like code, and Pencil is now a paid, per-seat tool.
- **Agents.** Agents write static JSX with the product's Tailwind tokens and check it by screenshot, without
  touching interaction logic.

## Design

- `ui/design/frames/<area>/<name>.design.tsx` default-exports the frames of one surface, one per state, each
  with a title, a size, its themes, and a status (`proposed`, `approved`, `shipped`). A frame renders the
  shipped component with fixtures; what is not shipped yet is drawn with `ui/design/kit/` components that use
  only product tokens and assets, and switches to the shipped component when it ships.
- `DesignShell` serves the providers Workbench surfaces read (router, toast, authorization, unsaved changes,
  window manager, Inbox, project tree) from fixtures. The real `ApiProvider` runs, with `fetch` and
  `EventSource` answered in the page, so no frame reaches a server; paths a frame fetched without a fixture are
  reported. The clock is fixed, so relative times are stable. No product code changes for this.
- The canvas (`ui/design.html`, not part of the production build) shows every frame, each in its own iframe so
  themes, portals, and routing never leak between frames. `?frame=<id>&theme=light|dark&lang=zh|en` renders one
  frame at its size.
- `npm run design:shot` renders frames with Playwright (2x PNG, animations off, portals included) and fails on
  any console error; CI runs it with the browser checks, and `typecheck:design` with the static checks.
- `npm run design:export` copies the canvas, the screenshots, and `frames.json` into the current Session's Show
  Page and writes `<DesignFrame>`, which embeds frames live (crisp, interactive) without any Show Page change.
  While the Show Page's annotation is on, `<DesignFrame>` draws each frame as its screenshot through the
  overlay's public window API, so annotations reach it and name the frame.

## Known by design

- A frame embedded in a Show Page is an iframe: the overlay annotates its screenshot, not its elements, and the
  Show proxy's annotation script logs a harmless error inside it (no import map in a non-entry document). A child
  frame mode in `vibe-show-runtime` would lift both; not needed yet.
- A surface whose markup and data hooks live in one component (`WorkbenchSidebar`) can be drawn whole, but a
  partial redesign of it can only be drawn beside it in `kit/`. Splitting such components into a view (props
  only) and a container makes them fully drawable and simpler to fixture.
- Kit copy is draft copy; it moves to `ui/src/i18n` when the component ships.

## Follow-ups

- Export every `design.pen` artboard to PNG while Pencil is available, then keep the file read-only.
- Update `avibe-docs/AGENTS.md` and the workspace index, which still name `design.pen` the design source.
- Draw `avibe-bot-backend` surfaces (avibe.bot landing page and console) the same way in that repository.
