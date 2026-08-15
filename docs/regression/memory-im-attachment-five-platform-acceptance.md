# Memory IM attachment five-platform acceptance

> **Safety boundary:** Run this checklist only in the local Incus regression
> environment after the owner explicitly authorizes a regression update. Keep the
> long-lived `master` target online and preserve its existing product state. Never
> use `--remote`, `--reset-config`, or `--reset-all`. After any authorized source
> update, verify service health before reporting an acceptance result.

This runbook validates the first Memory attachment release across Slack, Discord,
Telegram, Lark, and WeChat. It is deliberately short: prepare two fixtures, send
ten direct messages, and then inspect the ten outcomes in one pass. The Agent's
reply is not evidence of Memory capture; Memory runs independently of Agent
delivery.

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
   installation. Use a one-to-one direct message only; group, channel, unbound,
   bot, forwarded, edited, quoted, webhook, and system traffic is denied by
   `MEMORY-IM-ATTACH-002` and the platform contracts.
5. Record a run tag such as `IMA-20260816-0508`. Put it in every supported
   caption and in every fixture's pixel/file marker. Lark and WeChat native
   image/file messages are verified by bound user plus send timestamp rather
   than by assuming those clients support attachment captions.

### Test fixtures

Prepare two small files locally. Do not use personal or production data.

- **Accepted image:** a 512 x 512 PNG under 1 MiB. Put a large, high-contrast
  platform-specific marker in the pixels, for example `IMA-DISCORD-A7Q2`. The
  caption must contain the run tag but must **not** contain the pixel marker. This
  separates image understanding from text-only capture.
- **Rejected file:** an SVG under 100 KiB with a different visible marker, for
  example `IMA-DISCORD-REJECT-R9M4`. SVG is intentionally excluded from the
  pinned Memory modality policy. Where the client supports a caption on that
  attachment message, use a non-empty caption containing the run tag but not the
  SVG marker. The table explicitly identifies the file-only cases.

Use different markers for every platform. Keep a local table mapping each marker
to its platform; do not put that table in chat.

## Ten-minute acceptance flow

Send the five accepted-image messages first, then the five rejected-file
messages. After all ten sends, open **Memory > Processing Record**, refresh the
processing log, and inspect the newest entries for the five bound IM users. The
processing log is the primary evidence because it covers every user and project
on the installation; **Memory > Search** is scoped to the current principal and
is not a substitute for cross-platform verification.

For each matching processing-log row, open its detail and inspect **Message
tracking**, **Processing timeline**, and the **Memory snippet created** step. A
row passes only when the falsifiable condition below is met. If an expected row
has not reached a terminal state within 60 seconds, record a failure rather than
waiting indefinitely.

| Platform | Scenario | Send | Pass condition in Memory > Processing Record |
| --- | --- | --- | --- |
| Slack | `MEMORY-IM-ATTACH-001` | In a bound human DM, send the PNG with caption `<run-tag> slack accepted image`. | One new terminal processing entry exists for the Slack user. Its created Memory snippet contains the caption and describes or reproduces the PNG-only marker. |
| Slack | `MEMORY-IM-ATTACH-004` | In the same DM, send the SVG with caption `<run-tag> slack rejected file`. | The caption is captured as text in a terminal Memory entry, but the SVG-only marker is absent. No attachment-derived snippet is created for that SVG. |
| Discord | `MEMORY-IM-ATTACH-005` | In a bound human DM, upload the PNG as an ordinary attachment with caption `<run-tag> discord accepted image`. Do not add a link embed, component, sticker, or forward. | One new terminal processing entry exists for the Discord user and contains both the caption and evidence of the PNG-only marker. If the raw message has an automatic embed and no entry is created, fail this row and apply the Discord fixture decision below. |
| Discord | `MEMORY-IM-ATTACH-004`, `MEMORY-IM-ATTACH-005` | Upload the SVG with caption `<run-tag> discord rejected file`. | The caption is captured as text; the SVG-only marker is absent from the created Memory snippet. |
| Telegram | `MEMORY-IM-ATTACH-006` | In a bound private chat, send the PNG as one photo message with caption `<run-tag> telegram accepted image`. Do not use an album. | One new terminal processing entry exists for the Telegram user and contains the caption plus evidence of the PNG-only marker. |
| Telegram | `MEMORY-IM-ATTACH-004`, `MEMORY-IM-ATTACH-006` | Send the SVG as one document with caption `<run-tag> telegram rejected file`. | The caption is captured as text; the SVG-only marker is absent from the created Memory snippet. |
| Lark | `MEMORY-IM-ATTACH-007` | In a bound one-to-one chat, send the PNG through the native **image** action as one image-only message. The pixel marker includes the run tag. | Exactly one new terminal processing entry appears for the Lark user after the recorded send time, and its created Memory snippet describes or reproduces the PNG-only marker. |
| Lark | `MEMORY-IM-ATTACH-004`, `MEMORY-IM-ATTACH-007` | Record the current newest Lark processing entry and send the SVG through the native **file** action as one file-only message. The filename includes the run tag; its visible SVG marker is different from the filename. | No new Memory processing entry is created for this Lark message, and the SVG-only marker is absent from every entry after the recorded send time. |
| WeChat | `MEMORY-IM-ATTACH-008` | In a bound direct chat, send the PNG as one direct image item with no quoted/reference message. The pixel marker includes the run tag. | Exactly one new terminal processing entry appears for the WeChat user after the recorded send time and contains evidence of the PNG-only marker. |
| WeChat | `MEMORY-IM-ATTACH-004`, `MEMORY-IM-ATTACH-008` | Record the current newest WeChat processing entry and send the SVG as one direct file item, with no quoted/reference message. The filename includes the run tag. | No new Memory processing entry is created for this WeChat message, and the SVG-only marker is absent from every entry after the recorded send time. |

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
- Inferring WeChat human origin when the raw shape does not provide a stable
  discriminator.
- Capturing SVG, Office/iWork/ODF/RTF documents, or other extensions excluded by
  the pinned modality policy. WeChat video may reach shared admission, but video
  processing is not enabled by this acceptance.
- Group/channel, unbound, bot-authored, edited, forwarded, quoted/reference, or
  system-message Memory capture.
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
