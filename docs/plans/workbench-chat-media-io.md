# Workbench Chat — Rich Media Input/Output

Branch: `feat/workbench-chat-media-io`

Source: page feedback on `/chat/:sessionId` (the avibe Workbench chat page).
Three requirements:

1. **Composer textarea not vertically centered** — looks shifted down.
2. **Composer input affordances** — an attachment button (upload file/image
   and send) and a voice button (record → avibe.bot ASR → fill the box).
3. **Agent reply media** — agent replies must render images inline and files
   as downloadable cards, reusing the IM `file://` parsing flow but adapted to
   the web surface (image-proxy URL instead of IM upload).

## Locked decisions (agreed with product owner)

- **D1 = A.** Image/file proxy uses an **opaque token** mapped server-side, not
  a raw path in the URL. Only files the agent actually referenced (or the user
  uploaded) in this session are reachable. No general "read any file" surface.
- **D2 = as proposed.** The Markdown renderer inlines a real `<img>` **only**
  for same-origin proxy URLs; any external image URL keeps the existing
  click-through behavior (no silent fetch → no IP/metadata leak).
- **D3.** Voice button is available only when paired with Vibe Cloud (same gate
  as Settings · Messaging audio transcription). Independent of the inbound-IM
  `audio_asr.enabled` toggle.
- **D4.** Attachments upload on select, show as removable chips, and may be sent
  with no text (the messages API already supports content-only rows).
- **Unified proxy.** Agent-reply images, agent-reply files, and user-uploaded
  attachment previews **all** go through the **same** token + table + endpoint.
- **In-place rewrite (owner refinement).** Images/files are rewritten **in
  place** inside the Markdown body (the `file://` URL is replaced with the proxy
  URL where it already sits) — never relocated to the end of the message. The
  frontend decides the visual purely from the element type + URL shape:
  `![]()` → inline image, `[]()` to a proxy URL → file card.
- **File card.** A filename card with a **download** button and a **preview
  (eye)** button. Same treatment for user-uploaded files.

## Architecture: the media proxy (shared spine for #2 + #3)

### New table `media_objects` (SQLAlchemy Core + Alembic)

Add to `storage/models.py` and a new migration
`storage/alembic/versions/20260602_0007_media_objects.py`. Fields are kept wide
and extensible (content type and format are first-class, per owner request):

| column | type | notes |
| --- | --- | --- |
| `token` | String PK | opaque url-safe id (the only thing in the URL) |
| `scope_id` | String FK scopes.id CASCADE | owning scope (access + cleanup) |
| `session_id` | String FK agent_sessions.id SET NULL | owning session |
| `message_id` | String, null | originating message (linked post-insert; optional) |
| `kind` | String | `image` \| `file` (extensible: `video`/`audio`) |
| `source` | String | `agent_reply` \| `user_upload` |
| `local_path` | Text | absolute path on disk |
| `file_name` | Text | display / download filename |
| `content_type` | String, null | MIME used for the HTTP response |
| `file_ext` | String, null | extension / format |
| `size_bytes` | Integer, null | size at registration (re-stat on serve) |
| `created_at` | String | ISO8601 |
| `expires_at` | String, null | optional TTL |
| `revoked_at` | String, null | optional invalidation |

Indexes: `(session_id)`, `(scope_id, created_at)`.

New service `storage/media_service.py` (mirrors `messages_service.py` style):
- `register(conn, *, scope_id, session_id, kind, source, local_path, file_name,
  content_type, file_ext, size_bytes) -> token`
- `get_by_token(conn, token) -> row | None`

### New endpoint `GET /api/sessions/<session_id>/media/<token>`

In `vibe/ui_server.py`. Auto-protected by the existing remote-access cookie
middleware (it is under `/api/*`, not exempt); `<img>`/anchor GETs carry the
same-origin session cookie, so they load without extra wiring. CSRF only guards
mutating verbs, so GET is fine.

Behavior (reuses the `_show_page_file_response` safety pattern):
- look up token → 404 if missing / revoked / expired;
- (defense) require the row's `session_id` to match the path;
- `resolve(strict=True)` the path, 404 if the file is gone;
- serve via `FileResponse(path, media_type=content_type)` with
  `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`;
