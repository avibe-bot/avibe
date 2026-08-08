# Codex Provider Rebinding

## Background

Codex persists the provider id on each native thread. Avibe previously compared
only that id when deciding whether `thread/resume` needed a provider override.
That is insufficient because Avibe deliberately reuses `openai-managed` while
switching its definition between ChatGPT OAuth and API-key/custom-base-URL
configurations. A legacy thread and the current runtime can therefore have the
same provider id but incompatible routing.

## Goal

Resume an existing Codex thread against the current Avibe-managed provider
definition without changing the thread's user-visible Avibe Session identity.
Keep API-key recovery guidance distinct from OAuth recovery guidance.

## Solution

1. Keep the existing provider-id transition rule.
2. When a persisted thread is first resumed in a fresh Codex app-server,
   explicitly rebind Avibe-managed providers even when the provider id matches.
   Settings saves already refresh the Codex runtime, so this makes the new
   provider definition authoritative without adding migration state.
3. For Codex API-key mode, report a key/base-URL recovery action instead of
   offering an OAuth reset.

## Verification

- Focused unit coverage for same-id managed-provider rebinding.
- A closed-loop auth scenario covering OAuth-era native thread resume after an
  API-key/custom-base-URL switch.
- Existing provider-transition, model-preservation, and auth recovery tests.
