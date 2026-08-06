import { specFor } from './messageTypes';

// Status pills rather than authored answers — the catalog's ``status`` render kind.
export const isNotifyMessageType = (type: string): boolean => specFor(type).render === 'status';

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
  author: string;
  type: string;
  metadata?: Record<string, unknown> | null;
};

// A terminal reply the TRANSCRIPT shows: the catalog's terminal activity role
// intersected with transcript visibility (``silent`` is terminal for activity
// bookkeeping but never rendered), plus the conditional terminals that only settle
// a turn for specific metadata events (``notify`` + ``backend_failure``).
export const isTerminalAgentMessage = (message: TerminalMessageCandidate): boolean => {
  if (message.author !== 'agent') return false;
  const spec = specFor(message.type);
  if (spec.transcript && spec.activityRole === 'terminal') return true;
  const event = message.metadata?.event;
  return typeof event === 'string' && spec.terminalWhenEvents.includes(event);
};
