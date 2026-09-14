# Memory profile switch

Approved scope, consolidated after PM simplicity review on 2026-09-14.
Inspected base: origin/master ab8c22867e58cf8a98d6815c9737ea2ce74fc9c8.

## Product contract

Add one persisted boolean, `memory.profile_enabled`, default true (including old configurations without the field). Use the existing Memory settings toggle/save flow. When Memory itself is disabled, retain the preference but do not activate any Memory work.

OFF means:
1. Hide only the profile tab. Filter the existing tab array and reuse existing `activeTab` fallback to `processingRecord`. No new effect, navigation, state synchronization or persisted tab preference. Keep search/settings and admin gating unchanged.
2. Omit profile-specific command guidance/content from newly constructed Agent prompts. Do not add a prohibition sentence. Keep all ordinary Memory guidance. Do not attempt to remove historical transcript text or arbitrary user-supplied references to profiles.
3. Stop future automatic EverOS profile clustering and extraction. Preserve ordinary capture/search and historical profile data. Already-running jobs may finish; no cancellation framework.
4. Shared profile-read entry returns existing `memory_disabled` closed-error result (HTTP409 at the UI boundary), including CLI access. Ordinary search clamps `include_profile` to requested value AND profile_enabled. This is feature policy, not a new permission system.

ON restores profile tab, guidance, reads and future processing. No automatic regeneration/migration or deletion of historical data.

## Minimal implementation

- Configuration: extend MemoryConfig and existing validation/serialization/atomic update in `config/v2_config.py`; carry through `vibe/ui_memory_routes.py` settings projection/patch/apply and existing reconciliation. Update UI types in `ui/src/context/ApiContext.tsx`.
- UI: existing Toggle in `ui/src/components/settings/memory/MemorySettingsPanel.tsx`, tab filtering only in `ui/src/components/settings/SettingsMemoryPage.tsx`; English/Chinese copy in `ui/src/i18n/en.json` and `zh.json`. Approved sketch: one 'Enable profile' toggle; existing tabs processingRecord/profile/search/settings minus profile when off. No new UI component or design system.
- Prompt: `core/prompts/memory-context.md:15` currently advertises `vibe memory profile --json`. Make that profile-only guidance conditional using the existing registry/template composition (`core/prompt_registry.py`, `core/system_prompt_injection.py`). Prefer a small conditional block/slot over duplicated templates or text replacement after rendering. Pass the single boolean through existing caller paths; no generic policy service, extra cache or event bus.
- Consumers: propagate the preference through actual controller/runtime settings and enforce shared profile-read/recall policy at the existing shared entry points, not individually at every transport. Search currently maps episodes/facts, so include_profile=true is not evidence that stored profiles were injected.
- EverOS: extend existing process settings and `_write_memory_child_config` in `avibe_memory/process.py`. Explicitly generate `[strategies.trigger_profile_clustering] enabled=<flag>` and `[strategies.extract_user_profile] enabled=<flag>`; retain all other settings. Update the managed configuration via the existing supervisor/reconcile path, and regenerate from persisted preference on startup. Do not edit EverOS/EverAlgo source or change pins/manifests.
- Reuse existing reload/reconciliation mechanism. Validate pinned EverOS1.2.3 OME reload behavior before claiming immediate effect. If the existing path needs a Memory-only restart, make that behavior explicit; never introduce a second host restart or a new lifecycle framework just for this toggle. Report a real unsupported boundary to PM before expanding scope.

## Verified prompt refresh seams

`core/system_prompt_injection.py:_context_block` selects registry block memory-context-prompt; `build_system_prompt_blocks` composes it (194-274 at base).
OpenCode `modules/agents/opencode/agent.py:1564` and Codex `modules/agents/codex/agent.py:2605` build per-request injection.
Claude `core/handlers/session_handler.py:1741-1787` rebuilds the system prompt; 498-507 compares bytes and recreates cached SDK clients when changed.
Thus the next newly constructed request after effective policy reconciliation uses the new guidance. Already-running requests/native transcript history remain intact. OpenCode caller-context refresh is separate from this prompt path.

## Evidence required

Use existing tests and synthetic fixtures to cover legacy default/false round-trip, settings save/read/restart persistence, OFF/ON tab filtering and fallback, preserved ordinary prompt content with profile guidance absent when off and restored when on, all backend callers passing current flag, shared CLI/UI disabled reads, search clamping, generated OME flags and preserved non-profile settings, ordinary capture/search independence. Test actual supported OME reload in hermetic environment if available and accurately report the layer verified. Check toggle UI on mobile/desktop with synthetic data. Run focused tests, changed-file lint, UI build, then normal exact-head Codex/CI PR gates.

## Explicit exclusions

No generalized policy helper unless existing shared code already owns this value; no watcher for tab changes; no auth redesign, migrations, deletion, identity unification, prompt wording redesign, dependency upgrade, or background cancellation framework. No current-host deployment/config changes are authorized by implementation alone.
