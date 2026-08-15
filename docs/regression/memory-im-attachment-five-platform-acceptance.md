# Memory IM attachment five-platform acceptance

> **Safety boundary:** Run this checklist only in the local Incus regression
> environment after the owner explicitly authorizes a regression update. Keep the
> long-lived `master` target online and preserve its existing product state. Never
> use `--remote`, `--reset-config`, or `--reset-all`. After any authorized source
> update, verify service health before reporting an acceptance result.

This runbook validates the first Memory attachment release across Slack, Discord,
Telegram, Lark, and WeChat. It is deliberately short: prepare five fixture pairs,
send ten direct messages, wait for one shared idle-flush window, and inspect the
ten outcomes. The Agent's reply is not evidence of Memory capture; Memory runs
independently of Agent delivery.

This owner-facing checklist uses only evidence visible in the product UI. It does
not require container access, database queries, or log inspection. A property
that needs engineering tools belongs in an automated contract test or a product
observability issue, not in this checklist.

## Preconditions

Do not start the 15-minute clock until all of these are true:

1. The authorized local Incus `master` target is healthy and still has its prior
   product state. This checklist requires a separately provisioned five-platform
   target: the standard regression seed covers Slack, Discord, Lark, and WeChat
   but does not provision Telegram credentials. If Telegram is not already
   configured and bound, record the run as `BLOCKED` and stop. Do not add or
   change credentials as part of this checklist.
2. Memory is enabled. In the Web UI, open **Memory > Processing Record** and verify
   **Engine status** is **Healthy**.
3. In **Memory > Processing Record**, verify **Call log** is **Recording
   normally**. Open at least one recent terminal entry and confirm that **Model
   calls** expands to a Request/Response payload. If the UI says **Model calls
   weren't logged or have expired**, stop and record the run as `INCONCLUSIVE`.
   Call recording is runtime state rather than a permanent capability:
   `core/memory/sidecar_lifecycle.py` may disable `records_calls`, and
   `MemoryLogPanel.tsx` renders the unavailable/expired state.
4. In **Memory > Settings**, the **IM attachment processing model (optional)** has
   a complete base URL, model, and API key. This is the explicit opt-in required by
   `MEMORY-IM-ATTACH-003`.
5. The human test account on each platform is enabled and bound to this Avibe
   installation. Use a one-to-one direct message only. Group, channel, unbound,
   forwarded, edited, quoted, webhook, and system traffic stays outside this
   human baseline and is closed where the platform exposes the required facts.
   WeChat bot/self/system source exclusion remains unverified and is collected
   below; this checklist does not claim those sources are currently excluded.
6. Record a run tag such as `IMA-20260816-0508`. Put it in every supported
   caption and in every fixture's pixel/file marker. Lark and WeChat native
   image/file messages are verified by bound user plus send timestamp rather
   than by assuming those clients support attachment captions.

### Test fixtures

Prepare one small fixture pair per platform: five PNGs and five SVGs, ten files
total. Do not reuse a file across platforms, and do not use personal or
production data.

- **Accepted image:** a 512 x 512 PNG under 1 MiB. Put a large, high-contrast
  platform-specific marker in the pixels, for example `IMA-DISCORD-A7Q2`. The
  caption must contain the run tag but must **not** contain the pixel marker. This
  separates image understanding from text-only capture.
- **Rejected file:** an SVG under 100 KiB with a different visible marker, for
  example `IMA-DISCORD-REJECT-R9M4`. SVG is intentionally excluded from the
  pinned Memory modality policy. Where the client supports a caption on that
  attachment message, use a non-empty caption containing the run tag but not the
  SVG marker. The table explicitly identifies the file-only cases.

Use different markers for every platform. Keep a local table mapping each pair
and marker to its platform; do not put that table in chat.

## 15-minute acceptance flow

Send the ten fixtures first, then wait once. Idle flush is tracked per session,
so waiting after the final send covers every platform without mutating any
session-owned state.

