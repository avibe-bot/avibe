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

// Operational state and process events no longer masquerade as Session Messages.
// Keep this list as a migration boundary: these names must resolve to neutral catalog
// defaults even when an old browser or deep link presents one.
const RETIRED_PSEUDO_MESSAGE_TYPES = [
  'tool_call',
  'queued',
  'draft',
  'pending',
  'harness_dedupe',
  'silent',
] as const;

const CANONICAL_MESSAGE_TYPES = [
  'user',
  'harness',
  'agent_initiated',
  'annotation',
  'output',
  'result',
  'notify',
  'error',
  'assistant',
] as const;

const PROBE_TYPES: readonly string[] = [
  ...CANONICAL_MESSAGE_TYPES,
  ...RETIRED_PSEUDO_MESSAGE_TYPES,
  'show_annotation',
  'future_type',
  '',
];

const wasTranscript = (type: string): boolean =>
  type === 'user' ||
  type === 'harness' ||
  type === 'annotation' ||
  type === 'output' ||
  type === 'result' ||
  type === 'error' ||
  type === 'notify';

const wasNotify = (type: string): boolean => type === 'notify' || type === 'error';

const wasActivity = (type: string): boolean => type === 'assistant';

const wasHarnessInputType = (type: string): boolean =>
  type === 'harness' || type === 'agent_initiated' || type === 'annotation';

type TerminalCandidate = { author: string; type: string; metadata?: Record<string, unknown> | null };

const wasTerminalAgentMessage = (message: TerminalCandidate): boolean =>
  message.author === 'agent' &&
  (message.type === 'result' ||
    message.type === 'error' ||
    (message.type === 'notify' && message.metadata?.event === 'backend_failure'));

describe('catalog-derived predicates match the communication-record boundary', () => {
  it('does not declare retired pseudo-message types', () => {
    for (const type of RETIRED_PSEUDO_MESSAGE_TYPES) {
      expect(messageTypeNames(), type).not.toContain(type);
      expect(specFor(type), type).toEqual(specFor(''));
    }
  });

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

  it('activity-step identity includes synthetic tool events', () => {
    for (const type of PROBE_TYPES) {
      expect(isActivityMessageType(type), type).toBe(type === 'tool_call' || wasActivity(type));
    }
  });

  it('harness input-turn identity (messageSearchRole type branch)', () => {
    for (const type of PROBE_TYPES) {
      // A genuinely neutral author, so the type test alone decides the role.
      // ``agent`` would not do: an agent-authored row is now attributed to the
      // agent before the catalog is consulted, because a harness-input type can
      // still carry agent-written rows (a reverse annotation is one).
      const match = { author: 'system', source: 'system', type };
      const expectedRole = type === 'annotation' ? 'you' : wasHarnessInputType(type) ? 'automated' : 'agent';
      expect(messageSearchRole(match), type).toBe(expectedRole);
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

  it('treats retired silent markers as neutral unknown types', () => {
    expect(specFor('silent').activityRole).toBe('none');
    expect(specFor('silent').transcript).toBe(false);
    expect(isTerminalAgentMessage({ author: 'agent', type: 'silent' })).toBe(false);
  });
});
