export const isNotifyMessageType = (type: string): boolean =>
  type === 'notify' || type === 'error';

// The transcript-visible message types — a literal mirror of the server's
// ``TRANSCRIPT_TYPES`` so the live ``message.new`` feed appends exactly the rows
// the initial load shows (``assistant`` / ``tool_call`` are process log).
const TRANSCRIPT_TYPES = new Set(['user', 'harness', 'result', 'error', 'notify', 'annotation']);

// Whether a row belongs in the chat transcript.
//
// Visibility is a function of ``type`` ALONE — which is why the parameter is
// narrowed to ``{ type }``: no caller and no future edit can reach ``author`` /
// ``source`` / ``author_name`` / ``metadata`` from in here to widen or narrow
// the set, even by accident. The clause this replaced kept any row whose
// ``metadata.source`` was ``show_page`` whatever its type, and a forward
// annotation carries that source in EVERY state — so a still-``queued``
// annotation rendered as a delivered bubble while the same row sat in the queue
// strip. An annotation becomes transcript-visible exactly once: when it is
// minted as ``annotation``.
export const isTranscriptMessage = (message: { type: string }): boolean =>
  TRANSCRIPT_TYPES.has(message.type);

type TerminalMessageCandidate = {
  author: string;
  type: string;
  metadata?: Record<string, unknown> | null;
};

export const isTerminalAgentMessage = (message: TerminalMessageCandidate): boolean =>
  message.author === 'agent' &&
  (message.type === 'result' ||
    message.type === 'error' ||
    (message.type === 'notify' && message.metadata?.event === 'backend_failure'));
