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

import { voiceInsertionSnapshot } from '../../lib/voiceCleanup';
import { MentionEditor, type MentionEditorHandle } from './MentionEditor';

afterEach(cleanup);

const renderEditor = (initialText = '') => {
  const ref = createRef<MentionEditorHandle>();
  const onChange = vi.fn();
  render(
    <MentionEditor
      ref={ref}
      initialText={initialText}
      placeholder="Message"
      onChange={onChange}
      onSubmit={vi.fn()}
      onSearchAgents={async () => []}
      onSearchSessions={async () => []}
    />,
  );
  return { ref, onChange };
};

describe('MentionEditor voice preview', () => {
  it('replaces successive previews inline before committing the cleaned result', async () => {
    const { ref, onChange } = renderEditor('Plan today');
    const editor = screen.getByLabelText('Message');
    await waitFor(() => expect(editor.textContent).toBe('Plan today'));

    act(() => {
      expect(ref.current?.showVoicePreview(
        voiceInsertionSnapshot('Plan today', 5, 5),
        'the lau',
      )).toBe(true);
    });
    await waitFor(() => expect(editor.textContent).toBe('Plan the lau today'));

    act(() => {
      expect(ref.current?.showVoicePreview(
        voiceInsertionSnapshot('Plan today', 5, 5),
        'the launch',
      )).toBe(true);
    });
    await waitFor(() => expect(editor.textContent).toBe('Plan the launch today'));
    expect(onChange.mock.calls.at(-1)?.[3]).toBe(true);

    act(() => {
      expect(ref.current?.commitVoicePreview(
        voiceInsertionSnapshot('Plan today', 5, 5),
        'The launch is tomorrow.',
      )).toBe(true);
    });
    await waitFor(() => expect(editor.textContent).toBe('Plan The launch is tomorrow. today'));
    expect(onChange.mock.calls.at(-1)?.[3]).toBe(false);
  });

  it('restores the original rich editor state when recording is discarded', async () => {
    const { ref } = renderEditor();
    const editor = screen.getByLabelText('Message');
    await waitFor(() => expect(ref.current).not.toBeNull());

    act(() => {
      ref.current?.setText('Ask ');
      ref.current?.insertMention('@', 'Alice', { agentId: 'agt-alice' });
    });
    await waitFor(() => expect(editor.querySelector('[data-beautiful-mention="@Alice"]')).not.toBeNull());
    const snapshot = ref.current!.captureSelection();

    act(() => {
      expect(ref.current?.showVoicePreview(snapshot, 'the team')).toBe(true);
    });
    await waitFor(() => expect(editor.textContent).toContain('the team'));

    act(() => {
      expect(ref.current?.restoreVoicePreview()).toBe(true);
    });
    await waitFor(() => expect(editor.querySelector('[data-beautiful-mention="@Alice"]')).not.toBeNull());
  });
});
