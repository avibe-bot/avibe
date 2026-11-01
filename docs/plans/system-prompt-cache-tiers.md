# System prompt snapshots and update tiers

Status: proposed (2026-11-01). Applies to every Vibey Session; Bots
(`docs/plans/bots.md`) are its heaviest users.

## Background

A provider caches a request by prefix: tool definitions, then the system
prompt, then the conversation. A change in the system prompt invalidates
everything after it, so the whole conversation is processed again at the
write price on the next request.

Vibey rebuilds the system prompt at the start of every Turn
(`modules/agents/vibey/agent.py`, `_avibe_sections`, `_project_instructions`,
`_start_agent`). The prompt is fixed for the rest of the Turn: the loop reads
`Agent.system` once per run (`core/agent_core/agent/loop.py`). Any change in
any source therefore reaches every live Session on its next Turn, and each of
those Sessions pays a cold re-read:

- an Agent created, enabled, renamed, or edited (the Agents roster);
- a Skill installed or removed (the Skills catalog);
- an MCP server connected, or its login expiring (the MCP catalog filters
  packages that need login, `core/mcp_hub/prompt.py`);
- any `AGENTS.md` on the path from the filesystem root to the cwd.

Bots make this worse: their contexts are the longest, and the shared memory
layer (`~/.avibe/bots/AGENTS.md`) is loaded by every Bot.

### Measured on the owner's machine (Vibey Sessions, 2026-10-15 to 2026-11-01)

Read from the response rows' usage (`messages.content_json`), grouped by
Session and ordered by time. "Missed prefix" is the part of the previous
request's prompt that the next request did not read from cache.

| Route | Requests | Cache reads | Cache writes / uncached |
| --- | --- | --- | --- |
| `claude-opus-5-5` (official Anthropic) | 20,349 in 72 Sessions | 7.09 B | 556 M written |
| OpenAI (`gpt-6-astra`, `gpt-6.1-sol`) | 36,784 | 5.17 B | 374 M uncached |

Where the Claude writes came from:

| Cause | Events | Missed prefix | Share of writes |
| --- | --- | --- | --- |
| Gap of 5 to 60 minutes (the 5-minute conversation cache expired) | 706 | 306 M | 55% |
| Gap over 60 minutes | 155 | 71 M | 13% |
| Prefix broken while warm, at a Turn start (where system prompt changes apply) | 52 | 22 M | 4% |
| Prefix broken while warm, inside a Turn (not the system prompt; see Open items) | 284 | 118 M | 21% |

Two conclusions:

1. System prompt changes are a small cost today (at most 4% of Claude writes),
   but they grow with Bots, shared memory, and more installed capabilities.
   The tiers below bound them and make the behavior predictable.
2. The largest lever is the conversation cache lifetime on Claude, not the
   system prompt (see Provider cache lifetimes).

## Goal

- A change reaches a live Session at a time chosen by its tier, not
  unconditionally on the next Turn.
- A new Session always starts from the current sources.
- The model is told at once when something it was given has changed, so a
  deferred refresh never leaves it acting on stale facts unknowingly.
- Forks of one Session share its cached prefix.

Out of scope: Claude Code and Codex, whose CLIs build their own system prompts.

## Design

### Snapshot

Each Vibey Session keeps a snapshot of what it last sent in the cached
prefix: the rendered system prompt as its components (each prompt block by
module id, each instruction file by path) and the tool definitions.

- It is stored per Session in the agent-core state directory, beside the
  Session's checkpoints, so a service restart keeps using it: the provider's
  cache survives an Avibe restart.
- It is written only when it is applied, and records when each pending change
  was first seen (`pending_since`).
- A fork copies its source's snapshot byte for byte, so its first request
  hits the source's cache. This and moving the Session id out of the system
  prompt (`agent-core-contracts/fork.md` §3) are both needed for warm forks.

### When a snapshot is replaced

Only at a Turn start, the one point where the system prompt can change.
Avibe renders the candidate, compares it with the snapshot component by
component, and classifies each changed component by the table below.

Replace the whole snapshot, all pending changes together, when any of these
holds:

1. a changed component is tier 1;
2. **a free moment**: the request would be cold anyway:
   - the route's conversation cache has expired (the existing
     `_cache_cold` clock, by the route's lifetime);
   - the model or route changed;
   - a checkpoint ran since the snapshot was applied (the context was just
     condensed, so the re-read is small);
3. a tier-2 change has been pending for at least `N` minutes.

Otherwise keep the snapshot and tell the model what is pending (below).

`N` is 30 minutes. With Claude's conversation cache at one hour (proposed
below), a Session in constant use never goes cold; `N` bounds how long a
tier-2 change can wait there. At most one forced refresh happens per Session
per `N` minutes, and only while a tier-2 change is pending; changes that
arrive together are applied together.

