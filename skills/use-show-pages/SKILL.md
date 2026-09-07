---
name: use-show-pages
slug: use-show-pages
description: Build, inspect, update, restore, or share Avibe Show Pages for visual explanations, diagrams, reports, or interactive prototypes. Covers the page workspace and its Git history.
version: 0.3.0
---

# Use Show Pages

When a visual page would help the user understand a problem, plan, process, result, or complex information more clearly, use Show Pages. They are useful for diagrams, flowcharts, mind maps, timelines, architecture maps, comparison views, dashboards, visual reports, interactive explanations, and small prototypes.

Each Agent Session has one Show Page. Get this session's page directory:

`vibe show path`

Check status:

`vibe show status`

Change visibility:

`vibe show update --visibility public`
`vibe show update --visibility private`
`vibe show update --visibility offline`

For more usage details, run `vibe show --help` or a subcommand help such as `vibe show update --help`.

### Agent-readable representation
- Every Show Page URL is agent-readable without page-specific code: request the same private or public URL with `Accept: text/markdown` to receive its rendered Markdown representation.
- Author semantic HTML so that representation stays dense and useful: use headings for sections, lists for sequences or groups, and `<table>` for genuinely tabular data.
- Add `data-agent-hidden` to visual-only or sensitive-to-representation elements that should be omitted from Markdown. Add `agent-note="..."` when an element needs short agent-only context; the note text is preserved in the representation.

### Show Page annotations & reverse marks
- Users can annotate your Show Page; each annotation arrives as a chat message tagged [show-annotation] with its event id. Some messages end with a ready-to-run reply command — whether to reply on the page or respond by editing the page content is your call, per scenario.
- After reworking a page area you may leave a short callout: `vibe show mark <selector-or-anchor> --message '...'` (same target replaces), or an `agent-note="..."` attribute on elements you author. Marks retire once read — leave at most 1-2 per turn.
- Inspect/withdraw: `vibe show marks` / `vibe show unmark <id|target> ...`; toggle the user's annotation mode: `vibe show annotate --on|--off [--mode smart|screenshot]`.

For live runtime, visibility, and URL availability, treat
`vibe show status` and the relevant command output as authoritative.

### Show Page workspace history

These rules apply only to this Session's Show Page workspace and its Avibe
history. Avibe manages page checkpoints automatically; do not create versions
manually for page edits.

Before operating on history, run `vibe show status --json`. Use its `path`
as `<show-workspace>` and inspect `history.mode`, `history.checkpointing_active`,
and `history.git_dir`. Status is read-only; it does not initialize a repository.
If checkpointing is inactive, do not assume new edits have automatic checkpoints
or start managing Avibe history yourself. Existing history can still be inspected
and restored when requested.

When `history.mode` is `managed`:
- Before using `git -C`, check that `git -C <show-workspace> rev-parse --absolute-git-dir`
  resolves to `history.git_dir`. If it does not, do not use an enclosing repository
  or initialize one in its place.
- Read freely with `git -C <show-workspace> status / log / diff / show`.
- Restore files with `git -C <show-workspace> restore --source=<ref> -- <path>`.
  When checkpointing is active, Avibe records the restored files in a new forward
  checkpoint. Do not move HEAD, switch branches, rewrite history, run gc, or
  commit checkpoints yourself, regardless of checkpoint availability.
- Adding remotes, pushing, or publishing the page requires the user's
  corresponding authorization.

When `history.mode` is `self-managed`, the workspace has the user's own Git
repository, separate from Avibe's shadow history:
- `git -C <show-workspace>` addresses the user's repository, not Avibe history.
  Do not commit or modify that repository to manage Avibe page checkpoints.
- Separately entrusted repository work follows the user's mandate; page
  checkpoint rules do not prohibit it.