1. In **Memory > Processing Record**, record the newest entry timestamp for each
   bound platform user. This is the per-user high-water mark for the run.
2. Within about two minutes, send each platform's accepted PNG and rejected SVG
   as described in the table. The order within a platform does not affect the
   result; finish all ten sends before waiting.
3. Do not send another message in any tested session for at least **5 minutes 30
   seconds after the final fixture**. The wait derives from
   `core/memory/coordinator.py`'s `IDLE_FLUSH_TIMEOUT = 5 minutes`, with 30 seconds
   of scheduling margin. `MAX_UNFLUSHED_AGE = 30 minutes` and
   `MAX_UNFLUSHED_MESSAGES = 100` are fallback bounds, not the expected trigger
   for this two-message-per-platform run.
4. Refresh **Processing Record** and inspect all ten outcomes. Recheck that **Call
   log** is **Recording normally** before interpreting accepted-image Model calls.
   Allow the remaining clock time for terminal entries to appear.

This flow deliberately does **not** use `/new`. That command tears down the Agent
session and pauses session-bound tasks and watches
(`core/handlers/command_handlers.py`), which conflicts with the long-lived
`master` environment's state-preservation boundary. The shared idle window avoids
those mutations and keeps the expected wall time near 15 minutes: roughly two
minutes to send, 5.5 minutes idle, and the remaining time to inspect and record
results.

The processing log is the primary evidence because it covers every user and
project on the installation; **Memory > Search** is scoped to the current
principal and is not a substitute for cross-platform verification. For an
accepted image, open the matching processing entry, expand **Model calls**, and
inspect the attributed multimodal model call's **Request** and **Response**. The
request must contain the image input and the response must describe or reproduce
the pixel-only marker. The timeline and Memory snippet preview do not expose
attachment-derived model output and therefore are not positive evidence.

For a rejected SVG, use only the post-baseline processing entries visible in the
UI. A caption-bearing rejected turn must preserve its caption without showing the
SVG filename as an attachment in the entry preview. The Lark and WeChat file-only
rows must not produce an entry attributable to the rejected filename, marker,
user, and send time. The hermetic `MEMORY-IM-ATTACH-010` scenario separately
proves that a fully rejected attachment does not enter the multimodal provider
path; the manual run does not try to reconstruct that engineering fact from an
unobservable UI absence.

| Platform | Scenario | Send | Pass condition in Memory > Processing Record |
| --- | --- | --- | --- |
| Slack | `MEMORY-IM-ATTACH-001` | In a bound human DM, send the Slack PNG with caption `<run-tag> slack accepted image`. | One post-baseline terminal entry exists for the Slack user. Its **Model calls** contain an attributed multimodal request with the image and a response that describes or reproduces the PNG-only marker. |
| Slack | `MEMORY-IM-ATTACH-010` | In the same DM, send the Slack SVG with caption `<run-tag> slack rejected file`. | A post-baseline entry preserves the caption, and its preview does not show the Slack SVG filename as an attachment. |
| Discord | `MEMORY-IM-ATTACH-005` | In a bound human DM, upload the Discord PNG as an ordinary attachment with caption `<run-tag> discord accepted image`. Do not add a link embed, component, sticker, or forward. | One post-baseline terminal entry exists for the Discord user. Its attributed multimodal request contains the image and its response exposes the PNG-only marker. If the raw message has an automatic embed and no entry is created, fail this row and apply the Discord fixture decision below. |
| Discord | `MEMORY-IM-ATTACH-010`, `MEMORY-IM-ATTACH-005` | Upload the Discord SVG with caption `<run-tag> discord rejected file`. | A post-baseline entry preserves the caption, and its preview does not show the Discord SVG filename as an attachment. |
| Telegram | `MEMORY-IM-ATTACH-006` | In a bound private chat, send the Telegram PNG as one photo message with caption `<run-tag> telegram accepted image`. Do not use an album. | One post-baseline terminal entry exists for the Telegram user. Its attributed multimodal request contains the Telegram image input and its response exposes the PNG-only marker. The request may identify the normalized JPEG photo rather than the original PNG filename/MIME. |
| Telegram | `MEMORY-IM-ATTACH-010`, `MEMORY-IM-ATTACH-006` | Send the Telegram SVG as one document with caption `<run-tag> telegram rejected file`. | A post-baseline entry preserves the caption, and its preview does not show the Telegram SVG filename as an attachment. |
| Lark | `MEMORY-IM-ATTACH-007` | In a bound one-to-one chat, send the Lark PNG through the native **image** action as one image-only message. The pixel marker includes the run tag. | One post-baseline terminal entry appears for the Lark user. Its attributed multimodal request contains the image input and its response exposes the PNG-only marker. |
| Lark | `MEMORY-IM-ATTACH-010`, `MEMORY-IM-ATTACH-007` | Send the Lark SVG through the native **file** action as one file-only message. The filename includes the run tag; its visible SVG marker is different from the filename. | No post-baseline entry is attributable to the rejected Lark filename, marker, user, and send time. |
| WeChat | `MEMORY-IM-ATTACH-008` | In a bound direct chat, send the WeChat PNG as one direct image item with no quoted/reference message. The pixel marker includes the run tag. | One post-baseline terminal entry appears for the WeChat user. Its attributed multimodal request contains the image input and its response exposes the PNG-only marker. |
| WeChat | `MEMORY-IM-ATTACH-010`, `MEMORY-IM-ATTACH-008` | Send the WeChat SVG as one direct file item, with no quoted/reference message. The filename includes the run tag. | No post-baseline entry is attributable to the rejected WeChat filename, marker, user, and send time. |

