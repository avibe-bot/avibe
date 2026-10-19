import { describe, expect, it } from 'vitest';

import type { WorkbenchMessage } from '@/context/ApiContext';

import { bringsReply } from './usePetSession';

const row = (type: string, overrides: Partial<WorkbenchMessage> = {}): WorkbenchMessage => ({
  id: 'm', session_id: 'S', scope_id: 'scope', author: 'agent', type, text: 'text', metadata: {}, content: {},
  created_at: '2026-10-19T00:00:00Z', updated_at: '2026-10-19T00:00:00Z', read_at: null,
  ...overrides,
} as unknown as WorkbenchMessage);

describe('bringsReply', () => {
  it('counts a new reply, and only a reply, as an arrival', () => {
    expect(bringsReply('new', row('result'), undefined)).toBe(true);
    for (const type of ['assistant', 'tool_call', 'output', 'interim', 'notify', 'user']) {
      expect(bringsReply('new', row(type), undefined)).toBe(false);
    }
    expect(bringsReply('new', row('result', { author: 'user' } as Partial<WorkbenchMessage>), undefined)).toBe(false);
  });

  it('counts an update that promotes a row into the reply, held or not', () => {
    const promoted = row('result', { metadata: { promoted_from: 'output' } } as Partial<WorkbenchMessage>);
    expect(bringsReply('updated', promoted, undefined)).toBe(true);
    expect(bringsReply('updated', promoted, row('output'))).toBe(true);
  });

  it('does not count an edit to a reply already held, nor an update without a promotion', () => {
    const promoted = row('result', { metadata: { promoted_from: 'output' } } as Partial<WorkbenchMessage>);
    expect(bringsReply('updated', promoted, row('result'))).toBe(false);
    expect(bringsReply('updated', row('result'), undefined)).toBe(false);
    expect(bringsReply('updated', row('result'), row('result'))).toBe(false);
    expect(bringsReply('updated', row('tool_call', { metadata: { promoted_from: 'output' } } as Partial<WorkbenchMessage>), undefined)).toBe(false);
  });
});
