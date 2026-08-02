// Pure parsing of an API error body. Lives outside ApiContext so the response
// contract stays unit-testable without mounting a provider (see ApiErrorParse.test.ts);
// ``handleApiError`` is the only place that turns these answers into side effects.

/** Pick the machine code + human fallback out of an error response body.
 *
 *  Accepts the legacy ``error`` shape (a bare string, or ``{ code, message }``) AND the
 *  top-level ``{ code, message }`` shape newer routes use (e.g. /api/vault/*), so callers
 *  always get a real ``ApiError.code`` to branch on instead of a generic status string.
 *
 *  Note the precedence, which is a CONTRACT for route authors: ``error`` is consulted
 *  first, and a STRING ``error`` is taken as the code — so a route that pairs a human
 *  sentence in ``error`` with the real code alongside it in a top-level ``code`` loses that
 *  code here, and its message is rendered verbatim in every locale. Routes with a machine
 *  code must nest it: ``{"error": {"code", "message"}}`` (see ``_show_page_error_response``
 *  in ``vibe/ui_server.py``). Extracted from ``handleApiError`` unchanged so that contract
 *  is directly testable — see ApiErrorParse.test.ts. */
export const selectApiErrorFields = (
  data: any,
  defaultMessage: string,
): { code: string | null; fallback: string } | null => {
  // Not ``data?.error``: a non-object JSON body (``null``) must keep THROWING here so
  // handleApiError's catch falls back to the status text, exactly as before.
  const rawErr = data.error ?? (data.code ? { code: data.code, message: data.message } : undefined);
  if (!rawErr) return null;
  const code = typeof rawErr === 'string' ? rawErr : rawErr?.code;
  const fallback = typeof rawErr === 'string' ? rawErr : rawErr?.message ?? rawErr?.code ?? defaultMessage;
  return { code: typeof code === 'string' ? code : null, fallback };
};

/** Which session an error response says is archived, or ``null`` if it says no such
 *  thing. The WHOLE decision — the code guard and the id lookup — so it is testable
 *  without a DOM (this repo has no DOM test environment; same reason
 *  ``selectApiErrorFields`` was extracted). ``handleApiError`` only fans the answer
 *  out to subscribers.
 *
 *  Every route that can answer ``409 session_archived`` puts the session id in the
 *  first segment after its collection: ``/api/sessions/<id>`` (PATCH), plus
 *  ``/messages`` and ``/fork`` under it, and ``/api/show-pages/<session_id>/…``
 *  (ensure / visibility / share-id / rotate-share / icon). One pattern therefore
 *  covers all of them, and a future session-scoped route inherits it.
 *
 *  Deliberately UNANCHORED: some callers pass a human LABEL rather than a bare
 *  path (``updateSession`` sends ``"PATCH /api/sessions/<id>"``), and that label
 *  belongs to the very route this convergence was added for. */
export const archivedConflictSessionId = (code: string | null, path: string): string | null => {
  if (code !== 'session_archived') return null;
  const match = /\/api\/(?:sessions|show-pages)\/([^/?#\s]+)/.exec(path);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]) || null;
  } catch {
    // A malformed escape must not throw inside an error handler.
    return match[1] || null;
  }
};
