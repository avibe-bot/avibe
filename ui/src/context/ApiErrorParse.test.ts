import { describe, expect, it } from 'vitest';

import { archivedConflictSessionId, selectApiErrorFields } from './ApiContext';

// ── Codex review #4 (ui_server.py:6611) ───────────────────────────────────────
// PATCH /api/sessions/<id> on an archived session answered
//   {"error": "session is archived", "code": "session_archived"}
// and the round-3 test asserted body["code"], which passed — while the field the
// Web UI actually consumes was the FLAT ``error`` string. handleApiError reads
// ``error`` first and treats a string ``error`` AS the code, so the machine code
// was dropped: ApiError.code became the English sentence, errors.session_archived
// never resolved, and a zh-locale user read raw English.
//
// The bodies below are copied from the route (asserted server-side in
// tests/test_ui_server_fastapi.py::test_sessions_patch_on_archived_session_is_409,
// which now pins the nested object too — the pair is what closes the loop).
const DEFAULT = 'Request failed: PATCH /api/sessions/ses_1 (409)';

describe('error-body parsing preserves the machine code', () => {
  it('reads the code out of the structured shape the routes use', () => {
    // The archived-PATCH 409, verbatim.
    expect(
      selectApiErrorFields(
        {
          ok: false,
          error: { code: 'session_archived', message: 'session is archived' },
          code: 'session_archived',
          message: 'session is archived',
        },
        DEFAULT,
      ),
    ).toEqual({ code: 'session_archived', fallback: 'session is archived' });
  });

  it('drops the code when a route pairs a string error with a top-level code', () => {
    // The defect, pinned as a NEGATIVE so the contract is explicit rather than
    // folklore: ``error`` wins, and a string ``error`` is the code. Any route that
    // needs a machine code must nest it.
    expect(
      selectApiErrorFields({ error: 'session is archived', code: 'session_archived' }, DEFAULT),
    ).toEqual({ code: 'session is archived', fallback: 'session is archived' });
  });

  it('still accepts the two pre-existing shapes unchanged', () => {
    // Bare human string, no code at all (the majority of routes) — the message is
    // both "code" and fallback, and t() misses harmlessly.
    expect(selectApiErrorFields({ error: 'Session not found: ses_1' }, DEFAULT)).toEqual({
      code: 'Session not found: ses_1',
      fallback: 'Session not found: ses_1',
    });
    // Top-level {code, message} only (e.g. /api/vault/*).
    expect(selectApiErrorFields({ code: 'vault_locked', message: 'Vault is locked' }, DEFAULT)).toEqual({
      code: 'vault_locked',
      fallback: 'Vault is locked',
    });
    // Nested code with no message falls back to the code, never to [object Object].
    expect(selectApiErrorFields({ error: { code: 'session_archived' } }, DEFAULT)).toEqual({
      code: 'session_archived',
      fallback: 'session_archived',
    });
    // An object with no code at all is not a code; the caller's default carries
    // the message rather than a stringified object.
    expect(selectApiErrorFields({ error: { detail: 'nope' } }, DEFAULT)).toEqual({
      code: null,
      fallback: DEFAULT,
    });
    // No error field at all → nothing to localize, caller keeps its default.
    expect(selectApiErrorFields({ ok: true }, DEFAULT)).toBeNull();
  });
});

// ── Codex review #5 (ChatPage.tsx:1280) ───────────────────────────────────────
// Round 2 made the archived 409 CONVERGE (patch the row read-only, then reload)
// for the messages POST only. The next verb the reviewer tried — a PATCH rename /
// re-route — stored the error text and left the title editor and route picker live,
// re-issuing a permanently rejected request. Fixing it per-verb is what produced
// the finding three rounds running, so convergence moved to the API layer:
// ``handleApiError`` announces every ``session_archived`` body once, and the
// session it is about is read off the request path with the helper below.
const ARCHIVED = 'session_archived';

describe('locating the session a session_archived response is about', () => {
  it('reads the id from every session-scoped route family that can 409', () => {
    // PATCH /api/sessions/<id> — the verb this round is about.
    expect(archivedConflictSessionId(ARCHIVED, '/api/sessions/ses_1')).toBe('ses_1');
    // ...and the nested writes under it.
    expect(archivedConflictSessionId(ARCHIVED, '/api/sessions/ses_1/messages')).toBe('ses_1');
    expect(archivedConflictSessionId(ARCHIVED, '/api/sessions/ses_1/fork')).toBe('ses_1');
    // Show Page mutations are session-keyed under their own collection: ensure,
    // visibility, share-id, rotate-share, icon.
    expect(archivedConflictSessionId(ARCHIVED, '/api/show-pages/ses_1/ensure')).toBe('ses_1');
    expect(archivedConflictSessionId(ARCHIVED, '/api/show-pages/ses_1/visibility')).toBe('ses_1');
    expect(archivedConflictSessionId(ARCHIVED, '/api/show-pages/ses_1/share-id')).toBe('ses_1');
    expect(archivedConflictSessionId(ARCHIVED, '/api/show-pages/ses_1/rotate-share')).toBe('ses_1');
    expect(archivedConflictSessionId(ARCHIVED, '/api/show-pages/ses_1/icon')).toBe('ses_1');
  });

  it('accepts the LABEL form updateSession passes, not just a bare path', () => {
    // ``updateSession`` hands handleApiError "PATCH /api/sessions/<id>" as its error
    // path. That is precisely the route this convergence exists for, so an anchored
    // pattern would miss the one case that motivated it.
    expect(archivedConflictSessionId(ARCHIVED, 'PATCH /api/sessions/ses_1')).toBe('ses_1');
  });

  it('announces nothing for any other failure', () => {
    // A read-only chat must be entered because the SERVER said the row is terminal —
    // never because some other request failed. A backend lock in particular is
    // transient and shares the 409 status.
    expect(archivedConflictSessionId('backend_locked', '/api/sessions/ses_1')).toBeNull();
    expect(archivedConflictSessionId('session_not_found', '/api/sessions/ses_1')).toBeNull();
    expect(archivedConflictSessionId(null, '/api/sessions/ses_1')).toBeNull();
  });

  it('ignores a query string, and decodes an escaped id', () => {
    expect(archivedConflictSessionId(ARCHIVED, '/api/sessions/ses_1?cache=0')).toBe('ses_1');
    expect(archivedConflictSessionId(ARCHIVED, '/api/sessions/ses%2F1/messages')).toBe('ses/1');
    // A malformed escape must not throw inside an error handler.
    expect(archivedConflictSessionId(ARCHIVED, '/api/sessions/ses%ZZ')).toBe('ses%ZZ');
  });

  it('answers null when the path names no session', () => {
    // Collection-level and unrelated routes: nothing to converge.
    expect(archivedConflictSessionId(ARCHIVED, '/api/sessions')).toBeNull();
    expect(archivedConflictSessionId(ARCHIVED, '/api/sessions/')).toBeNull();
    expect(archivedConflictSessionId(ARCHIVED, '/api/projects/proj_1')).toBeNull();
    expect(archivedConflictSessionId(ARCHIVED, '/api/vault/secrets')).toBeNull();
  });
});
