# Multi-Platform IM Support Plan

## Background

Vibe Remote currently treats IM platform selection as a single-choice global mode.
This assumption is baked into:

- `config.V2Config.platform`
- `core/controller.py` creating exactly one IM client
- `modules/im/factory.py` returning exactly one bot instance
- the setup wizard using a single `platform` choice and a single platform-specific config step
- dashboard/settings UI reading and writing message-handling controls against one active platform

The desired product outcome is to let one Vibe Remote instance serve multiple IM platforms at the same time, so users can enable Slack + Discord + Lark + WeChat in one deployment and receive/send messages on all enabled platforms concurrently.

## Goals

1. Allow users to select multiple IM platforms during onboarding and in Web UI settings.
2. Start and maintain multiple IM adapters concurrently in one process.
3. Preserve a single shared agent/session/routing core unless platform-specific behavior is required.
4. Keep per-platform credentials, lifecycle, and health isolated so one failed platform does not break others.
5. Maintain backward compatibility with existing single-platform config as much as possible.

## Non-goals (phase 1)

- No cross-platform shared channel abstraction beyond existing per-platform settings keys.
- No unified "send one reply to all linked platforms" behavior.
- No merging of different platform identities into one user account model.
- No major redesign of agent routing semantics beyond making them platform-aware in a multi-platform runtime.

## Product Plan

### 1. Onboarding flow

Current behavior:

- Platform step is single select.
- Wizard continues into exactly one platform config step.
- Channel step assumes exactly one active platform.

Planned behavior:

- Platform step becomes multi-select with check cards.
- Wizard dynamically inserts one config step per selected platform.
- Step order becomes:
  - Welcome
  - Mode
  - Agents
  - Platforms (multi-select)
  - Slack config (if selected)
  - Discord config (if selected)
  - Lark config (if selected)
  - WeChat config (if selected)
  - Channel config (only for platforms that support channel discovery/config)
  - Summary

UX details:

- First selected platform can still be used as the default "primary platform" for UI ordering and backward compatibility.
- In phase 1, `primary` is an internal compatibility concept and should not be exposed in the UI as a user-facing choice.
- WeChat should keep its current special onboarding behavior:
  - auto-generate QR
  - skip channel configuration
  - default `show_duration=false`
- Summary page should show all enabled platforms and their readiness, not just one.

Wizard state rules:

- each selected platform config step has explicit state: `not_started`, `in_progress`, `completed`
- Back from a platform config step returns to the previous selected platform config step; eventually back to platform selection
- unselecting an already configured platform keeps its credential block in config, but marks it disabled
- if a user selects a platform in phase 1, they must complete its minimum required auth/config before continuing

Minimum completion rule per platform:

- Slack: valid bot token + app token (or SaaS OAuth completion)
- Discord: valid bot token
- Lark: valid app id + app secret
- WeChat: QR login confirmed and bot token issued

### 2. Web UI interactions

Dashboard / settings changes:

- Replace single `platform` display with `enabled platforms` list.
- Separate global settings from per-platform settings:
  - global: default Agent, ack mode, duration display, reply enhancements, language
  - per-platform: credentials, require mention, allowlists, OAuth / QR login state
- Add per-platform cards or tabs:
  - Slack settings
  - Discord settings
  - Lark settings
  - WeChat settings

Dashboard information architecture:

- top summary shows enabled platform count rather than a single platform label
- each enabled platform gets its own card with status:
  - connected
  - auth required
  - error
- cards link into platform-specific configuration views for recovery

Channel settings:

- Channel management must become platform-scoped.
- UI should clearly separate:
  - Slack channels
  - Discord guild/channels
  - Lark chats
- WeChat: no channel page

Recommended phase-1 UX:

- Wizard channel configuration is shown as one step per channel-capable platform
- Dashboard channel management uses platform tabs rather than mixing all channels in one list

Operational feedback:

- UI should show per-platform status:
  - enabled / disabled
  - connected / auth required
  - last error (if available)

### 3. Command / welcome behavior

- `/start` remains platform-local.
- Non-interactive welcome content should render correctly on every enabled platform.
- Interactive platforms still show platform-appropriate buttons.
- If future product wants platform-specific menus, keep message generation platform-aware but share one command handler.

### 4. Identity and binding rules

