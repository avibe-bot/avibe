/* @vitest-environment jsdom */

import { createRef } from 'react';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.hoisted(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: false }),
  });
});

import { MentionEditor, type MentionEditorHandle } from './MentionEditor';

afterEach(cleanup);

// The editor owns the marker a picked mention becomes (docs/plans/bots.md D3):
// a Bot's chip shows its display name while the text carries its frozen handle,
// and an Agent's marker names its kind, so the two never read alike.
describe('MentionEditor typed mentions', () => {
  it('serializes a Bot by handle and an Agent by name, each with its kind', async () => {
    const ref = createRef<MentionEditorHandle>();
    const onChange = vi.fn();
    render(
      <MentionEditor
        ref={ref}
        placeholder="Message"
        onChange={onChange}
        onSubmit={vi.fn()}
        onSearchAgents={async () => []}
        onSearchSessions={async () => []}
      />,
    );
    const editor = screen.getByLabelText('Message');
    await waitFor(() => expect(ref.current).not.toBeNull());

    act(() => {
      ref.current?.setText('Ask ');
      ref.current?.insertMention('@', '首席安全官', { kind: 'bot', handle: 'cso' });
    });
    await waitFor(() => expect(editor.querySelector('[data-beautiful-mention="@首席安全官"]')).not.toBeNull());
    act(() => {
      ref.current?.insertMention('@', 'reviewer', { kind: 'agent', agentId: 'agt-reviewer', backend: 'claude' });
    });

    await waitFor(() => {
      const [text, references] = onChange.mock.calls.at(-1)!;
      expect(text).toBe('Ask @<bot:cso> @<agent:reviewer> ');
      expect(references).toEqual([
        { kind: 'bot', handle: 'cso', display_name: '首席安全官' },
        { kind: 'agent', name: 'reviewer', agent_id: 'agt-reviewer', backend: 'claude' },
      ]);
    });
    // The chip reads as the Bot, not as its handle.
    expect(editor.textContent).toContain('@首席安全官');
    expect(editor.textContent).not.toContain('cso');
  });
});