Possible later optimization: let the loop switch to the candidate right after
a checkpoint inside a Turn (the checkpoint request keeps the old prompt for
its warm prefix; the next request is cold anyway), which makes that moment
free instead of cheap.

### Tiers

| Tier | Meaning | Reserved for |
| --- | --- | --- |
| 1 Immediate | Replace at the next Turn start | Changes that reading on demand cannot cover: who the Agent is, and Avibe's own contract after an upgrade; an explicit refresh |
| 2 Bounded | Replace at the next free moment, or after `N` minutes | Instructions and capabilities someone else changed for this Session |
| 3 Natural | Replace at the next free moment only | The Session's own writing, and environment details |

The pending notice makes tiers 2 and 3 semantically immediate: the model
learns of the change on the next input and can read the current version. A
tier decides only when the system prompt itself is replaced.

### Component table

| Component | Source | Changed by | Tier |
| --- | --- | --- | --- |
| Agent identity and definition: preamble, the Agent's system prompt, a Bot's display name and role | Agent record | the user | 1 |
| Avibe prompt modules (capabilities, working principles, Show Pages, Harness, Vault routing, quick replies, session title) and tool definitions | Avibe code | an Avibe upgrade | 1, only when the rendered bytes changed |
| Explicit refresh | the user (a refresh action, or a CLI command) | the user | 1 |
| Global instructions `~/.avibe/AGENTS.md` | file | the user | 2 |
| Project `AGENTS.md` chain in a project Session | files | the user, the team, `git pull` or a branch switch | 2 |
| Shared Bot memory `~/.avibe/bots/AGENTS.md` | file | the memory Bot | 2 |
| Agents and Bots roster | database | the user (create, enable, rename, set as Bot) | 2 |
| Skills catalog | Skill directories | the user (install, remove, enable) | 2 |
| MCP catalog | MCP Hub | the user (connect, remove), or a login expiring | 2 |
| Hosted tool declarations | Model Hub service configuration | the user (enable a search service) | 2 |
| Platform switches (quick replies, session title section) | configuration | the user | 2 |
| A Bot's own `AGENTS.md` | file | the Bot itself; others' edits get the notice | 3 |
| Tool list details (ripgrep found on `PATH`) | environment | the runtime | 3 |
| Session id, fork note | Session | fixed per Session (to move out of the system prompt) | not applicable |

Not governed by tiers, because the request is cold anyway: changing the
Session's model or route, or switching its Agent. These are free moments.

A Project's Agent binding affects only new Sessions; it never changes an
existing Session's system prompt.

### Pending notice

While the snapshot differs from the candidate, the environment block
(`agent-core-contracts/tools.md` §8, `modules/agents/vibey/prompt.py`) carries
one more field, sent only when its value changes, like the other fields:

```text
<environment>
instructions: pending ~/.avibe/AGENTS.md; skills
</environment>
```

- It names changed files by path and catalogs by name (`skills`, `mcp`,
  `agents`), never their contents.
- It omits changes this Session made itself (its own edits are already in the
  conversation).
- When a snapshot is applied the value becomes `current`, sent once.
- The field name is lowercase letters, as the block's parser requires.
- A fixed sentence in the system prompt explains the field once: the listed
  parts changed after the instructions were loaded; read the current version
  (the file itself, `vibe skill list`, `vibe mcp --list`, `vibe agent list`)
  before relying on them.

The same change removes `date` and `timezone` from the environment block:
the `[Now: ...]` line that every input carries
(`core/agent_input.py`, `AgentInputMetadata.render`) already has the date,
time, and UTC offset. The block keeps `cwd`, `os`, `shell`, `watches`, and
`instructions`. Every Vibey input path renders that metadata; the one
exception, a recovered input whose sender facts cannot be rebuilt, loses no
date it would have had, because the block sends `date` only when it changes.

## Provider cache lifetimes (as of 2026-11-01)

Read from each provider's documentation on 2026-11-01. "Write" and "read" are
multiples of the model's uncached input price.

