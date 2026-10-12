# Vibey is the default Agent for new installs

## Owner decision (2026-10-12)

Avibe leads with its own agent. A new install starts new chats with Vibey. An install that already recorded a
default keeps it while that Agent exists and is enabled: upgrades never change a user's choice. A default that can no
longer be used is replaced exactly as before. When Vibey cannot run, Avibe says what is missing and where to fix
it; it never hands the turn to another Agent.

Setup stays as it is (owner, 15:01): it asks nothing about the default and adds no copy about it. A new install
simply starts with Vibey, and Vibey has a model as soon as setup places a source.

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
  - An install whose recorded default exists and is enabled keeps it across this release. The global default is
    always an enabled Agent, so a recorded default that is missing or disabled is replaced, as before.
  - A store with no recorded default ends the sync on the built-in Agent, unless Model Hub is disabled on the
    instance. In that case the rank applies, and it still records the built-in Agent when no native backend is
    enabled, because that is the only enabled candidate.
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
- **The next step.** Every Model Hub refusal whose fix is in Models ends with one next step, built by one helper in
  `modules/agents/model_hub.py`. This covers:
  - the supply blocks other than `hub_disabled`;
  - the built-in backend's missing model;
  - route refusals whose copy already sends the reader to Models.
- **On the Workbench,** the requester is known, so the step depends on them:
  - a caller with `can_manage_instance` (the gate on `/settings/models`) gets a link to `/settings/models`;
  - anyone else gets `errors.askAdminForModels`, never a link they would be bounced from.
- **In IM,** no identity maps an IM user to a Workbench principal. So the step is the same for everyone and makes no
  claim about the requester's access:
  - With Avibe Cloud paired, `errors.modelsNextStepImUrl` offers `workbench_url('/settings/models')` as information.
  - Without it, `errors.modelsNextStepIm` names where the fix is.
- **Exception:** `hub_disabled` gets no step, because the Models page is unavailable there.
- **IM `/setup`.** Two forms reach the built-in backend:
  - an explicit `/setup vibey`, which today is refused by the backend-hint gate in `handle_setup_command`;
  - a bare `/setup` that resolves to the built-in backend (`start_setup`).
  Both answer with Model Hub guidance and the same next step instead of "does not support setup". With Model Hub
  disabled on the instance, both answer with `errors.modelHubDisabled` instead, because Models is not there to send
  anyone to. `docs/COMMANDS.md` and `docs/COMMANDS_ZH.md` document both answers.
- **UI.** `no_source` is rendered in the slots that already render `gateway_off` and `hub_disabled`:
  - the setup card note
  - the Settings → Backends list badge
  - the backend page notice and its Models link
- **No fallback.** A turn sent to an Agent that cannot run is refused. It is never re-routed: the first turn pins its
  Agent to the Session, so a fallback would silently become permanent.

### 3. Vibey has a model as soon as a source is placed (PR-4)

- **Owner.** `ModelHubService._seed_vibey` gives Vibey its starting supply once, when the first source eligible for it
  exists: on a new install, the source setup places; on an upgrade, the existing sources at the first start.
- **Today,** the starting models are the native built-ins' model selections that a placed source lists. When none
  qualifies, the list stays empty, and Vibey has no model until someone picks one on its setup card.
- **A native selection** still qualifies first, unless models.dev explicitly describes it as taking no tool calls or
  producing no text. A selection models.dev does not know still qualifies, because it is a person's own choice for a
  native Agent.
- **New: when no selection qualifies, the seed starts Vibey on one model the placed sources list.** The rule is
  deterministic:
  1. Take the first model, in source placement order and then each source's own inventory order, that is not
     retired and that models.dev describes as accepting tool calls and producing text. The description uses the
     exact match the picker's candidates already use.
  2. If models.dev describes no listed model that way, choose none. The setup card's pick and §2's refusal remain.
- **How the model is added.** It joins Vibey's list as the picker adds a provider model.
  `_reconcile_vibey_agent_model` then gives the built-in Agent that model, as it already does for the first model in
  the list.
- **Later changes.** The person can change the model on the card or in Models. A later source never replaces it.

### Copy

