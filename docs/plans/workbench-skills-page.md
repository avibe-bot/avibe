# Workbench Skills Page — Implementation Plan

> **Branch**: `feature/workbench-skills` (from `origin/master` @ `88c8afe`)
> **Status**: Design finalized 2026-05-30 — ready to build.
> **Design source of truth**: `../avibe-docs/design.pen` — frames listed in §2. Read them via the `pencil` MCP tools, not text.
> **Owner**: cyhhao
> **Core principle**: extreme reuse + first-principles. Reuse a primitive → extend it → only then build new. Never re-roll a one-off variant inline. **Pixel-perfect 1:1 with the design is a hard requirement** (see §8).

## 1. Goal

Replace the `SkillsPage` placeholder (`ui/src/components/workbench/SkillsPage.tsx`, currently a 6-line `WorkbenchModulePlaceholder`) with the full Skills management surface: manage **global** and **project-scoped** Agent Skills across the Claude / OpenCode / Codex backends, search/discover from the askill.sh registry, and import skills from a **GitHub URL** or an **uploaded .zip**. The backend is a thin shell over the open-source **askill** CLI (`github.com/avibe-bot/askill`) in `--json` mode — Vibe Remote owns UI + orchestration only.

This is a greenfield build: `/skills → SkillsPage` and the sidebar nav (`CAPABILITY_NAV`, `WorkbenchSidebar.tsx:36-41`) are already wired.

**Out of scope (deferred):** Light theme + mobile variants. The whole new Workbench is dark-desktop-only today (only the Chat/Agents/Harness/Skills dark frames exist); Skills follows that until the Workbench gets a responsive/light pass as one coordinated effort.

## 2. Design source of truth (design.pen frames)

All Dark desktop, 1440×900, in the `WORKBENCH · SKILLS` shelf of `design.pen`:

| Frame ID | Name | What it specifies |
| --- | --- | --- |
| `ua2wX` | Skills (Dark) | **Primary page.** Global scope. Header + scope/toolbar + flat skill list (rows) + 400px detail panel (pdf-tools selected). |
| `ZigTZ` | Skills · Project scope (Dark) | Project tab active, **project-picker dropdown** (open state), project path context bar, two sections (PROJECT SKILLS / INHERITED FROM GLOBAL dimmed), full-width list (no detail panel). |
| `gNDdF` | Skills · Add from GitHub (Dark) | "Add a skill" modal, **GitHub URL** source tab active: URL input + Fetch → discovered-skills checklist → scope + backend targets → Install. Footer shows the literal `askill add …` command. |
| `KR9B0` | Skills · Upload ZIP (Dark) | Same modal, **Upload .zip** source tab active: dashed dropzone + uploaded-file card + "local, nothing uploaded" note → same discovered/scope/backends/install flow. |
| `R2OBP` | Skills · Browse registry (Dark) | Registry search modal: search + tag filters + result cards (aiScore badge, owner/repo, tags, stars, Add / Installed). |

When building, open each frame and read exact values (sizes, paddings, fills, effects) with `mcp__pencil__batch_get`. The token names in the frames (`$--surface-2`, `$--mint`, `#5BFFA066`, …) map 1:1 to `ui/src/index.css` (§8.2).

## 3. Architecture

```
SkillsPage / dialogs (React)
  → useApi() methods (ui/src/context/ApiContext.tsx)
    → GET/POST/DELETE /api/skills/*            (vibe/ui_server.py @app.route shims)
      → vibe/api.py  list_skills / add_skill / remove_skill / find_skills / preview_source
        → core/services/skills.py  SkillsService          (transport-agnostic business layer)
          → askill <cmd> --json                            (subprocess; resolve_cli_path("askill"))
```

This matches the existing `storage → api.py → ui_server.py` layering and the `core/services/` direction set by `docs/plans/workbench-dispatch-architecture.md` (Accepted). The Skills surface is **pure data CRUD** (list/add/remove/find) — it stays in the UI-server process via `core/services/`, and does **not** go through the Controller dispatch socket (boundary rule R1).

## 4. Backend plan

