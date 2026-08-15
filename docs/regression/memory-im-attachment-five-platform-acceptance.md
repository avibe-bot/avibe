# Memory IM attachment five-platform acceptance

> **Safety boundary:** Run this checklist only in the local Incus regression
> environment after the owner explicitly authorizes a regression update. Keep the
> long-lived `master` target online and preserve its existing product state. Never
> use `--remote`, `--reset-config`, or `--reset-all`. After any authorized source
> update, verify service health before reporting an acceptance result.

This runbook validates the first Memory attachment release across Slack, Discord,
Telegram, Lark, and WeChat. Establish five UI-visible principal baselines, send
five accepted images and three caption-bearing rejected files, then wait for the
eight observable outcomes. The Agent's reply is not evidence of Memory capture;
Memory runs independently of Agent delivery.

This owner-facing checklist uses only evidence visible in the product UI. It does
not require container access, database queries, or log inspection. A property
that needs engineering tools belongs in an automated contract test or a product
observability issue, not in this checklist.

## Preconditions

Do not send the attachment fixtures until all of these are true:

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
   installation. Use an ordinary, unquoted one-to-one direct message only.
   Group, channel, unbound, forwarded, edited, webhook, system, quoted, and
   replied-to traffic stays outside this human baseline. Quoted/replied-to
   exclusion is not verified across platforms: Slack accepts ordinary
   `rich_text_quote` content, while Discord and Telegram expose reference facts
   that their Memory attachment classifiers do not currently enforce. WeChat
   bot/self/system source exclusion also remains unverified and is collected
   below; this checklist does not claim those sources are currently excluded.
6. Record a run tag such as `IMA-20260816-0508`. Use it in baseline text and every
   supported caption, but never in an accepted image's pixel-only marker.
7. From the exact human identity that will send each platform fixture, send one
   pure-text baseline with a unique value such as `<run-tag> principal slack`.
   In **Memory > Processing Record**, wait for each tagged baseline, open it, and
   record its opaque `principal_id` and timestamp. That entry is both the safe
   product-UI mapping from test identity to principal and the per-principal
   high-water mark. If any of the five identities cannot establish its tagged
   baseline, record the run as `BLOCKED` and stop; do not infer the mapping from
   the HMAC-derived identifier or use an engineering lookup tool.

### Test fixtures

Prepare five accepted PNGs (one per platform) and three rejected SVGs (Slack,
Discord, and Telegram), eight files total. Do not reuse a file across platforms,
and do not use personal or production data.

- **Accepted image:** a 512 x 512 PNG under 1 MiB. Put a large, high-contrast,
  platform-specific marker in the pixels, for example `PX-Q7M4-V2K9`. Match the
  complete marker in the model response, never the run tag or a shared substring.
  Use a neutral filename such as `accepted-01.png`; the complete marker must not
  appear in the filename, caption, run tag, title, description, alt text, or any
  other message metadata.
- **Rejected file:** an SVG under 100 KiB with a different visible marker, for
  example `REJECT-R9M4`. SVG is intentionally excluded from the
  pinned Memory modality policy. Where the client supports a caption on that
  attachment message, use a non-empty caption containing the run tag but not the
  SVG marker. Use a neutral filename such as `rejected-01.svg`.

The isolation rule covers every text channel retained by the current path:

- `EverOSPort.add()` sends capture text plus attachment `kind`, `name`, `uri`, and
  `ext`; kind/extension are closed classifier values, while the pinned URI uses an
  opaque bundle ID and indexed filename rather than the source name;
- Slack may derive `attachment.name` from file `name` or `title`, so both stay
  neutral;
- Discord retains the attachment filename; leave descriptions/alt text absent or
  neutral as well;
- Telegram photo names normalize to `telegram-photo.jpg`, while its caption is
  capture text;
- Lark image resource keys become the attachment name; do not repeat the pixel
  marker in surrounding message text; and
- WeChat item filenames become attachment names, so they stay neutral.

Use a different complete pixel marker for every accepted platform image. Keep a
local mapping from neutral fixture name and marker to platform; do not put that
mapping in any message or platform metadata.

