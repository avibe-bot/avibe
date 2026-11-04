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
  window manager, Inbox and its Bot roster, project tree, Dock) from fixtures, and runs the real `ApiProvider`
  and `BotsProvider`.
  `fetch` and `EventSource` are answered in the page from the frame's API fixtures (by path and query, then by
  path; a `Response` fixture, such as a file's raw text, is served as is), a `WebSocket` delivers its `sockets`
  fixture's messages, and an image at an API path or an iframe the page opens is drawn from the asset URL its
  fixture names, else left blank, so no frame reaches a server; paths a frame touched without a fixture are reported. Each frame gets
  fresh in-memory Web Storage (seeded by its `storage` fixture) and a fixed clock, so a viewer's remembered
  preferences and the date never change a frame. No product code changes for this.
- The canvas (`ui/design.html`, not part of the production build) shows every frame, each in its own iframe so
  themes, portals, and routing never leak between frames. `?frame=<id>&theme=light|dark&lang=zh|en` renders one
  frame at its size and loads only that frame's file, so a frame's load time does not grow with the number of
  frames. Every product stylesheet is loaded in every frame, as the app build makes them global.
- `npm run design:shot` renders frames with Playwright (2x PNG, animations off, portals included, optional
  clicks and a hover, for states only an interaction opens) once fonts are loaded and the network is quiet (after the frame's `wait`, for a read that starts on a
  timer, and again after each click, for a transition it starts on a timer), and fails on any console error.
  Captures are run explicitly for design work and review, not by routine PR or master CI.
  `typecheck:design` remains in the static CI checks, so incompatible component props still fail the PR.
  Each frame is shot in a fresh page,
  several at once (`--jobs`), since most of a frame's time is waiting; a frame that never gets ready is
  reported with the step it stalled in, and the run goes on.
- `npm run design:export` copies the canvas, the screenshots, and `frames.json` into the current Session's Show
  Page and writes `<DesignFrame>`, which embeds frames live (crisp, interactive) without any Show Page change.
  Each shot keeps the language it was exported in; a frame that needs a click or hover is embedded as its
  screenshot, since the live frame opens before the interaction. While the Show Page's annotation is on, `<DesignFrame>` draws
  each frame as its screenshot through the overlay's public window API, so annotations reach it and name the
  frame.

## CI boundary

Design captures are manual by owner decision (2026-11-04). The capture script
checks runtime errors and readiness but does not compare against approved
images; routine CI did not publish its PNGs for review. Rendering the whole
catalog on every change is therefore not a required merge gate.

This deliberately removes whole-catalog runtime smoke coverage from routine CI;
it does not claim that type checking replaces that coverage. Design type checks,
existing unit tests and assertion-based browser suites remain required. Manual
`design:shot` (including optional sharding) and `design:export` remain available
for targeted design verification and Show Pages. No replacement nightly job or
optional GitHub workflow is introduced.

## Known by design

- A frame embedded in a Show Page is an iframe: the overlay annotates its screenshot, not its elements, and the
  Show proxy's annotation script logs a harmless error inside it (no import map in a non-entry document). A child
  frame mode in `vibe-show-runtime` would lift both; not needed yet.
- A surface whose markup and data hooks live in one component (`WorkbenchSidebar`) can be drawn whole, but a
  partial redesign of it can only be drawn beside it in `kit/`. Splitting such components into a view (props
  only) and a container makes them fully drawable and simpler to fixture.
- Kit copy is draft copy; it moves to `ui/src/i18n` when the component ships.

## Follow-ups

- `avibe-bot-backend` designs the avibe.bot site and console the same way, in its `design/`
  (avibe-bot/avibe-backend#300); its baseline frames follow. Its `design:export` writes into the same Show
  Page: each `frames.json` entry names its canvas folder (`canvas`, or `avibe-bot/canvas`), and
  `<DesignFrame>`, the same file in both repositories, loads each frame from its own canvas.
