# Prompt Studio

## Purpose

Prompt Studio is a review surface for Avibe-authored runtime instructions. It
lets maintainers read the authoritative English beside a generated Chinese
review draft, annotate or edit that draft, and ask an Agent to prepare an
English candidate. The candidate is never an automatic source write or PR.

## Source model

The Git checkout is authoritative:

- `core/prompt_registry.py` owns stable module identity, ordering, composition
  whitespace, and declared placeholders.
- `core/prompts/*.md` owns the English System Prompt prose.
- `skills/*/SKILL.md` owns built-in Skill prose.
- `vibe debug prompt export --format json` is the only supported extraction
  interface for Prompt Studio. Consumers do not parse Python source.

The exporter reads the current checkout or installed package. It does not fetch
Git or choose a branch. A contributor reviews the branch they have checked out.
Catalog order and revisions are content-derived and deterministic; the export
contains no generation timestamp.

Completeness is defined by the production injection, not by a manually selected
set of files. All authored prose, including Skill invocation/pagination rules,
Show history contracts, and Codex snapshot boundaries, belongs in the registry.
Agent instructions and generated Skill/Agent catalogs have declared input slots;
their machine/session-specific values are not alternative English sources.

The default export is the complete **source catalog**, including mutually
exclusive branches. It is not a rendered session prompt. To render explicit
inputs, use the same command with `--context-file context.json`:

```json
{
  "backend": "codex",
  "agent_instructions": "Review changes carefully.",
  "options": {
    "context": {
      "user_id": "reviewer",
      "channel_id": "review",
      "platform": "avibe",
      "platform_specific": {"agent_session_id": "sesreview"}
    },
    "memory_enabled": true,
    "skills_cwd": "/path/to/project",
    "enabled_agents": [
      {"name": "codex", "backend": "codex", "description": "Code review"}
    ]
  }
}
```

`options` are the keyword inputs of `build_system_prompt_blocks`; omitted
options use that builder's defaults. Image guidance defaults to Codex only.
In JSON, `enabled_agents` is an array of Agent objects or `null`, not an arbitrary
Python iterable. Each object must have a usable `name` or `normalized_name`;
optional fields use the production defaults. An empty array, `null`, or omission
renders no Agent table. Invalid input returns a localized error, not a rendering
with silently discarded Agent rows.
Callers reproducing a turn must supply its effective options (for example,
quick replies disabled on WeChat), Agent instructions, and enabled Agent list;
the command does not infer them from the Session ID. Normal path and Show
history resolution still use the invoking environment. Providing `skills_cwd`
uses production discovery, including ordinary built-in snapshot maintenance.

The additive `rendered` field contains ordered, source-addressable `blocks`,
their exact concatenated `text`, and its content-derived `revision`. This calls
the same block builder used by all three production backends. Codex includes its
snapshot envelope; native backend presets, project files, tool schemas and
conversation history are outside this Avibe-injection scope. The export is a
rendering of supplied inputs, not a claim to have captured a live model request.
It starts no backend and does not grant Memory access or change configuration.

Studio must consume this interface rather than select files or assemble prompt
text itself. Source-document token estimates and rendered-injection token
estimates are different measurements; never present the former as the latter.

## Runtime compatibility

Markdown files omit composition-only leading and trailing newlines. The registry
adds those boundaries when rendering. Moving existing prose into Markdown must
leave the composed System Prompt byte-for-byte unchanged so the migration does
not invalidate backend prompt caches.

Dynamic values remain runtime-owned. The registry declares their placeholders,
while the prompt builder supplies stable Session IDs, paths, and deterministically
ordered Agent tables in their established call path. Required built-in Skill
routing is authored directly and is not conditioned on catalog discovery.

The System Prompt describes current capabilities and their positive behavior.
Disabled, missing, or degraded capabilities are omitted; installation, recovery,
and compatibility guidance is loaded only when the task requires it. Stable
configuration selects complete prompt modules, while turn-scoped authorization
affects runtime access only and never selects prompt content.

## Translation and drafts

Translation is a review aid, not a second source of truth. Server-side cache keys
include the source text, action, translator revision, Agent backend, model, and
reasoning effort. Reopening the Studio on another device reuses the same cached
translation when those inputs match.

Chinese edits are server-side drafts bound to the source revision. Source drift
does not overwrite a draft. Writes use optimistic version checks so two devices
cannot silently replace each other's work.

## Contribution flow

1. Check out the branch to review and open Prompt Studio against that checkout.
2. Read or edit the Chinese draft, or leave anchored Show Page annotations.
3. Ask an Agent to turn the review into an English candidate.
4. Review and refine the candidate in the Studio.
5. Ask the Agent to apply the confirmed patch and deliver it through the normal
   PR review loop.

A review bundle may be exported for handoff, but Git remains the shared source
and the PR remains the publication boundary. Prompt Studio never pushes or opens
a PR by itself.
