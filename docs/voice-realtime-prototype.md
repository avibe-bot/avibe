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

## Cloud token

The browser opens the socket with the token from `GET /api/cloud/token`. A
remote-access browser receives a token for its signed-in avibe.bot user,
minted through `POST /api/v1/instances/{id}/user-token`. The Workbench opened
on this machine's own loopback origin (a local browser or the desktop shell)
has no avibe.bot session, so a direct, same-origin loopback request receives a
token whose principal is the paired device, minted through
`POST /api/v1/instances/{id}/device-token`. That token grants nothing the
device secret does not already grant. Proxied requests, LAN setup hosts,
requests without same-origin evidence, an unpaired instance, and a refused or
unavailable mint get `503 cloud_unavailable`, and the browser keeps the local
HTTP relay.

## Conversation context

Dictation into a conversation sends recognition two texts from it: `reply`, the
body of its latest Agent `result` without Avibe's generated footer, and
`user_previous`, the newest message the user sent there. The cloud extracts
recognition hotwords from them; the browser never logs them. Every entry sends
the conversation it writes into: a tap on the microphone, ⌥Z, and a hold on the
composer read the chat's transcript; a hold on a Bot's avatar, or on the phone
tab bar's main Bot, reads that Bot's conversation (its newest 20 rows, waited
for at most 1.5 s while the socket opens). A chat window detached from the live
tail reads the newest rows the same way. The context is read once, when the
dictation starts; a result row arrives whole, so during a running Turn the
previous reply is used. Each text is trimmed and cut to its newest 8,000
UTF-16 code units; an empty text is not sent. The Workbench home, the
new-session sheet, and Show Page dictation have no conversation and send none.

The `start` schema is strict, so an unknown field closes the socket. The
browser therefore sends `reply` only when the cloud token that opened the
socket declares `realtime_reply_context`, and `user_previous` only when it
declares `realtime_user_previous_context`. When realtime recognition fails and
the dictation falls back to HTTP segments, every segment carries the same two
texts as form fields, sent directly to avibe.bot only when that request's token
declares `dictation_context`. The local relay (`/api/asr/transcribe`, used when
no cloud token can be minted) forwards them as given, cut to the same 8,000
units: avibe.bot reads a segment's form by field name, so a release that
predates them ignores them. avibe.bot returns `capabilities` with `user-token`
and `device-token`. `GET /api/cloud/token` relays it as a bounded list of
strings. A backend that predates the field declares none.