- default `Content-Disposition: inline` (preview / `<img>`); `?download=1`
  switches to `attachment; filename=...`.

Optional `GET .../media/<token>/meta` → `{name, content_type, size, kind}` for
richer file cards (size + type icon) without an extra full download. MVP can
derive name/ext from the link label and lazy-`HEAD` for size.

## #1 — Textarea vertical centering

Root cause (`ui/src/components/workbench/ChatPage.tsx`, `Compose`):
- the input row uses `items-end` (so the send button stays at the bottom when
  the textarea grows multi-line), but with a single line the text is pushed to
  the bottom → looks low;
- the `textareaRef` is declared but unused — there is no auto-resize.

Fix:
- wire auto-resize on `textareaRef` (`height='auto'; height=min(scrollHeight,
  160)`), keep `max-h-40`;
- give the textarea `min-h-9` (match the 36px button) and tune `py` so a single
  line is vertically centered within that min-height; keep row `items-end` so
  multi-line still grows upward with the button pinned at the bottom.
- Verify pixel-for-pixel against the design.pen compose-bar frame.
- Forward-compatible with #2 (left-side 36px buttons align on the same baseline).

## #2 — Attachment + voice buttons

Composer gets two left-side `ghost`/`icon` buttons (paperclip, mic), mirroring
the right-side send/stop button.

### Attachments
- `POST /api/sessions/<id>/attachments` (multipart) → save under
  `get_attachments_dir()/<scope>/` (same root as IM), register in
  `media_objects` (`source=user_upload`), return `{id/token, name, mime, size,
  url}` (url = the proxy URL, used for the chip thumbnail).
- Composer shows removable chips; send writes them into `content.attachments`.
- The web dispatch must carry the local paths into the agent turn: extend the
  `dispatch_async` payload + the internal dispatch handler to pass files into
  `AgentRequest.files` (reuse the IM mechanism at
  `core/handlers/message_handler.py:373`). For already-local files we skip the
  IM "download" step and build `FileAttachment(local_path=...)` directly.

### Voice (ASR)
- Record with `MediaRecorder` (webm/opus, which `core/audio_asr.py` supports).
- `POST /api/asr/transcribe` (multipart) → save temp file → wrap as
  `FileAttachment(local_path=...)` → `AudioAsrService.transcribe_attachments()`
  → return `{text}` → fill the composer (do **not** auto-send).
- Reuses 100% of `core/audio_asr.py` + Vibe Cloud device-secret auth.
- Button gated on Vibe Cloud pairing (D3); disabled + hint otherwise.
- **Backend (already deployed):** the ASR endpoint `POST /v1/audio/transcriptions`
  is live on `avibe-backend` `main` (PR #32, merged 2026-05-19; the
  DashScope/Qwen key lives server-side in Vercel Production). It authenticates
  with the paired device's `X-Vibe-Instance-Id` / `X-Vibe-Device-Secret` and
  requires the device tunnel to be running (else `409 tunnel_not_running`) —
  exactly the contract `core/audio_asr.py` already speaks, so voice is
  end-to-end functional once the device is paired. No backend work is pending.
  (An earlier note here wrongly called this a pending dependency — it was a
  stale, already-merged source branch, ``feature/audio-transcriptions``.)

### Status (② implemented)

- ✅ Upload `POST /api/sessions/<id>/attachments` (base64 JSON → save under
  `attachments/avibe/<id>/` → register `media_objects` source=user_upload →
  token + proxy URL). base64-over-JSON keeps it on the auth+CSRF compat route
  (no multipart, no `ui_compat` expansion).
- ✅ Attachment → agent turn: tokens resolved server-side
  (`workbench_media.resolve_attachment_specs`) → dispatch payload `files` →
  `internal_server._build_dispatch_payload` builds `FileAttachment(local_path)`
  → `MessageContext.files` → existing `message_handler`/`AgentRequest.files`
  (backends only need `local_path`). `_process_file_attachments` now passes
  already-local files through (no download). Queue-flush carries queued
  attachments too, so a file attached while busy isn't lost.
- ✅ Voice `POST /api/asr/transcribe` (base64 audio → temp file →
  `AudioAsrService.transcribe_attachments` → `{text}`) + `GET /api/asr/status`
  to gate the mic button on pairing.
