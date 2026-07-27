import { specFor } from './messageTypes';

// Status pills rather than authored answers — the catalog's ``status`` render kind.
export const isNotifyMessageType = (type: string): boolean => specFor(type).render === 'status';

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