| Key | en | zh |
|---|---|---|
| `errors.modelSourceRequired` (server, new) | {backend} has no model source yet. Add an API key or a subscription in Models. | {backend} 还没有模型来源。请在「模型」页添加 API Key 或订阅。 |
| `errors.openModelsLink` (server, new; Workbench) | [Open Models]({url}) | [打开「模型」页]({url}) |
| `errors.askAdminForModels` (server, new; Workbench) | Ask an Avibe admin to fix this in Models. | 请联系 Avibe 管理员在「模型」页处理。 |
| `errors.modelsNextStepImUrl` (server, new; IM) | Someone who manages this Avibe can fix it in Models: {url} | 管理这个 Avibe 的人可以在「模型」页处理：{url} |
| `errors.modelsNextStepIm` (server, new; IM) | Someone who manages this Avibe can fix it in Models, in the Avibe Workbench. | 管理这个 Avibe 的人可以在 Avibe 工作台的「模型」页处理。 |
| `command.setup.builtinBackend` (server, new) | {backend} gets its models from Model Hub and needs no sign-in. Add or manage model sources in Models. | {backend} 通过模型网关获取模型，无需单独登录。请在「模型」页添加或管理模型来源。 |
| `onboarding.setup.noteNoSource` (new) | No model source supplies {{name}} yet. | 还没有模型来源供给 {{name}}。 |
| `settings.backends.noModelSource` (new) | No model source | 无模型来源 |
| `settings.backends.noModelSourceNotice` (new) | {{name}} gets its models from Model Hub. Add a source in Models. | {{name}} 通过模型网关获取模型，请在「模型」页添加来源。 |

## Validation

Any PR here that changes the outcome of a setup flow also carries a `tests/scenarios/auth_setup/catalog.yaml` entry
and a closed-loop case in `tests/scenarios/auth_setup/test_auth_setup_scenarios.py`, per the repository rule.

- **PR-1, at the store boundary.**
  - An empty store records the built-in Agent.
  - A store whose recorded default exists and is enabled keeps it, whatever its backend. One that is missing or
    disabled is replaced by the unchanged rank.
  - A user Agent named `vibey` leads to `vibey-2`.
  - Model Hub disabled:
    - with a native backend enabled, records that native Agent;
    - with none enabled, records the built-in Agent, the only enabled candidate.
- **PR-1, user docs.** `docs/INSTALL_FOR_AI.md` and `docs/INSTALL_FOR_AI_ZH.md` now say that a new install starts
  with Vibey once setup adds a Model Hub source, and that the CLI assistants are optional.
- **PR-2:**
  - a table test of `hub_supply_block`
  - one dispatch-level refusal test with its next step
  - a table test of the next step:
    - Workbench: a caller who can open Models, and one who cannot
    - IM: with Avibe Cloud paired, and without
  - IM `/setup vibey`, and a bare `/setup` on a Vibey default, with Model Hub on and with it disabled
  - every UI renderer of `no_source`: the setup card note, the Settings → Backends badge, and the backend page notice
    with its Models link
- **PR-4, at the seed boundary:**
  - a source listing a native selection still starts Vibey on that selection;
  - a source listing none starts Vibey on its first tool-capable text model, skipping models that lead its inventory
    but that models.dev describes as having no tool calls or no text output;
  - the order across two sources is deterministic;
  - with no model models.dev can describe, Vibey starts with none;
  - a native selection that models.dev rules out for an Agent's turn does not start Vibey, while one it does not
    know still does.
- **PR-4, closed loop (auth_setup).** A source with no native selection but a tool-capable model goes through the real
  add-source route. The case checks the whole chain: the seed, the catalog-change announcement,
  `_reconcile_vibey_agent_model`, and Vibey's runnable named route, which is the evidence setup's entry gate admits.
  Completion on the Vibey default then succeeds with no pick.
- **Hermetic upgrade runs**, as in the 3.2.0 merge readiness check:
  - fresh install → `vibey`
  - v3.1.3 (custom default) → unchanged
  - gh-v3.1.4rc4 (default `claude`, a user Agent `vibey`) → unchanged, with the built-in at `vibey-2`
  - gh-v3.2.0rc1 → unchanged

## Rollout

- These ship in 3.2.0rc2 and merge after the gh-v3.2.0rc1 tag.
- PR-1, PR-2 and PR-4 are each safe alone.
  - Setup completion is unchanged: it keeps the recorded default when it can run. On a new install that default is
    Vibey, which can run once PR-4 has given it a starting model.
  - The UI ignores supply-block values it does not know.

## Known by design

- An upgraded install is never moved to Vibey, even when Vibey is runnable there. Inviting upgraders to switch would
  be a separate product decision.
- The starting model comes only from what models.dev can describe. An endpoint whose models it does not know still
  needs one click on Vibey's setup card.
- Setup completion keeps its existing fallback. If Vibey cannot run at completion, which takes a source models.dev
  cannot describe and no pick, the existing rule records the first runnable CLI assistant. Otherwise setup would
  finish on a default that cannot run.
- The five replacement orderings (sync, archive, read-time fallback, per-caller fallback, setup) predate this
  change. Giving them one owner is a follow-up.