- Bind code remains global, but bound identities remain platform-scoped
- User identity is always modeled as `platform::user_id`
- No cross-platform user merge in phase 1
- Admin status remains effectively per-platform because settings keys remain platform-scoped

This rule should be reflected in both code and user documentation to avoid ambiguity.

## Technical Plan

### 1. Config model evolution

Current shape:

```json
{
  "platform": "wechat"
}
```

Proposed phase-1 shape:

```json
{
  "platform": "slack",
  "platforms": {
    "enabled": ["slack", "discord", "wechat"],
    "primary": "slack"
  }
}
```

Rules:

- Keep `platform` for backward compatibility and as `primary` alias in transition.
- New source of truth becomes:
  - `platforms.enabled`
  - `platforms.primary`
- `platforms.primary` is used only for compatibility, ordering, and legacy fallbacks in phase 1
- Migration rules:
  - if only legacy `platform` exists, transform to `enabled=[platform]`, `primary=platform`
  - if `platforms` exists, keep `platform = primary` during serialization for compatibility with old code paths

Validation changes:

- validate each enabled platform's config independently
- require credentials only for enabled platforms
- allow disabled platform blocks to remain partially filled

### 2. Controller architecture

Current architecture assumes one IM client:

- `self.im_client`
- one formatter
- one `SettingsManager(platform=...)`
- one startup/shutdown lifecycle

Proposed architecture:

- introduce an IM runtime registry, e.g. `self.im_clients: dict[str, BaseIMClient]`
- keep `self.im_client` as an alias to the primary platform during transition if needed
- add a small platform manager abstraction:

```python
class IMRuntimeManager:
    clients: dict[str, BaseIMClient]
    async def start_all()
    async def stop_all()
    def get(platform: str) -> BaseIMClient
```

Benefits:

- isolates startup/shutdown per platform
- easier health reporting
- lets one platform fail without bringing down the whole controller

### 3. IM factory and startup lifecycle

Current factory returns a single client.

Proposed:

- keep `create_client(config)` for compatibility
- add `create_clients(config) -> dict[str, BaseIMClient]`

Runtime behavior:

- initialize all enabled clients
- inject shared dependencies (`settings_manager`, `controller`) into each client if supported
- start receive loops concurrently with `asyncio.gather(..., return_exceptions=True)`
- if one client exits unexpectedly:
  - log platform-specific error
  - keep other clients alive
  - surface degraded status to UI

### 4. Settings and routing model

Current code already uses per-context settings keys, but runtime construction is single-platform aware.

Needed changes:

- make `SettingsManager` and `AgentRouter` platform-aware for multiple active platforms at once
- ensure settings keys remain namespaced by platform, for example:
  - `slack:C123`
  - `discord:channel_id`
  - `lark:chat_id`
  - `wechat:user_id`
- all message handling, auth, and routing decisions must derive platform from context instead of global config

This is the most important architectural change.

Capability model requirement:

- shared handlers must stop assuming all global settings are supported equally on all platforms
- phase 1 keeps these settings global: `ack_mode`, `show_duration`, `reply_enhancements`, `language`
- runtime must apply per-platform capability fallback, for example:
  - WeChat forces `typing` when `ack_mode=reaction`
  - WeChat omits quick replies when `reply_enhancements=true`
  - channel-only settings are ignored for WeChat

Add a capability matrix during implementation and keep the fallback behavior explicit.

### 5. Message ingress / egress path

Current path relies heavily on `self.config.platform` checks in shared handlers.

Required refactor direction:

- extend `MessageContext` so platform is always explicit and trusted
- replace global checks like `if self.config.platform == "wechat"` with `if context.platform == "wechat"` in shared flows
- make formatter lookup per outgoing platform instead of one controller-global formatter
- make ack mode, typing, quick replies, and enhancements resolve per message's platform

Without this refactor, mixed-platform runtime will leak one platform's behavior into another.

### 6. Process / concurrency / reliability

Multi-platform means more background tasks in one service process.

Need to account for:

- one poll/socket loop per enabled platform
- per-platform reconnection backoff
- per-platform auth/session expiry handling
- startup idempotency so reloads do not duplicate clients
- cleanup that stops all platform tasks cleanly

Recommended rules:

