// Which card family draws a transcript row.
//
// A pure mapper so the branching is unit-tested without the component — the same
// reason ``chatTrigger`` is one. It replaces a cascade of booleans read off the
// row in ``MessageRow``, where the ordering between them was the load-bearing
// part and nothing checked it.
//
// The ordering is the whole point. ``type`` is settled FIRST and returns, so a
// message type can never be outvoted by who happens to be recorded as its
// author. A forward annotation is authored by ``harness``: under the old
// author/source-first cascade it drew as a collapsed trigger row behind a
// "定时任务"-style chip, which is how an annotation looked before it had a type.
//
// ``author`` and ``source`` still decide among the ROLE families below, which is
// what they legitimately describe. What they no longer do is decide whether a
// typed row is that type.
import { isNotifyMessageType } from './chatMessageTypes';
import { readAnnotationView, type AnnotationView } from './annotationView';

// A row typed ``annotation`` whose display record is unreadable. Every writer of
// the type emits one, and the migration backfills ``direction: "agent"`` onto the
// historical reverse marks, so reaching this is a backend defect — it is here so
// the defect degrades INSIDE the annotation family (a card with the wrong title)
// instead of escaping it (the user's own words, drawn as if the user typed them).
const UNREADABLE_ANNOTATION: AnnotationView = { direction: 'agent', resolved: false };

// The annotation case carries its view along, so a caller cannot narrow to the
// family and then disagree with a second lookup about what is in it.
export type ChatRowKind =
  | { kind: 'annotation'; annotation: AnnotationView }
  | { kind: 'notify' }
  | { kind: 'agent' }
  | { kind: 'system' }
  | { kind: 'harness' }
  | { kind: 'user' };

type ChatRowFields = {
  type: string;
  author: string;
  source?: string | null;
  content?: unknown;
};

export function chatRowKind(message: ChatRowFields): ChatRowKind {
  // Typed families first, and by ``type`` alone: not ``author``, not ``source``,
  // not ``metadata.source`` (a forward annotation carries ``show_page`` in every
  // state, including while it is still queued and belongs only to the strip).
  if (message.type === 'annotation') {
    return { kind: 'annotation', annotation: readAnnotationView(message.content) ?? UNREADABLE_ANNOTATION };
  }
  // Runtime notifications and legacy error rows are compact status pills, not
  // Agent-authored answers.
  if (isNotifyMessageType(message.type)) return { kind: 'notify' };

  if (message.author === 'agent') return { kind: 'agent' };
  if (message.author === 'system') return { kind: 'system' };
  // A harness-origin row is turn input the human didn't type (scheduled task /
  // watch / webhook); collapsed by default so it doesn't dominate.
  if (message.source === 'harness') return { kind: 'harness' };
  return { kind: 'user' };
}
