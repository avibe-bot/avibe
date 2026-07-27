import { describe, expect, it } from 'vitest';

import catalog from '../../../vibe/message_types.json';
import { messageSearchRole } from '../components/workbench/search/messageSearchRole';
import { isActivityMessageType } from './agentActivity';
import { isNotifyMessageType, isTerminalAgentMessage } from './chatMessageTypes';
import { messageTypeNames, specFor } from './messageTypes';

describe('message type catalog reader', () => {
  it('reads the same tracked file the Python readers read', () => {
    // Not a re-declaration: the assertion compares the reader against the very
    // bytes it loaded, which is what makes a hand-kept TypeScript copy impossible.
    expect(messageTypeNames()).toEqual(Object.keys(catalog.types));
  });

  it('resolves every declared property, defaults included', () => {
    // ``user`` declares 6 of the properties; the other 7 must come from defaults.
    expect(Object.keys(specFor('user'))).toEqual(Object.keys(catalog.defaults));
    expect(specFor('user').transcript).toBe(true);
    expect(specFor('user').unread).toBe(false);
  });

  it('gives unknown types the catalog defaults', () => {
    expect(specFor('a_type_the_catalog_never_declares')).toEqual(specFor(''));
    expect(specFor('a_type_the_catalog_never_declares')).toMatchObject(catalog.defaults);
  });
});

// ===== One-time equivalence oracle =====
// The five predicates as they read BEFORE this lane replaced their inline type-name
// lists with catalog lookups. This is frozen historical behavior, NOT a catalog
// mirror to maintain: it is asserted only over ``PRE_REFACTOR_TYPES`` — the names
// that existed at the refactor — so a type added to the catalog later is out of its
// scope by construction. A failure here means a catalog value and the frontend's
// former behavior disagree, which is a drift finding to raise, not a snapshot to
// quietly update.
const PRE_REFACTOR_TYPES = [
  'user',
  'harness',
  'result',
  'notify',
  'error',
  'assistant',
  'tool_call',
  'queued',
  'draft',
  'pending',
  'harness_dedupe',
  'silent',
] as const;

// Unknown / non-type strings the predicates must also agree on. ``show_annotation``
// is one of them on purpose: it is an ``author_name``, never a message type, and
// keeping it here pins that it does not become one by the back door.
//
// ``annotation`` is deliberately absent. This block is an equivalence oracle
// against pre-refactor behavior, and ``annotation`` is behavior that did not
// exist then — it had no type, so the transcript reached it through
// ``metadata.source``. Its behavior is pinned by the tests that own it
// (chatMessageTypes / AnnotationMessage), not by an oracle it postdates.
const PROBE_TYPES: readonly string[] = [...PRE_REFACTOR_TYPES, 'show_annotation', 'future_type', ''];

const wasTranscript = (type: string): boolean =>
  type === 'user' ||
  type === 'harness' ||
  type === 'result' ||
  type === 'error' ||
  type === 'notify';

const wasNotify = (type: string): boolean => type === 'notify' || type === 'error';

const wasActivity = (type: string): boolean => type === 'assistant' || type === 'tool_call';

const wasHarnessInputType = (type: string): boolean => type === 'harness';

type TerminalCandidate = { author: string; type: string; metadata?: Record<string, unknown> | null };

const wasTerminalAgentMessage = (message: TerminalCandidate): boolean =>
  message.author === 'agent' &&
  (message.type === 'result' ||
    message.type === 'error' ||
    (message.type === 'notify' && message.metadata?.event === 'backend_failure'));

describe('catalog-derived predicates match pre-refactor behavior', () => {
  it('transcript visibility (ChatPage isTranscriptMessage)', () => {
    // ``isTranscriptMessage`` is now ``specFor(type).transcript`` and nothing
    // else — the ``metadata.source`` side channel it used to be OR'd with is
    // gone — so for these types the catalog property IS the predicate.
    for (const type of PROBE_TYPES) {
      expect(specFor(type).transcript, type).toBe(wasTranscript(type));
    }
  });

  it('status-pill rendering (isNotifyMessageType)', () => {
    for (const type of PROBE_TYPES) {
      expect(isNotifyMessageType(type), type).toBe(wasNotify(type));
    }
  });

  it('activity-step identity (isActivityMessageType)', () => {
    for (const type of PROBE_TYPES) {
      expect(isActivityMessageType(type), type).toBe(wasActivity(type));
    }
  });

  it('harness input-turn identity (messageSearchRole type branch)', () => {
    for (const type of PROBE_TYPES) {
      // Neutral author/source so the type test alone decides the role.
      const match = { author: 'agent', source: 'agent', type };
      expect(messageSearchRole(match), type).toBe(wasHarnessInputType(type) ? 'automated' : 'agent');
      expect(specFor(type).inputAuthors.includes('harness'), type).toBe(wasHarnessInputType(type));
    }
  });

  it('visible terminal replies (isTerminalAgentMessage), across authors and events', () => {
    const metadataCases: Array<Record<string, unknown> | null | undefined> = [
      undefined,
      null,
      {},
      { event: 'backend_failure' },
      { event: 'activity_completed' },
      { event: 42 },
    ];
    for (const type of PROBE_TYPES) {
      for (const author of ['agent', 'user', 'system', 'harness']) {
        for (const metadata of metadataCases) {
          const message: TerminalCandidate = { author, type, metadata };
          expect(isTerminalAgentMessage(message), `${author}/${type}/${JSON.stringify(metadata)}`).toBe(
            wasTerminalAgentMessage(message),
          );
        }
      }
    }
  });

  it('keeps silent out of the visible-terminal set even though it is terminal', () => {
    // ``silent`` carries ``activityRole: terminal`` for activity bookkeeping but is
    // not transcript-visible, which is why the visible-terminal predicate is the
    // intersection of the two properties rather than the role alone.
    expect(specFor('silent').activityRole).toBe('terminal');
    expect(specFor('silent').transcript).toBe(false);
    expect(isTerminalAgentMessage({ author: 'agent', type: 'silent' })).toBe(false);
  });
});