Record `PASS`, `FAIL`, or `INCONCLUSIVE` for every row. Where a caption is
supported, a missing caption is a failure. For every platform, an accepted image
whose pixel-only marker is absent, a rejected SVG shown as an attachment, an
unexpected entry attributable to a file-only rejected turn, or an entry
attributed to the wrong platform user is a failure.

An accepted row is `INCONCLUSIVE`, not `PASS` or `FAIL`, whenever **Call log** is
not **Recording normally**, its Model calls are unavailable/expired, or the
Request/Response evidence cannot be opened. Any row is `INCONCLUSIVE` when the
Processing Record section needed by its pass condition is unavailable. Rejected
rows do not use absence of Model calls as evidence; only the visible
post-baseline entry conditions in the table decide them.

## Fixture collection plan

This is an optional, separately authorized engineering follow-up, not a phase of
the 15-minute UI acceptance flow. The ten manual messages can inform which
fixtures are most valuable, but they do not provide the Lark `media` or WeChat
bot/self/system samples below. Do not block or complete the acceptance result on
these fixture decisions unless the collector was separately authorized and run.

Raw platform payloads may contain message IDs, user IDs, signed download URLs,
tokens, filenames, captions, and other user content. Never commit a raw capture.
Use an owner-approved, one-shot collector at the adapter-to-`message_facts`
boundary, write only to test-owned local state, and reduce the capture to a
minimal structural fixture before it enters the repository. Replace identifiers
with stable placeholders, remove URL query strings and credentials, replace text
and filenames with synthetic values, and retain only field presence, primitive
types, enum values, and list cardinality needed by the classifier.

### 1. Discord ordinary-upload embeds (highest priority)

Capture the same human DM message used by the accepted Discord row before
classification. Preserve this minimal shape:

- `author.bot`, `webhook_id`, `edited_at`, and `message.is_system()`;
- message flag `forwarded`;
- the number and structural fields of `attachments` (`filename`, MIME family,
  declared size; redact IDs and URLs);
- the number and structural types of `embeds`, including whether an embed points
  at the uploaded attachment; and
- presence/cardinality only for components, stickers, sticker items, snapshots,
  and forwards.

Decision:

- **No automatic embed:** the fixture confirms the current fail-closed predicate;
  add the reduced fixture to the Discord contract tests and keep
  `has_unverified_attachment_embeds` closed for authored embeds.