- every platform adapter exposes explicit `run()` / `shutdown()` semantics
- controller owns the task group
- runtime logs always include platform prefix
- one platform crashing should not cancel sibling platform tasks automatically

### 7. Web API changes

The setup server and config API currently expect one selected platform.

Needed changes:

- `getConfig` / `saveConfig` should accept `platforms.enabled` and `platforms.primary`
- QR login endpoints stay WeChat-specific but should coexist with other enabled platforms
- UI payloads should stop overwriting unrelated platform config blocks when saving one platform step
- config save semantics should move toward merge / patch behavior rather than rebuilding unrelated platform blocks from defaults

### 8. Backward compatibility strategy

Phase-1 compatibility approach:

- preserve existing `platform` field
- read both old and new config formats
- serialize both during transition
- keep all old single-platform behavior valid if only one platform is enabled

This reduces migration risk and lets the feature roll out incrementally.

## Suggested Implementation Phases

### Phase 0 - Planning / compatibility groundwork

- add config schema support for `platforms.enabled` + `platforms.primary`
- add migration helpers
- introduce platform-aware context helpers without changing runtime behavior yet

### Phase 1 - Multi-platform runtime backbone

- add `IMRuntimeManager`
- create/start/stop multiple clients concurrently
- make controller and handlers resolve platform from context
- keep UI still single-select if needed while backend already supports multiple enabled platforms

### Phase 2 - Wizard and settings UI

- convert platform selection to multi-select
- add dynamic per-platform steps
- split channel/settings UI by platform
- add per-platform status cards
- define and implement explicit step-state transitions for back, unselect, and partial completion

### Phase 3 - Product polish

- per-platform health indicators
- better onboarding copy
- clearer restart/auth-required messaging
- optional platform ordering and default behavior refinements

## Key Risks

1. **Global `config.platform` assumptions are widespread**
   - shared handlers, dashboard, settings, routing, and command UX all use it
   - this is the largest refactor surface

2. **Formatter / feature bleed across platforms**
   - quick replies, typing, button support, and markdown rules differ by platform
   - must resolve capability per message, not globally

3. **Settings collisions**
   - if settings keys are not platform-namespaced consistently, one platform may overwrite another's routing/user settings

4. **Lifecycle complexity**
   - one process managing multiple long-lived IM connections needs careful shutdown and restart behavior

5. **Wizard persistence bugs**
   - current setup flow tends to rewrite large config payloads; this can accidentally erase non-current platform state if not refactored

6. **Product ambiguity around partial completion**
   - if a selected platform is left unauthenticated, users need a clear visible state and recovery action
   - this must be defined before implementation to avoid inconsistent UX across wizard and dashboard

## Recommended Initial Delivery Scope

For the first implementation, target this smaller but complete slice:

- enable exactly two or more platforms concurrently in config/runtime
- support Slack + Discord + Lark + WeChat all through one controller process
- update wizard to multi-select and show one config step per chosen platform
- update summary/dashboard to show all enabled platforms
- keep channel config only for platforms that support it
- add per-platform status cards with at least `connected / auth required / error`
- postpone deeper product features like per-platform live health dashboards if needed

## Open Questions

1. Should one platform be designated as the "primary" platform purely for UI display, or should the UI become fully platform-neutral?
2. Should `require_mention`, `ack_mode`, and `show_duration` stay global, or should some of them become per-platform?
3. Should onboarding require all selected platforms to finish auth before completion, or allow partial completion with some platforms pending?
4. Should platform enable/disable changes trigger selective restarts, or a full controller restart in phase 1?

## Product Acceptance Checklist

- Wizard supports selecting 1..N platforms and preserving per-platform in-progress data
- Saving one platform's credentials does not clear another platform's credentials
- Slack + Discord can be enabled together and both receive replies in one process
- One platform auth failure does not break other enabled platforms
- Dashboard shows all enabled platforms with at least `connected / auth required / error`
- WeChat remains special-cased correctly: QR auth, no channel setup, no quick replies, duration off by default
- Single-platform installs remain behavior-compatible with current versions

## Recommended Answer for Phase 1

- keep `primary` for compatibility/UI ordering
- keep `ack_mode`, `show_duration`, `reply_enhancements`, `language` global in phase 1
- keep `require_mention` per platform
- allow partial completion, but clearly show which platforms are not ready
- use full controller restart first, optimize to selective restart later
