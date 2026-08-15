# Memory IM attachment five-platform acceptance

> **Safety boundary:** Run this checklist only in the local Incus regression
> environment after the owner explicitly authorizes a regression update. Keep the
> long-lived `master` target online and preserve its existing product state. Never
> use `--remote`, `--reset-config`, or `--reset-all`. After any authorized source
> update, verify service health before reporting an acceptance result.

This runbook validates the first Memory attachment release across Slack, Discord,
Telegram, Lark, and WeChat. It is deliberately short: prepare five fixture pairs,
send ten direct messages in two flush-bounded phases, and inspect the ten
outcomes. The Agent's reply is not evidence of Memory capture; Memory runs
independently of Agent delivery.

## Preconditions

Do not start the ten-minute clock until all of these are true:

1. The authorized local Incus `master` target is healthy and still has its prior
   product state.
2. Memory is enabled. In the Web UI, open **Memory > Processing Record** and verify
   **Engine status** is **Healthy**.
3. In **Memory > Settings**, the **IM attachment processing model (optional)** has
   a complete base URL, model, and API key. This is the explicit opt-in required by
   `MEMORY-IM-ATTACH-003`.
4. The human test account on each platform is enabled and bound to this Avibe
   installation. Use a one-to-one direct message only. Group, channel, unbound,
   forwarded, edited, quoted, webhook, and system traffic stays outside this
   human baseline and is closed where the platform exposes the required facts.
   WeChat bot/self/system source exclusion remains unverified and is collected
   below; this checklist does not claim those sources are currently excluded.
5. Record a run tag such as `IMA-20260816-0508`. Put it in every supported
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

## Ten-minute acceptance flow

Run the accepted and rejected rows as separate phases. This separation matters:
ordinary Memory adds otherwise wait for the five-minute idle flush, and a late
accepted result could contaminate a rejected-row baseline.

1. Record the newest processing entry and provider-call timestamp for each bound
   user, then send all five accepted-image messages.
2. In each of the same five direct-message sessions, send `/new` after the image
   turn has been accepted for processing. Wait for the new-session acknowledgement.
   `/new` invokes the old session's final Memory flush; it is the documented phase
   boundary and avoids treating the five-minute idle timeout as a failure.
3. Open **Memory > Processing Record**, refresh the processing log, and finish all
   five accepted-row checks before continuing. Start the 60-second terminal-state
   allowance only after the corresponding `/new` acknowledgement. A lifecycle
   busy/flush error, a missing acknowledgement, or a non-terminal expected row
   after that allowance is a recorded failure; do not continue to the rejected
   phase with an unsettled accepted row.
4. Record fresh per-user processing-entry and provider-call baselines. Send all
   five rejected SVG messages, then send `/new` in each corresponding direct
   message and wait for its acknowledgement.
5. Refresh **Processing Record** and evaluate all five rejected rows against the
   fresh baselines. Apply the same post-`/new` 60-second allowance where a
   text-only entry is expected.

The processing log is the primary evidence because it covers every user and
project on the installation; **Memory > Search** is scoped to the current
principal and is not a substitute for cross-platform verification. For an
accepted image, open the matching processing entry, expand **Model calls**, and
inspect the attributed multimodal model call's **Request** and **Response**. The
request must contain the image input and the response must describe or reproduce
the pixel-only marker. The timeline and Memory snippet preview do not expose
attachment-derived model output and therefore are not positive evidence.

For a rejected SVG, inspect calls after the phase baseline and verify that no
multimodal call has an image/attachment request attributable to that turn. A
caption-bearing rejected turn must still produce its expected text-only entry;
the Lark and WeChat file-only rows must produce neither a new processing entry nor
an attributable multimodal call. A row passes only when the falsifiable condition
below is met.

