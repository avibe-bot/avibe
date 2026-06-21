# Setup Wizard Revamp + Provider-Config Reuse

## Background
User feedback on `/setup` (the first-run Wizard). Two steps redesigned + a new
provider-config modal. Design proposals were built in `design.pen` (Platforms
`jXiOv`, Backends `JHgjz`, modal scene `yKgt1`) and approved in direction. The
modal must NOT be a mock: it must render the **real** `/admin/settings/backends/<backend>`
provider config (UI + request logic), shared 100% with the settings pages — no
copy-paste.

## Architecture (verified)
The provider config bodies are already route-clean. The prior refactor extracted
`settings/shared/useBackendRuntime`, `BackendRuntimeCard`, `BackendOAuthPanel`,
`BackendTestPanel`, `OpencodeProviderTestPanel`, `useOAuthFlowLock`,
`surfaceBackendNotices`. The only route-bound wrapper is `SettingsPageShell` +
the breadcrumb `<Link>`. No page uses `useParams`/`useNavigate`.

Reuse target: peel each page's config body (everything inside the
`!runtime.loaded` gate) into a reusable component; the page becomes
`SettingsPageShell + <XxxProviderConfig/>`; the wizard modal renders the same
component via a `backend`-switch dispatcher inside the existing Radix `Dialog`
(`ui/src/components/ui/dialog.tsx`, widen to `max-w-3xl`).

## Steps
1. **Extract provider config components** → `ui/src/components/settings/providers/`:
   - `ClaudeProviderConfig.tsx` (Claude page lines ~214–476: RuntimeCard + auth Card + BackendTestPanel + auth state/effects)
   - `CodexProviderConfig.tsx` (Codex page ~224–490; incl. surfaceBackendNotices)
   - `OpencodeProviderConfig.tsx` (OpenCode page ~509–1180: RuntimeCard w/ permission slot + provider catalog; keep retryTimer cleanup useEffect)
   - Move the loading gate INTO each config component.
   - `BackendProviderConfig.tsx` — thin `switch(backend)` dispatcher.
2. **Slim the 3 settings pages** to `SettingsPageShell` + `<XxxProviderConfig/>`. No behavior change on the settings route.
3. **AgentDetection.tsx** (wizard backend step) revamp:
   - Remove the **Default backend** selector (+ DEFAULT pill); also from the shared/settings surface. `agents.default_backend` is deprecated and should not be emitted by the wizard.
   - Keep **auto-detect** (`detectAll` on mount) + a Re-scan affordance.
   - **Missing backend → one-click Install** (existing `installAgent`).
   - **OpenCode → Set up Allow / permission** (existing `opencodeSetupPermission`).
   - **Two-row backend card**: top row = identity + (status + enable toggle) at the end; second row = action buttons.
   - Ready backend → **"Configure provider"** button opening `<Dialog>` with `<BackendProviderConfig backend={name}/>`; on close, re-run `detectAll()`.
   - Plain-language copy ("Set up your agent backends").
4. **PlatformSelection.tsx** (wizard platform step) revamp:
   - Two columns: left = **Avibe Workbench required/locked** card (mascot, REQUIRED, PWA "add to home screen" note, always-on); right = **optional** third-party IM logos (toggleable, no token entry) + single soft skip mention.
   - Remove inline token entry from the wizard; defer credential config to Settings → Platforms.
   - `WORKBENCH_PLATFORM_ID='avibe'` always enabled, not deselectable.
   - Plain-language heading.
5. **Wizard.tsx flow**: KEEP the per-platform IM credential steps
   (SlackConfig/DiscordConfig/…) as-is — they remain in the first-run flow for
   each selected chat platform (skippable via the wizard Skip button). The
   platform step is selection-only, but the wizard still walks each selected
   IM's credentials. ONLY the always-on workbench (avibe) is excluded from
   credential + channel step generation (it has no credentials). Do NOT
   remove/move the IM credential steps — that was an incorrect inference, not
   requested by the user.
6. **i18n**: add all new strings to `ui/src/i18n/en.json` + `zh.json` (1:1).
   Plain Chinese per user ("安装并配置 Agent 后端", "可跳过…在设置页继续配置", etc.).
7. **Verify**: `npm run build`; reviewer subagent; manual sanity; PR (non-draft) + Codex review watch.

## Decisions (from user)
- Workbench label = **"Avibe Workbench"** (consistent with PR #450).
- Skip-IM mention: once, gentle (subtitle), not emphasized.
- Default-backend selector: remove **everywhere** (setup + settings).
- Modal = 100% reuse of real provider page component; per-backend config
  components behind a dispatcher (Claude/Codex = auth form; OpenCode = provider
  catalog). No copy-paste.

## Notes / risks
- Built on top of PR #450 branch (avibe icon + `platform.avibe` i18n). Rebase
  onto master once #450 merges.
- OpenCode config owns a retry timer — keep its `useEffect` cleanup so the
  Dialog unmount clears it.
- `BackendTestPanel` type currently excludes `opencode` (uses OpencodeProviderTestPanel instead) — preserve.
