import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  clipVoiceContext,
  loadVoiceContext,
  MAX_VOICE_CONTEXT_CHARS,
  resolveVoiceContext,
  VOICE_CONTEXT_WAIT_MS,
  voiceContextFromMessages,
} from './voiceContext';

const row = (author: string, type: string, text: string, content: Record<string, unknown> = {}) => (
  { author, type, text, content }
);

afterEach(() => {
  vi.useRealTimers();
});

describe('voiceContextFromMessages', () => {
  it("reads the newest Agent reply body and the user's newest message", () => {
    expect(voiceContextFromMessages([
      row('user', 'user', 'first question'),
      row('agent', 'result', 'first answer'),
      row('user', 'user', 'deploy it'),
      row('agent', 'tool_call', 'running make'),
      row('agent', 'result', 'Run make release on prod-7.\n\n✅ 12s', { result_footer: '✅ 12s' }),
      row('agent', 'notify', 'Turn finished'),
    ])).toEqual({ reply: 'Run make release on prod-7.', userPrevious: 'deploy it' });
  });

  it('keeps the previous reply while a Turn runs on the newest message', () => {
    expect(voiceContextFromMessages([
      row('agent', 'result', 'Old reply'),
      row('user', 'user', 'and then?'),
    ])).toEqual({ reply: 'Old reply', userPrevious: 'and then?' });
  });

  it('offers nothing for an empty conversation, and only what a new one has', () => {
    expect(voiceContextFromMessages([])).toEqual({});
    expect(voiceContextFromMessages([row('user', 'user', 'hello')])).toEqual({ userPrevious: 'hello' });
  });

  it('does not reach past the newest reply when its body is only a footer', () => {
    expect(voiceContextFromMessages([
      row('agent', 'result', 'An older reply'),
      row('agent', 'result', '✅ ⏱️ 12s'),
    ])).toEqual({});
  });
});

describe('clipVoiceContext', () => {
  it('keeps the newest characters of a long text', () => {
    const reply = `old${'x'.repeat(MAX_VOICE_CONTEXT_CHARS)}`;
    expect(clipVoiceContext({ reply }).reply).toBe('x'.repeat(MAX_VOICE_CONTEXT_CHARS));
  });

  it('never starts on half of a surrogate pair', () => {
    const userPrevious = `${'😀'.repeat(MAX_VOICE_CONTEXT_CHARS / 2)}a`;
    const clipped = clipVoiceContext({ userPrevious }).userPrevious!;
    expect(clipped.length).toBe(MAX_VOICE_CONTEXT_CHARS - 1);
    expect(clipped.startsWith('😀')).toBe(true);
    expect(clipped.endsWith('a')).toBe(true);
  });

  it('drops blank texts', () => {
    expect(clipVoiceContext({ reply: '  ', userPrevious: '\n' })).toEqual({});
  });
});

describe('resolveVoiceContext', () => {
  it('waits for a context still loading, then goes without it', async () => {
    vi.useFakeTimers();
    const pending = resolveVoiceContext(new Promise(() => undefined));
    await vi.advanceTimersByTimeAsync(VOICE_CONTEXT_WAIT_MS);
    await expect(pending).resolves.toEqual({});
  });

  it('turns a failed load into no context', async () => {
    await expect(resolveVoiceContext(Promise.reject(new Error('offline')))).resolves.toEqual({});
  });
});

describe('loadVoiceContext', () => {
  it("reads the conversation's newest rows, and a failure is no context", async () => {
    const list = vi.fn()
      .mockResolvedValueOnce({ messages: [row('user', 'user', 'hi'), row('agent', 'result', 'hello')] })
      .mockRejectedValueOnce(new Error('404'));
    await expect(loadVoiceContext(list, 'ses_pm')).resolves.toEqual({ reply: 'hello', userPrevious: 'hi' });
    expect(list).toHaveBeenCalledWith('ses_pm', { tail: true, limit: 20, cache: false });
    await expect(loadVoiceContext(list, 'ses_pm')).resolves.toEqual({});
  });
});