- ✅ Composer: paperclip (upload + removable chips), mic (MediaRecorder →
  transcribe → fill box, never auto-send), attachment-only send runs a turn.
- ✅ User-bubble rendering of `content.attachments` (image thumbnails + FileCard).
- ✅ unit tests extended (`resolve_attachment_specs`, `MessageContext(files=)`),
  ruff clean, UI build clean.
- ⏳ residual: live regression (upload → agent reads file; voice round-trip).
  The backend ASR endpoint is already deployed (see above), so this is a
  verification step, not a dependency.

## #3 — Agent reply images + files

### Server-side rewrite (controller process)
Injection point: `core/message_mirror.py` `persist_agent_message(context,
canonical_type, text)` — the unified persist entry for all platforms. Gate the
rewrite on `context.platform == "avibe"` and `canonical_type in {result,
notify}` so IM mirror rows and intermediate streaming rows are untouched.

Reuse `core/reply_enhancer.py` `_extract_file_links` / `FileLink` (the
`(!?)\[...\]\((file://...)\)` parser, `is_image` from the `!`). For each link:
1. register it in `media_objects` (kind from `is_image`, content_type from
   mimetypes, name from label/basename) → token;
2. replace the `file://...` URL **in place** with `/api/sessions/<sid>/media/
   <token>`, preserving the `!`/`[]` structure and position.

The rewritten text is what gets persisted (`content_text`) and streamed via
`message.new`. New module `core/workbench_media.py` holds the transform so the
extraction primitive stays shared with the IM enhancer.

> Confirmed: `core/system_prompt_injection.py` `_BASE_CAPABILITIES_PROMPT` is
> platform-agnostic and carries the "Send files" `file://` instruction, so avibe
> agents emit `file://` links the same as IM turns → the rewrite triggers.

### Status (③ implemented)

- ✅ `media_objects` table + migration `20260602_0011` + `media_service`.
- ✅ `core/workbench_media.rewrite_agent_media` + hook in `persist_agent_message`
  (avibe, result/notify only).
- ✅ `GET /api/sessions/<id>/media/<token>` (+ `/meta`) proxy.
- ✅ `markdown.tsx` img/a overrides + `file-card.tsx`.
- ✅ unit tests (`tests/test_workbench_media.py`, 3 pass), `ruff` clean, UI build clean.
- ⏳ residual: end-to-end check in the running workbench (real agent reply with a
  saved image/file) — best done in the local Incus regression environment.

### Frontend rendering (`ui/src/components/ui/markdown.tsx`)
- `img` override: if `src` matches the proxy route (relative `^/api/sessions/
  [^/]+/media/`) → render a real inline `<img>` (lazy, `max-width:100%`,
  rounded); otherwise keep the current click-through link (D2 preserved).
- `a` override: if `href` matches the proxy route → render the **file card**
  (filename + type icon + download + preview eye) instead of a plain link;
  otherwise unchanged.
- New `FileCard` component (reuse Card + Button + Badge). Download →
  `href + '?download=1'`; preview eye → open `href` (inline) in a new tab /
  lightbox.

### design.pen
Add two chat-media styles (inline image, file download card) to the workbench
chat frames; map every token before coding the card. (Draw after this spec is
locked.)

## Sequence

1. **#1** textarea centering (UI only, fast).
2. **#3** media spine: table + migration + `media_service` + proxy endpoint +
   `workbench_media` rewrite + markdown img/a overrides + FileCard + design.pen.
3. **#2** attachments (upload endpoint + dispatch threading + chips) and voice
   (transcribe endpoint + MediaRecorder), reusing the spine.

Single branch, atomic commits, one PR at the checkpoint (or split output/input
if the diff gets large).

## Evidence layers to update

- unit: `media_service` register/get, `workbench_media` rewrite (image vs file,
  in-place, external-URL untouched), ASR endpoint wrap.
- contract: proxy endpoint (auth, 404 on bad/expired token, disposition,
  content-type), attachments upload shape.
- scenario: n/a yet (no catalog entry for workbench media); add if one emerges.
- manual: render side-by-side vs design.pen; verify inline image + file card +
  voice fill on the running UI.
