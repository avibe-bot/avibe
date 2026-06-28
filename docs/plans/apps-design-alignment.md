# Apps — design-fidelity alignment (post-#679 polish)

After #679 shipped windowed Apps, runtime eyeballing surfaced that the three apps
drifted from their **design.pen** frames (built code-green but never visually
verified against the design). Alex's directive: **strictly align all three apps to
their design frames, design-first, no free-styling.** Build → Codex real-browser
visual cross-check vs the frames → PR. (See memory `feedback_design_fidelity_visual_gate`.)

Branch: `feat/apps-windowing-polish` off origin/master.

## Design targets (all confirmed in design.pen, 2026-06-28)
- **File Browser → `nknn2`** — pure full-width Finder: favorites/projects rail +
  Name/Size/Modified columns + toolbar (breadcrumb, search, New File/New Folder) +
  status bar. **No in-pane editor** (browser does not edit). Clicking a code file
  opens the Editor window (open-by-default model, refine later); unsupported files
  show a prompt.
- **File Editor → `dnYPx` + `w0qoC` (welcome)** — VS-Code IDE: activity bar +
  collapsible explorer file-tree + editor tabs + Monaco + cyan status bar; welcome/
  empty state (Open Folder / New File / Open Recent) when no folder. Monaco fully
  integrated here. **Terminal embeddable later** as an integrated panel (build the
  Terminal reusably; design that variant first when we get to it).
- **Terminal → `iwYIX`** — multi-tab (each tab = a session) + `tmux · persistent`
  badge + accessory key bar. Built as a **reusable component**. Also fixes the
  sizing/scrollback bug (rows must fit the container; scroll history works).

## Windowing / sidebar (new gap designs, confirmed)
- Editor welcome state `w0qoC`; sidebar bottom `bVke5` (prominent Apps pill wider
  than a compact Settings gear; version row = green dot left, version right);
  Dock new-window menu `UkkCV` (right-click / hover ＋ → New Window / Show All
  Windows; multi-instance reachable); maximize-over-sidebar `If1Tt` (window layer =
  full viewport, over the sidebar; Apps button / Dock float on top, always reachable).

## Small fixes
- AppWindow traffic-light ×/–/+ glyph vertical centering.
- `/admin` label "工作区" → "设置".

## Process
Build strictly to the frames → `npm run build` + UI vitest green → **Codex views it
in a real browser and cross-checks each app vs its design frame for omissions** →
open PR (not draft) → Codex review loop to pass → Alex eyeballs.
