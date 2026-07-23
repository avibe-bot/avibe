import { describe, expect, it } from 'vitest';

import { chatTriggerLink, isUnresolvedAgentCallback } from './chatTrigger';

type Msg = Parameters<typeof chatTriggerLink>[0];
const msg = (over: Partial<Msg>): Msg => ({
  source: 'harness',
  author_name: null,
  author_id: null,
  session_id: 'ses_here',
  source_session_id: null,
  source_session_title: null,
  source_session_agent_name: null,
  ...over,
});
// Second arg is the localized "agent" fallback word; a fixed literal keeps the
// mapper translation-free and the test deterministic.
const link = (over: Partial<Msg>) => chatTriggerLink(msg(over), 'agent');

describe('chatTriggerLink', () => {
  it('returns null for non-harness messages', () => {
    expect(link({ source: 'user' })).toBeNull();
    expect(link({ source: 'agent' })).toBeNull();
  });

  it('A9a: agent-callback links to the source session chat, labelled by title prefix', () => {
    expect(link({ author_name: 'agent_run', source_session_id: 'ses_src_1234', source_session_title: 'Vaults 总控' })).toEqual({
      kind: 'source',
      to: '/chat/ses_src_1234',
      label: 'Vaults 总控',
    });
  });

  it('A9a: truncates a long source title to ~12 chars', () => {
    expect(link({ source_session_id: 'ses_x', source_session_title: '0123456789ABCDEF' })).toEqual({
      kind: 'source',
      to: '/chat/ses_x',
      label: '0123456789AB…',
    });
  });

  it('A9a: falls back to agent name + short id when the title is null', () => {
    expect(link({ source_session_id: 'ses_abcdef123456', source_session_title: null, source_session_agent_name: 'pm' })).toEqual({
      kind: 'source',
      to: '/chat/ses_abcdef123456',
      label: 'pm · 123456',
    });
  });

  it('A9a: uses the localized agent fallback when both title and agent name are null', () => {
    expect(link({ source_session_id: 'ses_abcdef123456' })?.label).toBe('agent · 123456');
  });

  it('A9b: watch trigger links to the Harness watches tab filtered to this session', () => {
    expect(link({ author_name: 'watch', author_id: 'def_w', session_id: 'ses_1' })).toEqual({
      kind: 'harness',
      to: '/harness?tab=watches&session=ses_1',
    });
  });

  it('A9b: scheduled + task_run + legacy task all link to the Harness tasks tab', () => {
    for (const author_name of ['scheduled', 'task_run', 'task']) {
      expect(link({ author_name, session_id: 'ses_1' })?.to).toBe('/harness?tab=tasks&session=ses_1');
    }
  });

  it('does not navigate for webhook or unknown harness kinds without a source', () => {
    expect(link({ author_name: 'webhook' })).toBeNull();
    expect(link({ author_name: 'agent_run', source_session_id: null })).toBeNull();
  });

  it('prefers the source link over the kind link when both could apply', () => {
    expect(link({ author_name: 'watch', source_session_id: 'ses_src' })?.kind).toBe('source');
  });
});

describe('isUnresolvedAgentCallback', () => {
  const m = (over: Partial<Parameters<typeof isUnresolvedAgentCallback>[0]>) =>
    isUnresolvedAgentCallback({ source: 'harness', native_message_id: 'agent_run:exec1', source_session_id: null, ...over });

  it('is true for a live agent_run harness message without a resolved source', () => {
    expect(m({})).toBe(true);
  });

  it('is false once the source session is resolved', () => {
    expect(m({ source_session_id: 'ses_src' })).toBe(false);
  });

  it('is false for non-agent_run harness messages and non-harness messages', () => {
    expect(m({ native_message_id: 'scheduled:def:exec' })).toBe(false);
    expect(m({ native_message_id: null })).toBe(false);
    expect(m({ source: 'agent' })).toBe(false);
  });
});
