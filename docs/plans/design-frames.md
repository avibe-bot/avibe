# Design frames

Status: adopted (2026-11-02). Avibe's UI designs live in `ui/design/` as frames; working rules are in
`ui/design/AGENTS.md`.

## Goals

- **Fidelity.** A frame renders the shipped components, tokens, and copy, so a design of a shipped surface
  cannot drift from the product, and a proposal built from the same tokens and primitives is implemented by
  assembly rather than translation.
- **Collaboration.** Frames are small TSX files in the repository. Everyone designs locally with their own
  Agent, reviews through PRs, and merges like code; there is one design source and no per-seat tool.
- **Focus.** Frames are static. A designer, person or Agent, works on layout and style from fixtures, checks
  the result by screenshot, and never writes interaction logic.

## Design

- `ui/design/frames/<area>/<name>.design.tsx` default-exports the frames of one surface, one per state, each
  with a title, a size, its themes, and a status (`proposed`, `approved`, `shipped`). A frame renders the
  shipped component with fixtures; what is not shipped yet is drawn with `ui/design/kit/` components that use
  only product tokens and assets, and switches to the shipped component when it ships.
- `DesignShell` serves the providers Workbench surfaces read (router, toast, authorization, unsaved changes,
  window manager, Inbox, project tree) from fixtures, and runs the real `ApiProvider` and `BotsProvider`.
  `fetch` and `EventSource` are answered in the page from the frame's API fixtures (by path and query, then by
  path), so no frame reaches a server; paths a frame fetched without a fixture are reported. Each frame gets
  fresh in-memory Web Storage (seeded by its `storage` fixture) and a fixed clock, so a viewer's remembered
  preferences and the date never change a frame. No product code changes for this.
- The canvas (`ui/design.html`, not part of the production build) shows every frame, each in its own iframe so
  themes, portals, and routing never leak between frames. `?frame=<id>&theme=light|dark&lang=zh|en` renders one
  frame at its size.
- `npm run design:shot` renders frames with Playwright (2x PNG, animations off, portals included, an optional
  hover) once fonts are loaded and the network is quiet, and fails on any console error. CI runs it with the
  browser checks, and `typecheck:design` with the static checks, so a product change that breaks a frame fails
  its PR.
- `npm run design:export` copies the canvas, the screenshots, and `frames.json` into the current Session's Show
  Page and writes `<DesignFrame>`, which embeds frames live (crisp, interactive) without any Show Page change.
  Each shot keeps the language it was exported in. While the Show Page's annotation is on, `<DesignFrame>` draws
  each frame as its screenshot through the overlay's public window API, so annotations reach it and name the
  frame.

## Known by design

- A frame embedded in a Show Page is an iframe: the overlay annotates its screenshot, not its elements, and the
  Show proxy's annotation script logs a harmless error inside it (no import map in a non-entry document). A child
  frame mode in `vibe-show-runtime` would lift both; not needed yet.
- A surface whose markup and data hooks live in one component (`WorkbenchSidebar`) can be drawn whole, but a
  partial redesign of it can only be drawn beside it in `kit/`. Splitting such components into a view (props
  only) and a container makes them fully drawable and simpler to fixture.
- Kit copy is draft copy; it moves to `ui/src/i18n` when the component ships.

## Follow-ups

- Draw `avibe-bot-backend` surfaces (the avibe.bot site and console) the same way in that repository.
