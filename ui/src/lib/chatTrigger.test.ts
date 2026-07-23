import { describe, expect, it } from 'vitest';

import { chatTriggerLink } from './chatTrigger';

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

describe('chatTriggerLink', () => {
  it('returns null for non-harness messages', () => {
    expect(chatTriggerLink(msg({ source: 'user' }))).toBeNull();
    expect(chatTriggerLink(msg({ source: 'agent' }))).toBeNull();
  });

  it('A9a: agent-callback links to the source session chat, labelled by title prefix', () => {
    const link = chatTriggerLink(
      msg({ author_name: 'agent_run', source_session_id: 'ses_src_1234', source_session_title: 'Vaults 总控' }),
    );
    expect(link).toEqual({ kind: 'source', to: '/chat/ses_src_1234', label: 'Vaults 总控' });
  });

  it('A9a: truncates a long source title to ~12 chars', () => {
    const link = chatTriggerLink(
      msg({ source_session_id: 'ses_x', source_session_title: '0123456789ABCDEF' }),
    );
    expect(link).toEqual({ kind: 'source', to: '/chat/ses_x', label: '0123456789AB…' });
  });

  it('A9a: falls back to agent name + short id when the title is null', () => {
    const link = chatTriggerLink(
      msg({ source_session_id: 'ses_abcdef123456', source_session_title: null, source_session_agent_name: 'pm' }),
    );
    expect(link).toEqual({ kind: 'source', to: '/chat/ses_abcdef123456', label: 'pm · 123456' });
  });

  it('A9b: watch trigger links to the Harness watches tab filtered to this session', () => {
    expect(chatTriggerLink(msg({ author_name: 'watch', author_id: 'def_w', session_id: 'ses_1' }))).toEqual({
      kind: 'harness',
      to: '/harness?tab=watches&session=ses_1',
    });
  });

  it('A9b: scheduled + task_run link to the Harness tasks tab', () => {
    expect(chatTriggerLink(msg({ author_name: 'scheduled', session_id: 'ses_1' }))?.to).toBe(
      '/harness?tab=tasks&session=ses_1',
    );
    expect(chatTriggerLink(msg({ author_name: 'task_run', session_id: 'ses_1' }))?.to).toBe(
      '/harness?tab=tasks&session=ses_1',
    );
  });

  it('does not navigate for webhook or unknown harness kinds without a source', () => {
    expect(chatTriggerLink(msg({ author_name: 'webhook' }))).toBeNull();
    expect(chatTriggerLink(msg({ author_name: 'agent_run', source_session_id: null }))).toBeNull();
  });

  it('prefers the source link over the kind link when both could apply', () => {
    // A resolved source session always wins (agent-callback provenance).
    const link = chatTriggerLink(msg({ author_name: 'watch', source_session_id: 'ses_src' }));
    expect(link?.kind).toBe('source');
  });
});
