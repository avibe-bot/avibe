import { specFor } from './messageTypes';

// Status pills rather than authored answers — the catalog's ``status`` render kind.
export const isNotifyMessageType = (type: string): boolean => specFor(type).render === 'status';

// A status-typed row whose ``metadata.event`` the catalog lists under
// ``agentRenderWhenEvents``: words addressed to the owner (a Computer Use script's
// notify or confirm prompt), drawn as the Agent's message so they are noticed
// mid-Turn. Presentation only — the row keeps its type's lifecycle, authorship and
// unread semantics, and every IM surface delivers it unchanged.
export const isAgentRenderedNotice = (message: {
  type: string;
  metadata?: Record<string, unknown> | null;
}): boolean => {
  const event = message.metadata?.event;
  return isNotifyMessageType(message.type)
    && typeof event === 'string'
    && specFor(message.type).agentRenderWhenEvents.includes(event);
};

// Whether a row belongs in the chat transcript — the catalog's ``transcript``
// property, the same declaration the server filter on
// ``GET /api/sessions/{id}/messages`` reads, so the live ``message.new`` feed
// appends exactly the rows the initial load shows (assistant / tool_call are
// process log).
//
// Visibility is a function of ``type`` ALONE, which is why the parameter is
// narrowed to ``{ type }``: no caller and no future edit can reach ``author`` /
// ``source`` / ``author_name`` / ``metadata`` from in here to widen the set,
// even by accident. What that replaced was a ``metadata.source === 'show_page'``
// clause standing in for a message type the server did not emit yet; it kept ANY
// row of that origin whatever its type, and a forward annotation carries that
// origin in EVERY state — so a still-``queued`` annotation rendered as a
// delivered bubble while the same row sat in the queue strip. An annotation
// becomes transcript-visible exactly once: when the flush mints it ``annotation``.
export const isTranscriptMessage = (message: { type: string }): boolean => specFor(message.type).transcript;

type TerminalMessageCandidate = {
  type: string;
  metadata?: Record<string, unknown> | null;
};

export const isDetachedCompletionMessage = (message: TerminalMessageCandidate): boolean => {
  const spec = specFor(message.type);
  return message.metadata?.detached === true && spec.detachedCompletion;
};

// Recovery reports a Turn that already ended, even when transport delivery is
// immediate. This provenance removes foreground authority, not Retry eligibility.
const isReplayedFailureNotice = (message: TerminalMessageCandidate): boolean =>
  message.type === 'notify'
  && message.metadata?.event === 'backend_failure'
  && message.metadata.replayed === true;

const activityRoleForMessage = (
  message: TerminalMessageCandidate,
): ReturnType<typeof specFor>['activityRole'] => {
  const spec = specFor(message.type);
  if (isDetachedCompletionMessage(message) || isReplayedFailureNotice(message)) return 'none';
  const event = message.metadata?.event;
  if (typeof event === 'string' && spec.terminalWhenEvents.includes(event)) return 'terminal';
  return spec.activityRole;
};

// A phase boundary uses the muted Agent presentation. The detached-result case
// keeps rows written by older servers on that presentation; notification types
// remain status pills even when detached.
export const isBoundaryMessage = (message: TerminalMessageCandidate): boolean => {
  const spec = specFor(message.type);
  return spec.render === 'agent' && (
    activityRoleForMessage(message) === 'boundary' || isDetachedCompletionMessage(message)
  );
};

type TerminalAgentMessageCandidate = TerminalMessageCandidate & { author: string };

export function isBackendFailureNotice(
  message: TerminalAgentMessageCandidate & { source?: string | null },
): boolean {
  const metadata = message.metadata;
  return message.type === 'notify'
    && message.author === 'agent'
    && message.source === 'agent'
    && metadata?.event === 'backend_failure'
    && typeof metadata.failure_id === 'string'
    && !!metadata.failure_id;
}

// This is action eligibility, not transcript visibility. The server additionally
// rechecks the durable failed-Turn boundary and current Session authority. A
// Turn whose running commands continue as Watches (``watch_ids``) is not retried:
// that would run them twice.
export function isRetryableFailureNotice(
  message: TerminalAgentMessageCandidate & { source?: string | null },
): boolean {
  const metadata = message.metadata;
  return isBackendFailureNotice(message)
    && typeof metadata?.turn_id === 'string'
    && !!metadata.turn_id
    && !metadata.detached
    && !(Array.isArray(metadata.watch_ids) && metadata.watch_ids.length > 0);
}

// A notice whose Agent cannot run (disabled, deleted, or its backend unavailable), which
// offers to move the session to Vibey and run the failed message there
// (``failure_next_step: continue_on_vibey``). That action replaces the plain Retry, which
// would only fail the same way again.
export function offersContinueOnVibey(
  message: TerminalAgentMessageCandidate & { source?: string | null },
): boolean {
  return isRetryableFailureNotice(message) && message.metadata?.failure_next_step === 'continue_on_vibey';
}

// The Retry states in which the notice's continuation is admitted: it was queued,
// claimed, or accepted into a Turn. ``reserved`` and ``retired`` are not.
export const ADMITTED_RETRY_STATES: readonly string[] = ['queued', 'claimed', 'accepted'];

// A failure notice whose Retry was admitted. The failure no longer ends the visible
// response: a retry continues it, and Vibey's retry writes no input row after the
// notice (it resumes the transcript), so the notice can stay the last row while the
// retried work runs. ``failure_retry.state`` is derived on the server from the
// retry's Delivery, so it holds on reload as well as live.
export function isRetriedFailureNotice(
  message: TerminalAgentMessageCandidate & { source?: string | null; content?: unknown },
): boolean {
  if (!isBackendFailureNotice(message)) return false;
  const content = message.content;
  const action = content && typeof content === 'object'
    ? (content as { failure_retry?: { state?: unknown } }).failure_retry
    : undefined;
  return typeof action?.state === 'string' && ADMITTED_RETRY_STATES.includes(action.state);
}

// A terminal row the TRANSCRIPT shows: the catalog's terminal activity role
// intersected with transcript visibility, plus the conditional terminals that only
// settle a turn for specific metadata events (``notify`` + ``backend_failure``). A
// ``result`` is a reply, not a terminal: a Turn may reply more than once and ends
// at ``turn.end`` (docs/plans/reply-and-turn-end.md).
export const isTerminalAgentMessage = (message: TerminalAgentMessageCandidate): boolean => {
  if (message.author !== 'agent') return false;
  const spec = specFor(message.type);
  return spec.transcript && activityRoleForMessage(message) === 'terminal';
};

// A reply or a phase boundary closes the current Activity group without settling
// the Turn. Presentation's isBoundaryMessage also includes detached legacy
// completions, which must not invalidate the foreground group's live rows or
// hydration; it excludes replies, which draw as the Agent's answer.
const isActivityPhaseEnd = (role: ReturnType<typeof specFor>['activityRole']): boolean =>
  role === 'boundary' || role === 'reply';

export const isAgentActivityBoundaryMessage = (message: TerminalAgentMessageCandidate): boolean =>
  message.author === 'agent' && isActivityPhaseEnd(activityRoleForMessage(message));

// Historical failures and detached completions refresh durable Activity without
// affecting its live generation. Only current terminal replies settle the Turn.
export const shouldRefreshAgentActivityForMessage = (
  message: TerminalAgentMessageCandidate,
): boolean => {
  if (message.author !== 'agent') return false;
  const role = activityRoleForMessage(message);
  return isActivityPhaseEnd(role) || role === 'terminal'
    || isDetachedCompletionMessage(message) || isReplayedFailureNotice(message);
};
