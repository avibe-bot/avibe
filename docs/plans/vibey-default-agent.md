# Vibey is the default Agent for new installs

## Owner decision (2026-10-12)

Avibe leads with its own agent. A new install starts new chats with Vibey. An install that already recorded a
default keeps it: upgrades never change a user's choice. When Vibey cannot run, Avibe says what is missing and
where to fix it; it never hands the turn to another Agent.

This amends [`avibe-agent-always-on.md`](avibe-agent-always-on.md), whose "Not the default" rule left this to a
separate decision.

## Background

On master before this change:

- **The first default.** It is written by `VibeAgentStore.ensure_builtin_default_agents`, which runs at controller
  startup, on `GET /api/agents`, on config saves and on backend toggles.
  - When no default is recorded, or the recorded one is missing or disabled, the sync picks
    `_implicit_default(enabled built-in Agents)`. Its rank, `implicit_default_rank`, puts the built-in backend last.
  - A fresh install therefore records `opencode`, whether or not that CLI is installed. At that point no backend has
    a model source either: a fresh config puts every backend in hub mode.
- **Released builds** (v3.1.x, gh-v3.1.4rc*) run the same sync at startup, and it writes a default whenever none is
  recorded and a backend is enabled. So every released install that has started records a default. The one exception
  is an install with no backend enabled, and there today's code already records Vibey, the only enabled Agent.
- **Setup requires a Model Hub source on the hub channel** (owner decision 2026-09-21,
  `setup-three-screen/contracts.md`). Every hub-channel source is eligible for Vibey.
  - Vibey's starting models are the native built-ins' models that a source lists (`ModelHubService._seed_vibey`).
  - A source listing none of them leaves Vibey with no model. Its setup card then offers a one-click pick (D3).
- **Vibey's no-model refusals** are plain text with no next step.
  - `errors.agentModelRequired` renders the backend id `vibey` rather than its display name.
  - IM `/setup` answers "Backend `vibey` does not support setup yet."

## Change contract

### 1. The first default is Vibey (PR-1)

- **Owner.** `ensure_builtin_default_agents` (`core/vibe_agents.py`) distinguishes two cases.
  - **No default recorded** (`state_meta.default_agent_name` absent): record the built-in backend's Agent.
    - It is identified by its built-in markers, never by name, so it is `vibey-2` when a user's own Agent holds
      `vibey`.
    - **Exception:** when `hub_supply_block(<built-in backend>, None)` reports `hub_disabled`, use today's rank.
      That is an instance where Model Hub is switched off, so Vibey can never run there.
  - **Recorded, but the Agent is missing or disabled:** unchanged. The replacement follows `implicit_default_rank`,
    native assistants first.
- **Why replacements stay native.** A replacement happens to an install whose default was a native assistant. On a
  native-only upgrade, Vibey has no source and so no model. Moving that install to Vibey would turn a working fallback
  into a refusal.
- **Unchanged:**
  - every API, schema and config shape
  - `POST /api/agents/default`, `vibe agent default`
  - the per-caller fallback (`resolve_usable_default_agent`)
  - the archive replacement
- **Invariants:**
  - An install with a recorded default keeps it across this release.
  - A store with no recorded default ends the sync on the built-in Agent, unless Model Hub is disabled on the instance.
  - Once Vibey is the default, only an explicit set-default moves it, because it is always enabled.

### 2. A turn Vibey cannot run says what is missing and where to fix it (PR-2)

- **A new supply-block value, `no_source`.**
  - `hub_supply_block` gains `no_source` for the built-in backend: Model Hub is on and the gateway is on, but no
    configured source can supply the backend (`source_eligible_for_backend`).
  - A source that exists but cannot serve the turn is not `no_source`. Examples are a disabled source, an expired
    sign-in, or a source not placed on the backend. The route refusal reports those, and it carries the same next
    step.
  - It is scoped like `hub_disabled`. A native backend keeps its own credentials and is never blocked this way.
  - `hub_supply_block` stays the one owner of why Model Hub leaves a backend no model, so every consumer follows:
    - the turn refusal (`hub_supply_refusal`)
    - backend readiness (`get_backend_connection`)
    - `GET /api/config.agent_supply_blocks`
- **Refusal copy.**
  - `no_source` renders `errors.modelSourceRequired`.
  - `errors.agentModelRequired` renders the backend's display name.