| Platform | Scenario | Send | Pass condition in Memory > Processing Record |
| --- | --- | --- | --- |
| Slack | `MEMORY-IM-ATTACH-001` | In a bound human DM, send the Slack PNG with caption `<run-tag> slack accepted image`. | After `/new`, one terminal entry exists for the Slack user. Its **Model calls** contain an attributed multimodal request with the PNG and a response that describes or reproduces the PNG-only marker. |
| Slack | `MEMORY-IM-ATTACH-004` | In the same DM, send the Slack SVG with caption `<run-tag> slack rejected file`. | After `/new`, the caption appears in a terminal text-only entry. No call after the phase baseline sends the SVG to the multimodal model, and no provider response contains its marker. |
| Discord | `MEMORY-IM-ATTACH-005` | In a bound human DM, upload the Discord PNG as an ordinary attachment with caption `<run-tag> discord accepted image`. Do not add a link embed, component, sticker, or forward. | After `/new`, one terminal entry exists for the Discord user. Its attributed multimodal request contains the PNG and its response exposes the PNG-only marker. If the raw message has an automatic embed and no entry is created, fail this row and apply the Discord fixture decision below. |
| Discord | `MEMORY-IM-ATTACH-004`, `MEMORY-IM-ATTACH-005` | Upload the Discord SVG with caption `<run-tag> discord rejected file`. | After `/new`, the caption appears in a terminal text-only entry. No attributable multimodal request contains the SVG, and no provider response contains its marker. |
| Telegram | `MEMORY-IM-ATTACH-006` | In a bound private chat, send the Telegram PNG as one photo message with caption `<run-tag> telegram accepted image`. Do not use an album. | After `/new`, one terminal entry exists for the Telegram user. Its attributed multimodal request contains the PNG and its response exposes the PNG-only marker. |
| Telegram | `MEMORY-IM-ATTACH-004`, `MEMORY-IM-ATTACH-006` | Send the Telegram SVG as one document with caption `<run-tag> telegram rejected file`. | After `/new`, the caption appears in a terminal text-only entry. No attributable multimodal request contains the SVG, and no provider response contains its marker. |
| Lark | `MEMORY-IM-ATTACH-007` | In a bound one-to-one chat, send the Lark PNG through the native **image** action as one image-only message. The pixel marker includes the run tag. | After `/new`, exactly one terminal entry appears for the Lark user. Its attributed multimodal request contains the PNG and its response exposes the PNG-only marker. |
| Lark | `MEMORY-IM-ATTACH-004`, `MEMORY-IM-ATTACH-007` | Send the Lark SVG through the native **file** action as one file-only message. The filename includes the run tag; its visible SVG marker is different from the filename. | After `/new`, no processing entry or attributable multimodal call appears after the rejected-phase baseline, and no provider response contains the SVG-only marker. |
| WeChat | `MEMORY-IM-ATTACH-008` | In a bound direct chat, send the WeChat PNG as one direct image item with no quoted/reference message. The pixel marker includes the run tag. | After `/new`, exactly one terminal entry appears for the WeChat user. Its attributed multimodal request contains the PNG and its response exposes the PNG-only marker. |
| WeChat | `MEMORY-IM-ATTACH-004`, `MEMORY-IM-ATTACH-008` | Send the WeChat SVG as one direct file item, with no quoted/reference message. The filename includes the run tag. | After `/new`, no processing entry or attributable multimodal call appears after the rejected-phase baseline, and no provider response contains the SVG-only marker. |

Record `PASS` or `FAIL` for every row. Where a caption is supported, a missing
caption is a failure. For every platform, an accepted image whose pixel-only
marker is absent, a rejected SVG whose marker appears, an unexpected entry for a
file-only rejected turn, or an entry attributed to the wrong platform user is a
failure.

## Fixture collection plan

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

## Follow-up tracking

- [#1477](https://github.com/avibe-bot/avibe/issues/1477) removes the dead
  `feishu` Memory admission alias after confirming released compatibility does
  not require it; runtime adapters use the canonical `lark` literal.
- [#1480](https://github.com/avibe-bot/avibe/issues/1480) investigates why an
  explicit `vibe watch add --timeout 0` can read back as the default `21600`
  despite the repository reader preserving zero.

## Result record

Record the run tag, source commit, service-health result, ten row outcomes, and
the three fixture decisions in the owning issue or acceptance report. Do not
paste raw payloads, signed URLs, credentials, or unsanitized logs into that
report.
