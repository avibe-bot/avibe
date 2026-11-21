// D2a: an answer chosen or typed in a macOS notification reaches the Runtime
// from this document, with the user's own authority. Native drives this
// receiver through fenced eval (desktop/src-tauri/src/notifications/answer.rs,
// contract version 1, docs/plans/2026-11-21-desktop-d2a-notification-actions.md).
// It posts once per begin; native asks again with the same operation ID only
// while the fate is unknown, and the Runtime owns idempotency
// (docs/plans/desktop-d2-runtime-contract.md).

export const ANSWER_RECEIVER = '__AVIBE_DESKTOP_ANSWER__';
const TEXT_LIMIT = 10000;
const ID_LIMIT = 256;
const OPERATION_LIMIT = 128;
/** How many settled answers the document remembers for native to read. */
const RETAINED = 32;

export type AnswerRequest = {
  id: string;
  sessionId: string;
  messageId: string;
  revision: string;
  kind: 'choice' | 'text';
  text: string;
};

export type AnswerOutcome =
  | 'accepted'
  | 'duplicate'
  | 'already_handled'
  | 'stale'
  | 'forbidden'
  | 'not_found'
  | 'unavailable';

export type AnswerResult = {
  outcome: AnswerOutcome;
  /** For `duplicate`: what the original attempt ended as, or null while it is pending. */
  originalOutcome?: AnswerOutcome | null;
  /** For `already_handled`: the winner's type and option (null for text). */
  answerKind?: 'choice' | 'text' | null;
  choice?: string | null;
};

export type AnswerReceiver = {
  version: 1;
  document: string;
  begin: (request: unknown) => string;
  status: (id: string) => string;
};

type Post = (path: string, body: Record<string, unknown>) => Promise<Response>;

const isObject = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === 'object' && !Array.isArray(value);

const bounded = (value: unknown, limit: number): value is string =>
  typeof value === 'string' && value.trim().length > 0 && value.length <= limit;

export function parseAnswer(value: unknown): AnswerRequest | null {
  if (!isObject(value)) return null;
  const { id, sessionId, messageId, revision, kind, text } = value;
  if (!bounded(id, OPERATION_LIMIT) || !bounded(sessionId, ID_LIMIT) || !bounded(messageId, ID_LIMIT)) return null;
  if (typeof revision !== 'string' || !/^[0-9a-f]{64}$/.test(revision)) return null;
  if ((kind !== 'choice' && kind !== 'text') || !bounded(text, TEXT_LIMIT)) return null;
  return { id, sessionId, messageId, revision, kind, text };
}

/** The Runtime request for one answer: a choice names its option group, text the message it replies to. */
export function answerBody(request: AnswerRequest): Record<string, unknown> {
  return {
    text: request.text,
    operation_id: request.id,
    revision: request.revision,
    ...(request.kind === 'choice'
      ? { metadata: { quick_reply_for: request.messageId } }
      : { in_reply_to: request.messageId }),
  };
}

const OUTCOMES = new Set<AnswerOutcome>(['accepted', 'duplicate', 'already_handled', 'stale', 'forbidden', 'not_found']);

const outcomeOf = (value: unknown): AnswerOutcome | null =>
  typeof value === 'string' && OUTCOMES.has(value as AnswerOutcome) ? (value as AnswerOutcome) : null;

/**
 * The settled result of one POST. The Runtime's `answer.outcome` decides; an
 * outer 401/403/404 without an answer body is forbidden / not_found; anything
 * else (network failure, uncertain dispatch, an older Runtime) is unavailable.
 */
export async function settle(response: Response | null): Promise<AnswerResult> {
  if (!response) return { outcome: 'unavailable' };
  const body: unknown = await response.json().catch(() => null);
  const answer = isObject(body) && isObject(body.answer) ? body.answer : null;
  const outcome = outcomeOf(answer?.outcome);
  if (answer && outcome) {
    return {
      outcome,
      ...(outcome === 'duplicate' ? { originalOutcome: outcomeOf(answer.original_outcome) } : {}),
      ...(outcome === 'already_handled'
        ? {
          answerKind: answer.answer_kind === 'choice' || answer.answer_kind === 'text' ? answer.answer_kind : null,
          choice: typeof answer.choice === 'string' ? answer.choice : null,
        }
        : {}),
    };
  }
  if (response.status === 401 || response.status === 403) return { outcome: 'forbidden' };
  if (response.status === 404) return { outcome: 'not_found' };
  return { outcome: 'unavailable' };
}

/** A settled result that proves nothing either way: asked again with the same operation ID. */
export const uncertain = (result: AnswerResult) =>
  result.outcome === 'unavailable' || (result.outcome === 'duplicate' && !result.originalOutcome);

export function createAnswerReceiver(post: Post, document = crypto.randomUUID()): AnswerReceiver {
  const results = new Map<string, AnswerResult | 'pending'>();
  const remember = (id: string, result: AnswerResult | 'pending') => {
    results.delete(id);
    results.set(id, result);
    while (results.size > RETAINED) results.delete(results.keys().next().value as string);
  };
  return {
    version: 1,
    document,
    begin: (value) => {
      const request = parseAnswer(value);
      if (!request) return 'invalid';
      // A repeated begin for the same click posts again only when the last
      // attempt's fate is unknown; the Runtime answers that replay by operation ID.
      const previous = results.get(request.id);
      if (previous !== undefined && (previous === 'pending' || !uncertain(previous))) return 'started';
      remember(request.id, 'pending');
      void post(`/api/sessions/${encodeURIComponent(request.sessionId)}/messages`, answerBody(request))
        .then((response) => settle(response), () => settle(null))
        .then((result) => remember(request.id, result));
      return 'started';
    },
    status: (id) => {
      const result = results.get(id);
      if (result === undefined) return 'unknown';
      return result === 'pending' ? 'pending' : JSON.stringify(result);
    },
  };
}
