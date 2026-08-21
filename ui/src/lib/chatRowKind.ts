// Three adjacent questions about a transcript row, kept apart on purpose:
// ``chatRowKind`` — which card family DRAWS it, ``isAgentAuthored`` — who WROTE
// it, and ``drawsEmptyBodyPlaceholder`` — whether an empty one still needs a
// stand-in body. All three agreed on every row until ``annotation`` arrived,
// which is exactly why they are asked separately now (see the two below).
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
import { isBoundaryMessage, isNotifyMessageType } from './chatMessageTypes';
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
  | { kind: 'boundary' }
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
  metadata?: Record<string, unknown> | null;
};

export function chatRowKind(message: ChatRowFields): ChatRowKind {
  // Typed families first, and by ``type`` alone: not ``author``, not ``source``,
  // not ``metadata.source`` (a forward annotation carries ``show_page`` in every
  // state, including while it is still queued and belongs only to the strip).
  if (message.type === 'annotation') {
    return { kind: 'annotation', annotation: readAnnotationView(message.content) ?? UNREADABLE_ANNOTATION };
  }
  if (isBoundaryMessage(message)) return { kind: 'boundary' };
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

// Did the AGENT write this row's text?
//
// Some Markdown affordances are earned by authorship, not by card family: a
// ``$<NAME>`` marker becomes an interactive secret-input card only in the agent's
// own words, because in anyone else's it would fake an "agent asked for this"
// prompt. Before ``annotation`` existed, "drawn as the agent" and "written by the
// agent" were the same rows, so one flag served both and the distinction cost
// nothing to ignore.
//
// An agent's reverse mark broke that: it draws as an annotation and is still the
// agent talking. Reading authorship off ``chatRowKind`` would silently strip the
// card from it — re-creating, one field over, the same conflation this module
// exists to delete. So this asks ``author`` directly, and equals
// ``kind === 'agent'`` for every row EXCEPT an agent-authored annotation.
export function isAgentAuthored(message: Pick<ChatRowFields, 'type' | 'author'>): boolean {
  // Notify/error rows draw a status pill with no Markdown body at all; excluding
  // them keeps this identical to the pre-annotation flag rather than widening it.
  return !isNotifyMessageType(message.type) && message.author === 'agent';
}

// A row with no text: does it still need the ``—`` stand-in body?
//
// A bubble holding neither text nor attachment would draw as a bare rounded box
// that reads as broken, so the transcript fills it with a muted em dash meaning
// "this row really is empty".
//
// The annotation card is the one that is never bare — it always draws its title,
// and usually the anchor quote — and an empty-text annotation is a SUPPORTED
// shape rather than a defect: a pure highlight, where the quote is the whole of
// what the annotator contributed. Both directions can be empty. So the stand-in
// would not be filling a hole here, it would be adding a body nobody wrote.
export function drawsEmptyBodyPlaceholder(row: ChatRowKind, hasAttachments: boolean): boolean {
  return !hasAttachments && row.kind !== 'annotation';
}
