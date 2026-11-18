import { apiFetch } from './apiFetch';

/** What a notice-bound Retry may also do: continue on another backend's built-in Agent. */
export interface FailureRetryRequestOptions {
  continueOnBackend?: string;
}

/**
 * The one retry of a failed Turn: a notice-bound message to the Session the Turn
 * belongs to. The chat page sends it for a notice in its own transcript; the
 * workspace notifications thread sends it to the Session its notice names
 * (``content.failure_source``). The server checks the same boundary for both.
 */
export async function requestFailureRetry(
  sessionId: string,
  noticeId: string,
  options?: FailureRetryRequestOptions,
): Promise<{ response: Response; body: Record<string, unknown> | null }> {
  const response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      retry_for: noticeId,
      ...(options?.continueOnBackend ? { continue_on_backend: options.continueOnBackend } : {}),
    }),
  });
  const body = await response.json().catch(() => null);
  return { response, body };
}

/** Whether a retry response says the retry was taken on (it may still be waiting its turn). */
export const retryAdmitted = (body: Record<string, unknown> | null) =>
  ['queued', 'claimed', 'accepted'].includes(String(body?.delivery_state ?? body?.state ?? ''));
