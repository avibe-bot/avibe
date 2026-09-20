# Memory IM attachment five-platform acceptance

> **Safety boundary:** Run manual checks only in the local Incus regression
> environment after the owner authorizes a regression update. Preserve the
> long-lived `master` target and its product state. Never use `--remote`,
> `--reset-config`, or `--reset-all`.

## Evidence boundary

The native Processing Record is scoped to the authenticated principal and one
selected Memory project. It is best-effort and may omit retained data. It does
not expose a Provider Call Log, raw provider requests/responses, calls without a
Memory entry, or installation-wide records from other principals.

Consequently, the old cross-principal UI procedure is no longer an end-to-end
five-platform acceptance test. Absence from Processing Record is inconclusive,
not proof that a provider call did or did not occur. The automated scenarios
below are the authoritative checks for attachment admission and provider
boundaries.

## Manual smoke check

Use this only for a bound direct-message identity whose Processing Record is
visible to the current Web user.

1. Verify **Memory > Processing Record > Engine status** is healthy.
2. Select the project used by the conversation.
3. Send a uniquely tagged caption with a supported image.
4. Refresh until the authorized native message appears. When retained, inspect
   its directly linked native runs, Episodes, and Atomic Facts.
5. Record `PASS` only for facts positively visible in that scoped record. Record
   `INCONCLUSIVE` when required native evidence is unavailable or absent.

This smoke check does not prove that a rejected attachment avoided the provider,
and it cannot turn missing best-effort evidence into a failure.

## Authoritative automated coverage

- `MEMORY-IM-ATTACH-001`: Slack accepted-image capture
- `MEMORY-IM-ATTACH-003`: explicit attachment-model opt-in
- `MEMORY-IM-ATTACH-005`: Discord attachment capture
- `MEMORY-IM-ATTACH-006`: Telegram attachment capture
- `MEMORY-IM-ATTACH-007`: Lark attachment capture
- `MEMORY-IM-ATTACH-008`: WeChat attachment capture
- `MEMORY-IM-ATTACH-010`: rejected attachment does not enter the multimodal
  provider path while an eligible caption remains capturable

Keep provider parsing and attachment-shape assertions in focused tests. Do not
add a durable observer, replay ledger, or call recorder to make this manual view
complete; accepted loss is part of the Memory contract.

## Result record

Record the source commit, selected project, test identity, visible native facts,
scenario results, and any `BLOCKED` or `INCONCLUSIVE` reason. Never paste raw
platform payloads, credentials, signed URLs, or unsanitized logs.
