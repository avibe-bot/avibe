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

  it('preserves whitespace consumed by a selected range', () => {
    const trailingSpace = voiceInsertionSnapshot('Write old today', 6, 10);
    const surroundingSpaces = voiceInsertionSnapshot('Write old today', 5, 10);

    expect(applyVoiceInsertion(trailingSpace.text, trailingSpace, 'new')).toBe('Write new today');
    expect(applyVoiceInsertion(surroundingSpaces.text, surroundingSpaces, 'new')).toBe(
      'Write new today',
    );
  });

  it('does not add spaces at CJK boundaries', () => {
    const snapshot = voiceInsertionSnapshot('请旧内容确认', 1, 4);

    expect(applyVoiceInsertion(snapshot.text, snapshot, '后天发布')).toBe('请后天发布确认');
  });

  it('does not add spaces at Southeast Asian script boundaries', () => {
    const snapshot = voiceInsertionSnapshot('กข', 1, 1);

    expect(applyVoiceInsertion(snapshot.text, snapshot, 'ค')).toBe('กคข');
  });

  it('adds spaces at Latin mention boundaries without changing CJK boundaries', () => {
    const beforeLatinMention = voiceInsertionSnapshot('Ask @<Alice>', 4, 4);
    const afterLatinMention = voiceInsertionSnapshot('Ask @<Alice>', 12, 12);
    const beforeCjkMention = voiceInsertionSnapshot('问@<小明>', 1, 1);

    expect(applyVoiceInsertion(beforeLatinMention.text, beforeLatinMention, 'please tell')).toBe(
      'Ask please tell @<Alice>',
    );
    expect(applyVoiceInsertion(afterLatinMention.text, afterLatinMention, 'now')).toBe(
      'Ask @<Alice> now',
    );
    expect(applyVoiceInsertion(beforeCjkMention.text, beforeCjkMention, '一下')).toBe('问一下@<小明>');
  });

  it('uses visible mention titles instead of serialized ids for spacing', () => {
    const cjkSession = voiceInsertionSnapshot('Ask #<session-id>', 4, 4, { right: '项' });
    const latinSession = voiceInsertionSnapshot('Ask #<session-id>', 4, 4, { right: 'P' });
    const afterCjkSession = voiceInsertionSnapshot('#<session-id>', 13, 13, { left: '目' });

    expect(applyVoiceInsertion(cjkSession.text, cjkSession, 'check')).toBe('Ask check#<session-id>');
    expect(applyVoiceInsertion(latinSession.text, latinSession, 'check')).toBe('Ask check #<session-id>');
    expect(applyVoiceInsertion(afterCjkSession.text, afterCjkSession, 'now')).toBe('#<session-id>now');
  });

  it('adds Latin boundary spaces outside terminal and opening punctuation', () => {
    const beforeWord = voiceInsertionSnapshot('Send today', 5, 5);
    const afterLabel = voiceInsertionSnapshot('Note:publish', 5, 5);
    const beforeQuotedWord = voiceInsertionSnapshot('Say "today"', 4, 4);

    expect(applyVoiceInsertion(beforeWord.text, beforeWord, 'the update.')).toBe(
      'Send the update. today',
    );
    expect(applyVoiceInsertion(afterLabel.text, afterLabel, 'now')).toBe('Note: now publish');
    expect(applyVoiceInsertion(beforeQuotedWord.text, beforeQuotedWord, 'please')).toBe(
      'Say please "today"',
    );
  });

  it('adds spaces outside symbol-delimited code and command tokens', () => {
    const beforeWord = voiceInsertionSnapshot('Use today', 4, 4);
    const betweenWords = voiceInsertionSnapshot('Runnow', 3, 3);
    const sentencePunctuation = voiceInsertionSnapshot('Say hello', 3, 3);

    expect(applyVoiceInsertion(beforeWord.text, beforeWord, 'C++')).toBe('Use C++ today');
    expect(applyVoiceInsertion(betweenWords.text, betweenWords, '--verbose')).toBe(
      'Run --verbose now',
    );
    expect(applyVoiceInsertion(sentencePunctuation.text, sentencePunctuation, ', actually')).toBe(
      'Say, actually hello',
    );
  });

  it('treats combining marks as part of boundary words', () => {
    const afterNfdWord = voiceInsertionSnapshot('cafe\u0301today', 5, 5);
    const afterArabicWord = voiceInsertionSnapshot('عربي\u0651next', 5, 5);
    const beforeNfdWord = voiceInsertionSnapshot('Trytoday', 3, 3);

    expect(applyVoiceInsertion(afterNfdWord.text, afterNfdWord, 'notes')).toBe('cafe\u0301 notes today');
    expect(applyVoiceInsertion(afterArabicWord.text, afterArabicWord, 'notes')).toBe(
      'عربي\u0651 notes next',
    );
    expect(applyVoiceInsertion(beforeNfdWord.text, beforeNfdWord, 'cafe\u0301')).toBe(
      'Try cafe\u0301 today',
    );
  });

  it('preserves adjacency inside paired delimiters', () => {
    const insideParentheses = voiceInsertionSnapshot('print()', 6, 6);
    const insideQuotes = voiceInsertionSnapshot('Say ""', 5, 5);
    const afterClosedQuote = voiceInsertionSnapshot('Say "hi"today', 8, 8);

    expect(applyVoiceInsertion(insideParentheses.text, insideParentheses, 'value')).toBe(
      'print(value)',
    );
    expect(applyVoiceInsertion(insideQuotes.text, insideQuotes, 'hello')).toBe('Say "hello"');
    expect(applyVoiceInsertion(afterClosedQuote.text, afterClosedQuote, 'again')).toBe(
      'Say "hi" again today',
    );
  });

  it('preserves adjacency around code and path delimiters', () => {
    const memberCall = voiceInsertionSnapshot('object.()', 7, 7);
    const pathSegment = voiceInsertionSnapshot('/usr/bin', 5, 5);
    const sentence = voiceInsertionSnapshot('Done.today', 5, 5);

    expect(applyVoiceInsertion(memberCall.text, memberCall, 'method')).toBe('object.method()');
    expect(applyVoiceInsertion(pathSegment.text, pathSegment, 'local/')).toBe('/usr/local/bin');
    expect(applyVoiceInsertion(sentence.text, sentence, 'again')).toBe('Done. again today');
  });

  it('replaces selected token text without introducing spaces', () => {
    const snapshot = voiceInsertionSnapshot('v12beta', 1, 3);

    expect(applyVoiceInsertion(snapshot.text, snapshot, '13')).toBe('v13beta');
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