- **The next step.** Every Model Hub refusal of a turn appends one next step to Models, built by one helper in
  `modules/agents/model_hub.py`. This covers supply blocks, a missing model and route refusals.
  - **Web Workbench:** a link to `/settings/models`.
  - **IM:** `workbench_url('/settings/models')` when Avibe Cloud is paired; otherwise no URL, because the copy
    already names Models.
  - **Exception:** `hub_disabled` gets no link, because the Models page is unavailable there.
- **IM `/setup`.** When it resolves to the built-in backend, it answers with Model Hub guidance and the same next
  step instead of "does not support setup".
- **UI.** `no_source` is rendered in the slots that already render `gateway_off` and `hub_disabled`:
  - the setup card note
  - the Settings → Backends list badge
  - the backend page notice and its Models link
- **No fallback.** A turn sent to an Agent that cannot run is refused. It is never re-routed: the first turn pins its
  Agent to the Session, so a fallback would silently become permanent.

### 3. Setup lets the person choose who starts new chats (PR-3, after the design is approved)

The design is frame F1, "Assistants · who starts new chats", in `avibe-docs/design_desktop.pen` (EN/ZH, four
states). PR-3 implements the approved frame and amends `setup-three-screen/contracts.md` §C4.

- **Options.** Screen 3 marks exactly one ready assistant as the one new chats start with. The ready assistants are
  listed with Vibey first. A saved custom default that can run is listed too.
- **Which one is marked.** The mark is derived, in order, until the person changes it:
  1. the person's pick
  2. the saved default, if it is ready
  3. Vibey, if it is ready
  4. the first ready CLI assistant
- **On completion.** The default is written only when the mark differs from the saved default, through the existing
  completion write and its uncached confirmation.

### Copy

| Key | en | zh |
|---|---|---|
| `errors.modelSourceRequired` (server, new) | {backend} has no model source yet. Add an API key or a subscription in Models. | {backend} 还没有模型来源。请在「模型」页添加 API Key 或订阅。 |
| `errors.openModels` (server, new) | Open Models: {url} | 打开「模型」页：{url} |
| `command.setup.builtinBackend` (server, new) | {backend} gets its models from Model Hub and needs no sign-in. Add or manage model sources in Models. | {backend} 通过模型网关获取模型，无需单独登录。请在「模型」页添加或管理模型来源。 |
| `onboarding.setup.noteNoSource` (new) | No model source supplies {{name}} yet. | 还没有模型来源供给 {{name}}。 |
| `settings.backends.noModelSource` (new) | No model source | 无模型来源 |
| `settings.backends.noModelSourceNotice` (new) | {{name}} gets its models from Model Hub. Add a source in Models. | {{name}} 通过模型网关获取模型，请在「模型」页添加来源。 |

PR-3 owns the setup copy that goes with F1: the screen 2 title and subtitle, the screen 3 subtitle, and the
choice's labels.

## Validation

- **PR-1, at the store boundary.**
  - An empty store records the built-in Agent.
  - A store with a recorded default keeps it, enabled or not; one that is missing or disabled is replaced by the
    unchanged rank.
  - A user Agent named `vibey` leads to `vibey-2`.
  - Model Hub disabled records a native Agent.
- **PR-2:**
  - a table test of `hub_supply_block`
  - one dispatch-level refusal test with its next step, on Web and IM
  - IM `/setup` on the built-in backend
- **Hermetic upgrade runs**, as in the 3.2.0 merge readiness check:
  - fresh install → `vibey`
  - v3.1.3 (custom default) → unchanged
  - gh-v3.1.4rc4 (default `claude`, a user Agent `vibey`) → unchanged, with the built-in at `vibey-2`
  - gh-v3.2.0rc1 → unchanged

## Rollout

- These ship in 3.2.0rc2 and merge after the gh-v3.2.0rc1 tag.
- PR-1 and PR-2 are each safe alone.
  - With PR-1 only, setup completion already keeps a runnable Vibey, and picks the first runnable CLI assistant
    otherwise.
  - The UI ignores supply-block values it does not know.

## Known by design

- An upgraded install is never moved to Vibey, even when Vibey is runnable there. Inviting upgraders to switch would
  be a separate product decision.
- A source that lists none of the native recommendations still needs one click on Vibey's setup card (owner decision
  2026-10-12). Setup does not choose a model nobody picked.
- The five replacement orderings (sync, archive, read-time fallback, per-caller fallback, setup) predate this
  change. Giving them one owner is a follow-up.