### 4.1 `core/services/skills.py` — new `SkillsService`

Transport-agnostic. Wraps the askill CLI; returns plain `dict`s; raises `ValueError`/`LookupError` for the route layer to translate. Model the subprocess execution on the proven pattern in `core/agent_auth_service.py` (`_run_utility_command` `:988-1016` and `test_web_auth` `:1752-1908`, which already do `create_subprocess_exec` + `wait_for(communicate(), timeout)` + `json.loads(stdout)` + error classification; `_verify_login` `:1238-1283` runs `claude … --json` and parses it — direct precedent).

Helper (async):

```python
async def _run_askill_json(args: list[str], *, cwd: str | None = None, timeout: float = 30.0) -> dict:
    cli = resolve_cli_path("askill")          # api.resolve_cli_path — checks ~/.local/bin, ~/.bun/bin, brew, npm globals, then which
    if not cli:
        raise LookupError("askill_not_found")
    proc = await asyncio.create_subprocess_exec(
        cli, *args, "--json",
        cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill(); await proc.communicate(); raise TimeoutError("askill_timeout")
    data = json.loads(out or b"{}")            # askill emits machine JSON even on non-zero exit in --json mode
    return data                                # caller inspects data["ok"] / data["error"]
```

### 4.2 askill command mapping (verified against askill `docs/cli-reference.md` + `docs/json-contracts/`)

| Service method | askill invocation | Notes |
| --- | --- | --- |
| `list(scope, project_id)` | `askill list [-g\|-p] [-a <agent>] --json` | scope: global→`-g`, project→`-p` (run with `cwd=<project folder>`). Response: `{ok, filters{scope,agents[]}, summary{global,project}, skills[]{name,scope,path,agents[]{id,name}}}`. |
| `preview(source)` | `askill add <slug> --json` (no `-y`) | For multi-skill repos askill returns `action:"preview"` + discovered `skills[]{name,description,frontmatter}` (or error `MULTIPLE_SKILLS_REQUIRE_SELECTION`). slug forms: `gh:owner/repo`, `gh:owner/repo@name`, `gh:owner/repo/path`, full URL, or a local dir/zip path. |
| `add(source, scope, agents, all)` | `askill add <slug> [-g] [-a a b] [--all] -y --json` | Install. Response: `{ok, action:"install", source{}, scope, selectedAgents[], summary{}, results[]}`. |
| `remove(name, scope, agents)` | `askill remove <name> [-g] [-a …] --json` | Response: `{ok, skill, scope, removedAgents[], skippedAgents[], failed[]}`. |
| `find(query)` | `askill find <query> --json` | Registry search. Response: `{ok, query, count, skills[]{id,name,description,owner,repo,tags[],stars,aiScore,aiBreakdown[]{key,label,score},updatedAt,installSource,url}}`. |