## State-driven acceptance flow

The completion condition is visible state, not elapsed wall time. The long-lived
`master` target has a shared preserved queue and may receive concurrent traffic,
so no finite delay proves that a missing fixture is a product failure.

1. Send the eight fixtures as described in the table. Use one fixed order for
   reproducibility, but no pass condition depends on that order.
2. Do not send another message in a tested session. Refresh **Memory > Processing
   Record** until all eight post-baseline outcomes are identifiable under the five
   recorded principals: five accepted-image terminal outcomes and the Slack,
   Discord, and Telegram rejected-caption outcomes. A flush may combine outcomes,
   so do not require eight distinct entries.
3. Stop waiting 30 minutes after the final fixture. This is an operator stop
   threshold, chosen at the same scale as `MAX_UNFLUSHED_AGE = 30 minutes`, not a
   delivery upper bound. If any expected outcome is still absent, mark the whole
   run `INCONCLUSIVE`, not `FAIL`: a shared preserved queue or concurrent traffic
   can delay this run without proving a product defect. In an idle environment,
   `IDLE_FLUSH_TIMEOUT = 5 minutes` explains why results commonly appear sooner.
4. Once all eight outcomes are present, recheck that **Call log** is **Recording
   normally**, then evaluate every row against its visible pass/fail condition.

This flow deliberately does **not** use `/new`. That command tears down the Agent
session and pauses session-bound tasks and watches
(`core/handlers/command_handlers.py`), which conflicts with the long-lived
`master` environment's state-preservation boundary. State-driven completion avoids
those mutations and returns as soon as the eight observable outcomes are ready.

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
SVG filename as an attachment in the entry preview. The hermetic
`MEMORY-IM-ATTACH-010` scenario separately proves that a fully rejected
caption-bearing attachment does not enter the multimodal provider path; the
manual run does not try to reconstruct that engineering fact from an
unobservable UI absence.

| Platform | Scenario | Send | Pass condition in Memory > Processing Record |
| --- | --- | --- | --- |
| Slack | `MEMORY-IM-ATTACH-001` | In the baseline Slack DM, send the neutral-named PNG with caption `<run-tag> slack accepted image`; keep the complete pixel marker out of the caption and file title. | One post-baseline terminal outcome exists under the recorded Slack principal. Its **Model calls** contain an attributed multimodal request with the image and a response that exposes the complete pixel-only marker. |
| Slack | `MEMORY-IM-ATTACH-010` | In the same DM, send the neutral-named SVG with caption `<run-tag> slack rejected file`. | An outcome under the recorded Slack principal preserves the caption, and its preview does not show the neutral Slack SVG filename as an attachment. |
| Discord | `MEMORY-IM-ATTACH-005` | In the baseline Discord DM, upload the neutral-named PNG as an ordinary attachment with caption `<run-tag> discord accepted image`. Keep the complete pixel marker out of the caption and attachment metadata. Do not add a link embed, component, sticker, or forward. | One post-baseline terminal outcome exists under the recorded Discord principal. Its attributed multimodal request contains the image and its response exposes the complete pixel-only marker. A missing outcome at the stop threshold is `INCONCLUSIVE`; the optional fixture decision below diagnoses automatic embeds. |
| Discord | `MEMORY-IM-ATTACH-010`, `MEMORY-IM-ATTACH-005` | Upload the neutral-named SVG with caption `<run-tag> discord rejected file`. | An outcome under the recorded Discord principal preserves the caption, and its preview does not show the neutral Discord SVG filename as an attachment. |
| Telegram | `MEMORY-IM-ATTACH-006` | In the baseline Telegram private chat, send the PNG as one photo with caption `<run-tag> telegram accepted image`. Keep the complete pixel marker out of the caption and do not use an album. | One post-baseline terminal outcome exists under the recorded Telegram principal. Its attributed multimodal request contains the Telegram image input and its response exposes the complete pixel-only marker. The request may identify the normalized JPEG photo rather than the original PNG filename/MIME. |
| Telegram | `MEMORY-IM-ATTACH-010`, `MEMORY-IM-ATTACH-006` | Send the neutral-named SVG as one document with caption `<run-tag> telegram rejected file`. | An outcome under the recorded Telegram principal preserves the caption, and its preview does not show the neutral Telegram SVG filename as an attachment. |
| Lark | `MEMORY-IM-ATTACH-007` | In a bound one-to-one chat, send the neutral-named Lark PNG through the native **image** action as one image-only message. Do not repeat its complete pixel marker in surrounding text. | One post-baseline terminal outcome appears under the recorded Lark principal. Its attributed multimodal request contains the image input and its response exposes the complete pixel-only marker. |
| WeChat | `MEMORY-IM-ATTACH-008` | In a bound direct chat, send the neutral-named WeChat PNG as one direct image item with no quoted/reference message. Do not repeat its complete pixel marker in surrounding text or item metadata. | One post-baseline terminal outcome appears under the recorded WeChat principal. Its attributed multimodal request contains the image input and its response exposes the complete pixel-only marker. |