- **Automatic attachment embed:** the current classifier disables ordinary
  Discord capture at launch. Mark the Discord acceptance row failed, then change
  the predicate only after the fixture distinguishes an automatic attachment
  embed from authored/rich embeds. Do not broadly allow every embed.

### 2. Lark human `media` message

Capture one real human one-to-one `media` event and the adapter's parsed content.
Preserve:

- `sender.sender_type` and the redacted sender identifier shape;
- `message.message_type`, plus presence of `edited` and `forwarded` facts;
- the parsed content key names and which native resource key is populated;
- the downloaded file's synthetic filename, MIME family, and declared size; and
- whether `shared_text` or another non-caption payload is produced.

Decision:

- Enable `media` only if the reduced fixture proves a direct human source, a
  stable non-empty native key, and the same bounded authenticated acquisition
  semantics as `file`/`image`.
- Otherwise retain the current fail-closed policy. The acceptance run does not
  require `media` to pass.

### 3. WeChat source identity

Capture four structural `item_list` samples: an ordinary human direct image
baseline, a bot-origin message, a message sent by the account itself, and a
system message. Preserve:

- top-level source/sender/system facts and their types;
- each item `type`, `ref_msg` presence, and the selected media-item field;
- presence only of nested `media.encrypt_query_param` (replace its value); and
- list cardinality and any source facts currently discarded before
  `is_ordinary_wechat_attachment` runs.

Decision:

- If bot, self, and system shapes expose stable negative source facts, thread
  those facts into the classifier and add fixtures proving all three remain
  closed while the human baseline stays open.
- If they are structurally indistinguishable at the current boundary, do not
  widen admission. Preserve fail-closed behavior and first retain the missing
  source fact at the adapter boundary.

## Explicit non-goals

- Enabling Lark `media` before the real fixture supports that decision.
- Broadly allowing Discord messages with embeds, components, stickers, webhooks,
  forwards, or snapshots.
- Claiming WeChat bot/self/system exclusion before the raw shapes prove a stable
  discriminator. This checklist exercises only the ordinary human baseline.
- Capturing SVG, Office/iWork/ODF/RTF documents, or other extensions excluded by
  the pinned modality policy. WeChat video may reach shared admission, but video
  processing is not enabled by this acceptance.
- Group/channel, unbound, edited, forwarded, quoted/reference, or system-message
  Memory capture, and bot-authored capture on platforms with verified source
  facts. WeChat source exclusion remains the explicit fixture decision above.
- Resetting regression state, changing credentials, testing remote Incus, or
  validating Agent attachment behavior. This run covers Memory capture only.
- Provisioning Telegram or any other platform credentials. A separately
  provisioned five-platform target is a precondition, not an acceptance step.
- Proving the absence of pre-memcell provider calls through the product UI. The
  `MEMORY-IM-ATTACH-010` automated scenario owns the rejection-to-provider
  boundary; issue #1483 tracks product visibility for calls without memcells.

## Follow-up tracking

- [#1477](https://github.com/avibe-bot/avibe/issues/1477) removes the dead
  `feishu` Memory admission alias after confirming released compatibility does
  not require it; runtime adapters use the canonical `lark` literal.
- [#1480](https://github.com/avibe-bot/avibe/issues/1480) investigates why an
  explicit `vibe watch add --timeout 0` can read back as the default `21600`
  despite the repository reader preserving zero.
- [#1483](https://github.com/avibe-bot/avibe/issues/1483) tracks the missing
  Processing Record surface for provider calls and terminal turns that have no
  memcell.

## Result record

Record the run tag, source commit, five-platform provisioning check,
service-health result, Call log baseline and final state, and ten row outcomes
(including any `BLOCKED` or `INCONCLUSIVE` reason) in the owning issue or
acceptance report. If a separately authorized fixture collection was also run,
append only its scrubbed decisions; otherwise record fixture collection as **not
run (optional follow-up)**. Do not paste raw payloads, signed URLs, credentials,
or unsanitized logs into that report.
