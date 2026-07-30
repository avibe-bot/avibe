import { describe, expect, it, vi } from 'vitest';

import {
  applyVoiceInsertion,
  cleanupVoiceTranscript,
  voiceInsertionSnapshot,
  voiceInsertionText,
} from './voiceCleanup';

describe('voice cleanup', () => {
  it('captures bounded context around the original selection', () => {
    const text = `${'a'.repeat(600)}SELECTED${'b'.repeat(300)}`;
    const snapshot = voiceInsertionSnapshot(text, 600, 608);

    expect(snapshot.before).toBe('a'.repeat(500));
    expect(snapshot.after).toBe('b'.repeat(200));
    expect(snapshot).toMatchObject({ text, start: 600, end: 608 });
  });

  it('replaces the original selection and adds only required Latin spaces', () => {
    const snapshot = voiceInsertionSnapshot('Write old text today', 6, 14);

    expect(voiceInsertionText(snapshot.text, snapshot, 'new plan')).toBe('new plan');
    expect(applyVoiceInsertion(snapshot.text, snapshot, 'new plan')).toBe('Write new plan today');
  });

  it('does not add spaces at CJK boundaries', () => {
    const snapshot = voiceInsertionSnapshot('请旧内容确认', 1, 4);

    expect(applyVoiceInsertion(snapshot.text, snapshot, '后天发布')).toBe('请后天发布确认');
  });

  it('refuses to insert when the draft no longer matches the snapshot', () => {
    const snapshot = voiceInsertionSnapshot('original', 8, 8);

    expect(applyVoiceInsertion('changed', snapshot, 'voice')).toBeNull();
  });

  it('sends only bounded context and returns the cleaned text', async () => {
    const cloudFetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ text: '后天发布。' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const snapshot = voiceInsertionSnapshot('计划：。', 3, 3);

    await expect(cleanupVoiceTranscript('呃后天发布', snapshot, { cloudFetch })).resolves.toMatchObject({
      text: '后天发布。',
      outcome: 'success',
    });
    const [, init] = cloudFetch.mock.calls[0];
    expect(JSON.parse(String(init?.body))).toEqual({
      transcript: '呃后天发布',
      before: '计划：',
      after: '。',
    });
  });

  it('keeps raw ASR text when cleanup is unavailable or malformed', async () => {
    const snapshot = voiceInsertionSnapshot('', 0, 0);
    const unavailable = vi.fn().mockResolvedValue(new Response('{}', { status: 404 }));
    const malformed = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    const networkError = vi.fn().mockRejectedValue(new TypeError('network unavailable'));

    await expect(cleanupVoiceTranscript('raw', snapshot, { cloudFetch: unavailable })).resolves.toMatchObject({
      text: 'raw',
      outcome: 'fallback',
    });
    await expect(cleanupVoiceTranscript('raw', snapshot, { cloudFetch: malformed })).resolves.toMatchObject({
      text: 'raw',
      outcome: 'fallback',
    });
    await expect(cleanupVoiceTranscript('raw', snapshot, { cloudFetch: networkError })).resolves.toMatchObject({
      text: 'raw',
      outcome: 'fallback',
    });
  });

  it('preserves intentional empty output but falls back from whitespace-only output', async () => {
    const snapshot = voiceInsertionSnapshot('', 0, 0);
    const empty = vi.fn().mockResolvedValue(new Response(JSON.stringify({ text: '' }), { status: 200 }));
    const whitespace = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ text: '   ' }), { status: 200 }),
    );

    await expect(cleanupVoiceTranscript('raw', snapshot, { cloudFetch: empty })).resolves.toMatchObject({
      text: '',
      outcome: 'success',
    });
    await expect(cleanupVoiceTranscript('raw', snapshot, { cloudFetch: whitespace })).resolves.toMatchObject({
      text: 'raw',
      outcome: 'fallback',
    });
  });
});