- Only when the user asks to recover from Avibe history, use the returned
  `history.git_dir` as `<shadow-git-dir>` with explicit paths:
  `git --git-dir=<shadow-git-dir> --work-tree=<show-workspace> log` to inspect,
  and `git --git-dir=<shadow-git-dir> --work-tree=<show-workspace> restore --source=<ref> -- <path>`
  to restore files. Do not commit to or otherwise mutate the shadow history.

Guidance:
- New Show Page workspaces are managed React/Vite apps that start as a clean "being generated" placeholder page (what the user sees while you build) plus a minimal file-based router (`src/router.tsx`) and one example page. When that router is present, add a route by creating a file under `src/pages/` — a folder becomes a nested path segment and a `[param]` file a dynamic segment — and customize the layout in `src/App.tsx`, styles in `src/styles.css`, and optional `api/*.ts` handlers. The starter is only a starting point, not a required structure: replace the placeholder with the real page, add or remove pages, and organize them however fits the app (flat, sections, or nested). Built-in UI is available to import, e.g. `@/components/ui/card`, `@/components/ui/button`, `@/components/ui/badge`.
- An older Show Page with no `src/router.tsx` is a single-page app that renders `src/App.tsx` directly. There, edit `src/App.tsx` (or adopt the router scaffold: add `src/router.tsx` + `src/pages/` and render it from `App.tsx`) — do not just drop files under `src/pages/`, since nothing would route them.
- Treat `index.html` and `src/main.tsx` as the runtime-owned app shell — you never edit them to add a page, and should not replace them unless you are repairing the shell.
- Hot reload is available while `/show/<session-id>/` is open. Users will see page changes live. Prefer component-level changes that preserve React state.
- Built-in UI uses the standard shadcn aliases: import components from paths such as `@/components/ui/button`, `@/components/ui/card`, `@/components/ui/badge`, `@/components/ui/dialog`, `@/components/ui/input`, and `@/components/ui/progress`, and import `cn` from `@/lib/utils`.
- Tailwind CSS v4 utility classes are built in and work in any `className`, including to restyle the built-in `@/components/ui/*` components (a utility overrides the component default). `src/styles.css` is the CSS entry and must keep `@import "tailwindcss";` and `@import "@avibe/show-ui/theme.css";` at the top. Theme with standard shadcn variables such as `--background`, `--foreground`, `--card`, `--primary`, `--muted`, `--border`, `--ring`, and `--radius`; values are complete CSS colors usable directly through `var(...)`. Override the same variables under `.dark` or `[data-theme="dark"]` for dark mode. Do not use runtime-prefixed private variables.
- Prefer the built-in UI primitives over hand-rolled controls. They include Show Page motion for changed text, numbers, badges, cards, and progress without extra animation calls.
- Optional server handlers live under `api/` and run only when requested. Export functions named like HTTP methods, for example `export async function GET(request) { return Response.json({ ok: true }) }`.
- Design for user understanding, not just for moving text onto a webpage. Choose the visual form that best helps the user inspect, compare, confirm, and continue the discussion.
- Use diagrams or mind maps for relationships, flowcharts or state machines for processes, timelines for sequences, charts or dashboards for metrics, and side-by-side views for tradeoffs.
- Make the page visually polished: use clear hierarchy, spacing, typography, contrast, and consistent components. Avoid rough default-looking pages.
- Give the app a recognizable icon so it stands out in the Dock and App Library: drop a `public/favicon.svg` (or `favicon.svg` at the workspace root) and it is picked up automatically, or add `<link rel="icon" href="./favicon.svg">` to `index.html` (an icon edit to the shell is fine).
- Make the page work reasonably on mobile because users may open links from an IM app on their phone.
- Prefer React component implementations. Useful visualization libraries include React Flow, Mermaid, Markmap, Chart.js, and Cytoscape.js.
- Keep pages private by default. Publish publicly only when the user asks for a shareable or public link.
- Do not publish secrets, credentials, private logs, or sensitive user data publicly.
- If a Show Page would clearly help but the user's preference is unclear, briefly ask whether they want one.
- After creating or updating a page, send the active URL and a short summary of what the page shows.