| Provider | How caching works | Lifetime | Write | Read |
| --- | --- | --- | --- | --- |
| Anthropic | Explicit `cache_control` breakpoints, at most 4 per request; a longer lifetime must come before a shorter one | 5 minutes by default, or 1 hour; refreshed on each use; measured from the start of the request | 1.25x (5 min), 2x (1 h) | 0.1x; 0.05x on Opus 5.5 and Sonnet 5.5; 0.025x on Fable 5.1 and Mythos 5.1 |
| OpenAI, GPT-5.6 and later | Automatic (implicit) or explicit breakpoints | at least 30 minutes by default, or at least 12 hours in the Responses API (`prompt_cache_options.ttl`); a read extends coverage to at least 30 minutes after it | 1.25x (30 min), 2.5x (12 h) | 0.1x; 0.05x on GPT-6.1 Sol and Terra |
| OpenAI, earlier models | Automatic | `in_memory`: about 5 to 10 minutes idle, up to 1 hour; `24h` extended retention (the default for organizations without Zero Data Retention, on supported models) | no write charge | model-dependent |
| Google Gemini API | Implicit on Gemini 2.5 and later, no hit guarantee; explicit caches only before Gemini 4 Argon | implicit: not published; explicit: default 1 hour, storage billed per hour | none (implicit) | 0.1x |
| Google Vertex AI | Implicit, in memory then durable storage | at most 24 hours, most entries evicted earlier | none (implicit) | 0.1x |
| xAI | Automatic, per server; `prompt_cache_key` (Responses) or `x-grok-conv-id` routes a conversation to the same server | not published | none | discounted |
| DeepSeek | Automatic disk cache | cleared when unused, "usually within a few hours to a few days" | none | deeply discounted |
| Moonshot Kimi | Automatic; `prompt_cache_options` chooses the lifetime | 5 minutes by default, or 1 hour; refreshed on hit; a cache entry's lifetime is fixed at its first write | charged (see the official price list) | discounted |
| Alibaba Qwen (Model Studio) | Implicit by default; explicit `cache_control` | implicit: not guaranteed, periodically cleaned; explicit: 5 minutes, refreshed on hit | implicit none; explicit 1.25x | implicit 0.2x; explicit 0.1x (exceptions per model) |
| Zhipu GLM | Implicit, built asynchronously | not published | none | discounted |
| Mistral, Groq, Together, Fireworks | Not verified for this document | unknown | unknown | unknown |
| OpenRouter | Passes through the upstream provider's cache; sticky routing per conversation | the upstream's | the upstream's | the upstream's |

Subscription and relay Sources keep whatever their upstream does; Avibe
cannot observe it.

### What Avibe sends and assumes today

| Route | Sent | Lifetime Avibe assumes (`CACHE_TTLS`) |
| --- | --- | --- |
| Official Anthropic, Claude model | tools and system prompt breakpoints with `ttl: 1h` (since #2484); the last two user-message breakpoints with the default 5 minutes | 300 s |
| Official OpenAI Responses, GPT-5.6 and later | nothing by default (30 minutes); a validated route override may send `prompt_cache_options.ttl` of `30m` or `12h` | 1,800 s |
| Every other route | no cache fields | 3,600 s (errs toward warm) |

No route sends `prompt_cache_key` or a conversation header.

## Recommended changes

1. **Claude conversation breakpoints: 5 minutes to 1 hour.** The system prompt
   is already cached for an hour; the conversation, which is almost all of
   the prefix, is not. Measured above, 55% of all Claude writes were
   re-writes after 5 to 60 minute gaps. At Opus 5.5 rates (read 0.05x),
   moving the conversation to 1 hour turns those 306 M re-written tokens into
   reads and makes the remaining 251 M writes cost 2x instead of 1.25x: the
   input-side cost falls by about 17%. Update `CACHE_TTLS["anthropic"]` to
   3,600 s and the route-override validation that limits official Anthropic
   conversation breakpoints to `5m`.
2. **OpenAI: keep 30 minutes.** Gaps of 30 minutes to 12 hours missed 48 M
   tokens; a 12-hour lifetime would save about 55 M token-equivalents but make
   every other write cost 2.5x instead of 1.25x, roughly 400 M more.
3. **Send a Session-scoped `prompt_cache_key` where it routes:** xAI (and its
   `x-grok-conv-id` header on Chat Completions), OpenAI models before GPT-5.6,
   and OpenRouter's session header. Follow-up, verified per route.
4. **Kimi: consider `prompt_cache_options` of `1h`** for Bot Sessions once its
   write rates are confirmed. Unknown routes keep the 3,600 s assumption: it
   errs toward warm, which only delays tier-3 refreshes, and tier 2 bounds
   them.

## Persisted shape

The snapshot is a new per-Session file in the agent-core state directory. A
missing or unreadable snapshot is treated as absent: the Turn renders fresh
and writes a new one. No database migration.

## Open items

- Claude prefix breaks while warm inside a Turn (284 events, 118 M tokens,
  21% of writes in the sample) are not caused by the system prompt. Likely
  causes are clearing old tool results at 80% of the threshold, checkpoints,
  and Source fallback; investigate separately, since it is a larger leak than
  system prompt changes. OpenAI shows the same pattern (1,338 events).
- OpenAI responses on `gpt-6-astra` report no cache-write tokens; verify
  whether the Source reports them or the meter misses them.
- Whether to make `N` configurable.
