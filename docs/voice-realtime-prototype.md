# Realtime Voice

The browser Realtime path is enabled by default and preserves the existing HTTP
dictation queue as its fallback. The default model is
`qwen3-asr-flash-realtime`.

The browser sends `start`, 250 ms PCM16 `audio`, and `finish` frames over the
`avibe-asr-v1.<cloud-token>` subprotocol. The cloud route authenticates the
existing `asr` capability, forwards audio to the configured DashScope realtime
endpoint, and returns non-authoritative previews followed by one final text.
The preview is transient editor state; only the server-cleaned final result is
committed to the draft. Provider credentials remain server-side. Handshake,
transport, protocol, timeout, and upstream failures activate the existing HTTP
recording fallback without changing legacy or dictation endpoint contracts.

## Reply context

When dictation starts in an existing chat, `start` may also carry `reply`: the
full body of the chat's latest Agent `result`, without Avibe's generated
footer. The cloud extracts recognition hotwords from it; the browser neither
extracts nor truncates it, and never logs it. The reply is read once, when
dictation starts and before the microphone opens. A result row arrives whole, so during a running
Turn the previous reply is used. The field is omitted when the reply is empty
or longer than 200,000 characters. It is also omitted when the transcript
window does not reach the live tail. The Workbench home, the new-session sheet,
and Show Page dictation have no latest reply and never send it.

The `start` schema is strict, so an unknown field closes the socket. The
browser therefore sends `reply` only when the cloud token that opened the
socket declares `realtime_reply_context`. avibe.bot returns `capabilities` with
`POST /api/v1/instances/{id}/user-token`. `GET /api/cloud/token` relays it as a
bounded list of strings. A backend that predates the field declares none.
