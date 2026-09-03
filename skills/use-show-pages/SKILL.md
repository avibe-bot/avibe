---
name: use-show-pages
slug: use-show-pages
description: Build, inspect, update, or share an Avibe Show Page when a visual explanation, diagram, dashboard, report, or interactive prototype would materially help.
version: 0.1.0
---

# Use Show Pages

Use this skill when a visual page would make a relationship, process, result, or
decision substantially easier to understand than chat alone.

## Core Workflow

1. Resolve the current Session's workspace with `vibe show path`.
2. Inspect the current state with `vibe show status`.
3. Build or update the page in that workspace.
4. Keep it private unless the user explicitly asks for a shareable public page.
5. After updating it, send the active URL and a short description of what it shows.

If public visibility is requested and `vibe show status` reports that Avibe
Cloud is not connected, load the `use-avibe` Skill and follow its remote pairing
workflow. Do not assume connection state from the System Prompt.

Visibility commands:

```bash
vibe show update --visibility public
vibe show update --visibility private
vibe show update --visibility offline
```

Read `vibe show --help` or the relevant subcommand help when behavior is
uncertain. The current Avibe Agent Session is the default target; pass an
explicit Session only when the task names a different one.

## Choose The Right Visual

- Use a flowchart or state machine for a process.
- Use a timeline for ordered events.
- Use a table for exact mappings or comparisons.
- Use a graph or tree for relationships and hierarchy.
- Use a dashboard for several related metrics.
- Use a side-by-side view for trade-offs.

Do not create a page merely to restate prose. Design for inspection,
comparison, confirmation, and the next decision.

## Page Runtime

New workspaces are managed React/Vite applications with a minimal file-based
router. When `src/router.tsx` exists, add routes under `src/pages/`; folders are
nested path segments and `[param]` files are dynamic segments. Customize shared
layout in `src/App.tsx`, styles in `src/styles.css`, and optional handlers in
`api/*.ts`.

Older workspaces without `src/router.tsx` render `src/App.tsx` directly. Update
that file or deliberately adopt the router scaffold; files placed under
`src/pages/` are otherwise unreachable.

Treat `index.html` and `src/main.tsx` as runtime-owned shell files. Do not
replace them to add a page unless repairing the shell. Hot reload is available,
so prefer component-level edits that preserve state.

Use the built-in shadcn components before hand-rolling controls. Common imports
include `@/components/ui/button`, `@/components/ui/card`,
`@/components/ui/badge`, `@/components/ui/dialog`,
`@/components/ui/input`, and `@/components/ui/progress`; import `cn` from
`@/lib/utils`.

`src/styles.css` must keep these imports at the top:

```css
@import "tailwindcss";
@import "@avibe/show-ui/theme.css";
```

Tailwind CSS v4 utilities are available. Theme with standard shadcn variables
such as `--background`, `--foreground`, `--card`, `--primary`, `--muted`,
`--border`, `--ring`, and `--radius`. Override the same variables under `.dark`
or `[data-theme="dark"]` for dark mode.

Optional server handlers export HTTP method functions, for example:

```ts
export async function GET(request) {
  return Response.json({ ok: true })
}
```

## Agent-Readable Pages

Every private or public Show Page URL can be requested with
`Accept: text/markdown`. Author semantic HTML so this representation remains
dense and useful: use headings for sections, lists for sequences or groups, and
`<table>` for genuinely tabular data.

Add `data-agent-hidden` to visual-only or sensitive elements that should be
omitted from Markdown. Add `agent-note="..."` when an element needs short
agent-only context.

## Annotations

User annotations arrive as chat messages tagged `[show-annotation]` with an
event id. Respond either by editing the page or through the page reply command,
depending on what the annotation asks.

After reworking an area, you may leave at most one or two short callouts:

```bash
vibe show mark <selector-or-anchor> --message '...'
```

Inspect or withdraw marks with `vibe show marks` and `vibe show unmark`. Toggle
annotation mode with `vibe show annotate --on|--off [--mode smart|screenshot]`.

## Design And Safety

- Make the page polished, responsive, and usable on mobile.
- Prefer React components. React Flow, Mermaid, Markmap, Chart.js, and
  Cytoscape.js are available when they fit the visual.
- Add a recognizable `favicon.svg` so the page stands out in the Dock and App
  Library.
- Keep pages private by default.
- Never publish secrets, credentials, private logs, or sensitive user data.
- Avibe may checkpoint managed Show Page history around turns. Do not manage
  versions yourself: never create commits, move HEAD, rewrite history, add
  remotes, push, or publish unless the user explicitly asks. Read-only
  `git status`, `log`, `diff`, and `show` are fine.
- If the workspace is already the user's own repository, treat its Git history
  as user-owned and do not commit or restore on their behalf.