**ZIP upload flow:** the UI uploads the `.zip` to a `POST /api/skills/upload` endpoint → backend saves to a temp dir, unzips into a scratch folder, then runs `preview`/`add` against that **local path** (`askill add /tmp/…/unpacked --all -y --json`). Clean up the temp dir after. Nothing leaves the machine (mirror the design's "installed locally" copy).

**Backend "Backend" filter** = askill `-a <agent>`. Map Vibe Remote backends → askill agent ids: `claude→claude-code`, `opencode→opencode`, `codex→codex`. Centralize this map in `skills.py`.

### 4.3 Known askill gaps (tracked in [avibe-bot/askill#11](https://github.com/avibe-bot/askill/issues/11))

`list --json` does not yet carry per-skill `description`/`version`/`source`, and `check`/`info` have no `--json`. **Until #11 lands**, enrich installed rows in `SkillsService.list()` by reading each skill's canonical `~/.agents/skills/<name>/SKILL.md` (or `<project>/.agents/skills/<name>/SKILL.md`) frontmatter for `description`/`version` — the path is already in the `list` payload. The "update available" badge is deferred until `check --json` exists (don't fake it). Keep the enrichment isolated so it can be deleted once #11 ships.

### 4.4 `vibe/api.py` + `vibe/ui_server.py`

Add pure functions in `api.py` (`async def list_skills(...)`, `add_skill`, `remove_skill`, `find_skills`, `preview_source`, `upload_skill_zip`) that call the `SkillsService`. Add thin `@app.route` shims in `ui_server.py` following the established pattern (`vibe_agents_get` `:1600-1612`): lazy `from vibe import api`, parse `request.args`/`request.json`, `await api.<fn>_async(...)` for async (NO `asyncio.run` bridges — AGENTS.md hard rule), `return jsonify(...)`. Routes:

- `GET  /api/skills?scope=&project_id=`
- `POST /api/skills/preview` `{source}`
- `POST /api/skills` `{source, scope, project_id?, agents[], all?}`
- `DELETE /api/skills/<name>?scope=&project_id=&agents=`
- `GET  /api/skills/find?q=`
- `POST /api/skills/upload` (multipart .zip) → preview payload

## 5. API client plan (`ui/src/context/ApiContext.tsx`)

Add 6 methods. **Three mechanical edits each** (do NOT touch the `useMemo([showToast, t])` deps — memoization is load-bearing, see the comment at `:816-839`):
1. Signature in the `ApiContextType` block (`:6-131`).
2. `export type`s near the other workbench types (`:136-689`): `SkillBrief` (`{name, scope:'global'|'project', path, agents:{id,name}[], description?, version?, source?}`), `SkillSearchResult` (mirrors askill `find` item), `SkillManifest` (preview: `{source, skills:{name,description}[]}`).
3. Impl inside the `useMemo` object literal (`:840-1115`) reusing `getJson`/`postJson`/`deleteJson` + `URLSearchParams` + `encodeURIComponent` exactly as `listVibeAgents`/`removeVibeAgent` do (`:1009-1028`).

Methods: `listSkills`, `previewSkillSource`, `addSkill`, `removeSkill`, `findSkills`, `uploadSkillZip`. Backend error `error.code` is surfaced via the existing `errors.<code>` i18n lookup (`:725`).

## 6. Frontend component plan — reuse / extend / new

**Page shell** — `SkillsPage.tsx`, single `/skills` route, **local state** for scope, selected skill, and dialog open/closed (this app uses local state for modals, not URL params — `AgentsPage` is the template). Wrapper: `<div className="mx-auto flex w-full max-w-[1200px] flex-col gap-5 py-2">`. Body is the master-detail grid `grid gap-5 lg:grid-cols-[1fr_400px]` (Global scope, detail open); Project scope is single-column full-width.

### 6.1 Reuse AS-IS
- **`Button`** (`ui/components/ui/button.tsx`): toolbar = `variant="outline" size="xs"`; primary "Add skill" = `variant="brand" size="xs"`; "Browse registry" = `variant="outline" size="xs"` (cyan icon); Remove = `variant="destructive-soft" size="xs"`; icon-only = `variant="ghost" size="icon"`.
- **`Badge`**: GLOBAL/inherited tags + status pills. Quality "AI 9.5" pill = `badgeVariants` on a span. Backend chips build on it (§6.3).
- **`Switch`** (`checked`/`onCheckedChange`/`label`): the per-backend link toggles in the detail panel's "Available to" section.
- **`Popover`** (`Popover`/`PopoverTrigger`/`PopoverContent`): the dropdown idiom for the **Backend filter**, the **project picker** (Project scope), and skill-row `⋯` actions. Copy the `BackendFilter`/`ImportMenu` markup from `AgentsPage.tsx:390-468` and the project `…` menu from `WorkbenchSidebar.tsx:238-280`.
- **`Combobox`** + `ComboboxOption {value,label}`: only if a searchable select is needed (not strictly required by these frames).
- **`Input`**, **`Label`**, **`Separator`**, **`Card`**: as-is.
- **`DirectoryBrowser`** (`ui/components/ui/directory-browser.tsx`): reuse for "Open a project folder…" in the project picker if we let users add a project from here.
- **`SegmentedRadio`** (`ui/components/settings/shared/SegmentedRadio.tsx`, generic `SegmentedRadio<T>`): the **Global / Project** scope switch in the toolbar, and the GitHub/ZIP source switch + Global/Project scope inside the Add dialog. → **Promote it to `ui/components/ui/segmented.tsx`** (it's now used outside settings); update the settings imports. This is the "reuse, don't fork" principle applied.
- **`useToast()`**, **`useApi()`**, AgentsPage's **`Field`** helper (`:835-847`, mono-uppercase label + slot) for the detail panel rows.

### 6.2 Extend / promote
- **`WorkbenchPageHeader`** — NEW shared component in `ui/components/workbench/`, props `{ icon, title, subtitle, actions? }`. AgentsPage, HarnessPage, and Skills all hand-roll the identical "40px mint-soft icon box + 24px title + 12px subtitle + Refresh" header (`AgentsPage.tsx:223-236`). Extract it once, render it in Skills, and **retrofit AgentsPage + HarnessPage** to use it. (User principle: don't duplicate; package and reuse.)
- **`SegmentedRadio` → `ui/`** (promotion, above).

### 6.3 New components (package, don't inline)
Each is a real reusable component so the list, detail panel, and dialogs share them — no per-call rewrites.
- **`ui/components/ui/checkbox.tsx`** — there is NO checkbox primitive. Add one (Radix `@radix-ui/react-checkbox` is already in the dep tree via other Radix primitives) for the discovered-skills multi-select in the Add dialog. Mint check on `bg-mint`, `bg-surface-3` + `border-border-strong` unchecked — matches design.
- **`SkillRow`** — one installed-skill row: lead icon, name + scope/inherited badge, description, source·version·updated meta, `BackendChip[]`, `⋯` actions. Used by both Global and Project lists. `selected` + `inherited` (dimmed) states.
- **`BackendChip`** — small pill (dot + label) colored by backend. Reuse the backend→color map (claude=mint, opencode=cyan, codex=violet) — lift `BACKEND_LABEL`/`BACKEND_ICON_CLASS` from `AgentsPage.tsx:36-46` into a shared `lib/backends.ts` and import in both.
- **`QualityMeter`** — the AI-quality block: big aiScore + 5 labeled bars (Safety/Clarity/Reusability/Completeness/Actionability), colored by score (≥9 mint, ≥8.5 cyan, else gold). Used in the detail panel and (compact aiScore pill) in Browse cards.
- **`FileDropzone`** — dashed-border drop area with uploaded-file card + Replace; emits the File for `uploadSkillZip`. Generic enough to reuse later.
- **`SkillDetailPanel`** — 400px panel: source card (repo/branch/path/stars/View-on-GitHub), description, `QualityMeter`, "Available to" backend `Switch` rows, commands (`askill run …` mono chips), footer (Open SKILL.md / Remove). Reuses `Field`, `Switch`, `Button`.
- **`AddSkillDialog`** — the "Add a skill" modal. Top `SegmentedRadio` source switch (GitHub URL / Upload .zip); GitHub state = URL `Input` + Fetch; ZIP state = `FileDropzone`; shared lower half = discovered `Checkbox` list + scope `SegmentedRadio` + backend `Checkbox`/chip multiselect + footer CLI-hint + Cancel/Install. **Follow the existing workbench dialog convention** (`NewAgentDialog`/`NewProjectDialog` hand-roll a fixed overlay — match that, don't introduce the Radix `Dialog` here unless we migrate all of them).
- **`BrowseRegistryDialog`** — search `Input` + tag-filter pills (`Popover`-less inline toggles) + scrollable result cards (`QualityMeter` compact + stars + tags + Add `Button`). Calls `findSkills`.
- **`ProjectPicker`** — `Popover` dropdown listing `useApi().listProjects()` results (the same projects shown in the sidebar), active one checked, with skill counts + "Open a project folder…". Shown in Project scope.

### 6.4 Surface → frame mapping
- Global page → `ua2wX`. Project page + picker → `ZigTZ`. Add dialog (GitHub) → `gNDdF`. Add dialog (ZIP) → `KR9B0`. Browse dialog → `R2OBP`.

## 7. i18n (`ui/src/i18n/en.json` + `zh.json`, kept 1:1)

Add a top-level `"skills": { … }` block in BOTH files, modeled on the rich `agents.*` block (`en.json:1244-1314`): `title`, `subtitle` (`{{count}}`), `searchPlaceholder`, `scopeGlobal`, `scopeProject`, `browseRegistry`, `addSkill`, `empty`, `noSearchMatch`, `removeConfirm` (`{{name}}`), nested `detail:{…}`, `addDialog:{ githubTab, zipTab, urlLabel, fetch, dropzoneHint, foundCount, installTo, backends, install }`, `browse:{…}`. Reuse `common.*` (`refresh`, `cancel`, `delete`, `close`, …) — don't re-add. Backend error codes resolve via `errors.<code>` — add any new askill codes (`askill_not_found`, `MULTIPLE_SKILLS_REQUIRE_SELECTION`, `INVALID_AGENTS`, …).

## 8. Pixel-perfect mandate (the part that has gone wrong before)

**Rule: every number in the design maps to an exact class — never eyeball it.** Build from this table, then verify by screenshot-diff.

### 8.1 Exact specs (from the frames)
- **Page**: `max-w-[1200px] gap-5 py-2`. Body grid `lg:grid-cols-[1fr_400px] gap-5`.
- **Header icon box**: `size-10 rounded-[10px] border border-mint/40 bg-mint-soft text-mint shadow-[0_0_18px_-6px_rgba(91,255,160,0.5)]`; icon `size-5`. Title `text-[24px] font-bold text-foreground`. Subtitle `text-[12px] text-muted`.
- **Toolbar**: `flex items-center gap-2.5`. Search: `flex items-center gap-2 rounded-md border border-border-strong bg-surface px-3 py-2`, icon `size-3.5 text-muted`, input `text-[12px] … placeholder:text-muted`. (Note `rounded-md` = **8px** here.)
- **Skill row**: `rounded-xl border px-4 py-3` (16px radius); default `border-border bg-surface`; selected `border-mint/40 bg-mint-soft shadow-[0_0_18px_-10px_rgba(91,255,160,0.6)]`; inherited `opacity-[0.66]`. Lead icon `size-9 rounded-[9px] bg-surface-3 border border-border`. Name `text-[14px] font-semibold`; desc `text-[11.5px] text-muted`; meta `font-mono text-[10px] text-muted`.
- **Backend chip**: `rounded-full px-2 py-0.5 border` + `bg-mint-soft text-mint border-mint/40` (claude) / cyan / violet; `font-mono text-[10px]`.
- **Detail panel**: `rounded-2xl border border-border-strong bg-surface p-5` (the design uses surface; AgentsPage uses the same). Section labels: `font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted`.
- **Dialog**: `rounded-2xl border border-border-strong bg-surface-2` + `shadow-[0_24px_60px_-12px_rgba(0,0,0,0.7)]`; scrim `bg-[#05050B]/[0.72] backdrop-blur-[6px]`; footer band `bg-surface-3 border-t border-border`. CLI hint `font-mono text-[10.5px] text-muted`.
- **Fonts**: body = **Inter**; all labels/badges/meta/code/CLI = **JetBrains Mono**. Weights per frame (600 semibold names, 700 bold titles/eyebrows).
- **Radii**: 6=`rounded-sm`, 8=`rounded-md`, 12=`rounded-lg`, 16=`rounded-xl`, 20=`rounded-2xl`, pill=`rounded-full`.

### 8.2 Token classes (all verified to resolve in `ui/src/index.css`)
`bg-background bg-surface bg-surface-2 bg-surface-3 text-foreground text-muted bg-mint-soft text-mint bg-mint/[0.08] bg-cyan-soft text-cyan bg-violet-soft text-violet text-gold border-border border-border-strong border-mint/40`. Backend brand fills via these; mint glow via the `--shadow-mint-card` token or the explicit arbitrary shadows above.

### 8.3 ⚠️ Do NOT use `text-pink` / `bg-pink/*` / `border-pink/*`
There is no `--pink` token in `index.css`; those utilities compile to **nothing** (verified against `ui/dist`). AgentsPage/WorkbenchSidebar use them but they silently no-op. For destructive/remove use `Button variant="destructive-soft"` (its pink is a baked arbitrary hex) or `destructive`. If a true pink accent is ever wanted, first add `--pink`/`--pink-soft` to the `@theme inline` + every theme block in `index.css`.

### 8.4 Verification loop (mandatory, per surface)
1. `npm run build` / dev-serve the UI.
2. Screenshot the surface at exactly **1440×900, dark** (use the `chrome-devtools` MCP or the `verify`/`browse` skill; resize viewport to 1440 wide).
3. Export the matching `design.pen` frame to PNG and put them side by side; enumerate deltas in spacing, font-size, weight, color, radius, shadow, alignment.
4. Fix until they match. Do not mark a surface done on "looks close" — match the table in §8.1.
5. Run the existing UI build + a quick interaction pass (open dialog, toggle scope, etc.).

## 9. Build order (atomic commits on this one branch; one PR at the checkpoint)

1. **C1 · backend** — `core/services/skills.py` + `api.py` fns + `ui_server.py` routes + a focused pytest (mock the CLI: feed canned `--json` fixtures, assert parsing/shape; model on existing service tests). No UI yet.
2. **C2 · api client + i18n** — `ApiContext` methods/types; `skills.*` en+zh (1:1).
3. **C3 · primitives** — promote `SegmentedRadio`→`ui/`; add `ui/checkbox.tsx`; add `lib/backends.ts`; extract `WorkbenchPageHeader` + retrofit Agents/Harness.
4. **C4 · list + detail** — `SkillRow`, `BackendChip`, `QualityMeter`, `SkillDetailPanel`, `SkillsPage` Global scope (frame `ua2wX`). Pixel-verify.
5. **C5 · project scope** — `ProjectPicker`, two-section list, context bar (frame `ZigTZ`). Pixel-verify.
6. **C6 · add dialog** — `AddSkillDialog` + `FileDropzone` + upload endpoint wiring (frames `gNDdF`, `KR9B0`). Pixel-verify.
7. **C7 · browse dialog** — `BrowseRegistryDialog` (frame `R2OBP`). Pixel-verify.

Per AGENTS.md: before opening the PR run the reviewer subagent and fix significant issues; open a **non-draft** PR naming the capability + evidence layers (unit/contract/manual); then keep a Codex review-fix loop via the `background-watch-hook` skill. `ruff check` changed Python before pushing; `npm run build` in `ui/`.

## 10. Reuse rules (restated — the project's core principle)

- Reuse an existing `ui/` primitive. If it doesn't fit, **extend it** (new `variant`/`size`/prop) or **promote** a near-duplicate (e.g. `SegmentedRadio`) into `ui/`. Only build new when nothing fits — and then build it as a real reusable component in the right place, not inline in a feature file.
- The source of truth for variant names, radii, colors is `design.pen` ↔ `index.css`. Extend primitives to match those names so design ↔ code stay aligned.
- One backend→color map, one page-header, one segmented control, one checkbox — shared, not copied. Touching three pages with the same header means extracting the header, not pasting it three times.

## 11. Open dependencies & gotchas
- **askill#11** (check/info `--json`, list metadata, find tag/limit) — build against the current contract; enrich `list` via SKILL.md frontmatter until it lands; defer the "update available" badge.
- **askill must be installed** — `resolve_cli_path("askill")` may return `None`; surface a clear empty/CTA state ("Install askill" with the one-liner) instead of erroring.
- **Memoized `ApiContext`** — don't break `useMemo([showToast, t])`.
- **No `asyncio.run()`** in UI request paths; await on the loop.
- **No textarea primitive** — hand-roll if needed (as AgentsPage does), or add one to `ui/`.
- **Dialog overlay** — match the hand-rolled workbench dialog convention, not the Radix `Dialog`, unless migrating all workbench dialogs together.