Record `PASS`, `FAIL`, or `INCONCLUSIVE` for every row. Where a caption is
supported, a missing caption is a failure. For every platform, an accepted image
whose complete pixel-only marker is absent from an available terminal response,
a rejected SVG shown as an attachment, or an outcome under a principal different
from the identity's tagged baseline is a failure.

An accepted row is `INCONCLUSIVE`, not `PASS` or `FAIL`, whenever **Call log** is
not **Recording normally**, its Model calls are unavailable/expired, or the
Request/Response evidence cannot be opened. Any row is `INCONCLUSIVE` when the
Processing Record section needed by its pass condition is unavailable. Rejected
rows do not use absence of Model calls as evidence; only the visible
post-baseline entry conditions in the table decide them. A terminal outcome that
is present but violates its row is `FAIL`; the 30-minute stop threshold applies
only when required evidence never becomes observable.

Before recording `PASS`, state the visible counterfactual: broken accepted-image
processing yields an available terminal call whose response lacks the complete
pixel-only marker; broken rejected-file filtering yields a visible attachment
filename or loses its caption; broken attribution places the outcome under a
principal other than the tagged baseline. If the UI never exposes the evidence
needed to distinguish those states, record `INCONCLUSIVE` instead.

## Fixture collection plan

This is an optional, separately authorized engineering follow-up, not a phase of
the state-driven UI acceptance flow. The eight manual messages can inform which
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
- Group/channel, unbound, edited, forwarded, webhook, or system-message Memory
  capture, and bot-authored capture on platforms with verified source facts.
  Quoted/replied-to traffic is unverified and out of scope rather than claimed
  closed; WeChat source exclusion remains the explicit fixture decision above.
- Resetting regression state, changing credentials, testing remote Incus, or
  validating Agent attachment behavior. This run covers Memory capture only.
- Provisioning Telegram or any other platform credentials. A separately
  provisioned five-platform target is a precondition, not an acceptance step.
- Proving the absence of pre-memcell provider calls through the product UI. The
  `MEMORY-IM-ATTACH-010` automated scenario owns the rejection-to-provider
  boundary; issue #1483 tracks product visibility for calls without memcells.
- Manually testing captionless, fully filtered Lark or WeChat file-only turns.
  `CaptureAdmission.decide()` returns `CaptureSkipped(memory_invalid_input)` when
  both text and selected attachments are empty (`core/memory/admission.py`),
  before a `CaptureRequest`, queue row, provider call, or memcell can exist. The
  unit test
  `test_im_attachment_only_turn_with_every_upload_filtered_is_not_captured`
  pins that structural boundary; this checklist does not treat an unobservable
  absence as a manual PASS.

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

Record the run tag, source commit, five-platform provisioning check, the five
tagged principal baselines, service-health result, Call log baseline and final
state, and eight row outcomes
(including any `BLOCKED` or `INCONCLUSIVE` reason) in the owning issue or
acceptance report. If a separately authorized fixture collection was also run,
append only its scrubbed decisions; otherwise record fixture collection as **not
run (optional follow-up)**. Do not paste raw payloads, signed URLs, credentials,
or unsanitized logs into that report.
