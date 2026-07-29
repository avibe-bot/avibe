import { describe, expect, it } from 'vitest';

import { selectApiErrorFields } from './ApiContext';

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
