import { describe, expect, it } from 'vitest';

import {
  applyVoiceInsertion,
  applyVoiceInsertionWithSnapshot,
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

  it('keeps successive realtime previews in one replaceable draft range', () => {
    const original = voiceInsertionSnapshot('Plan today', 5, 5);
    const first = applyVoiceInsertionWithSnapshot(original.text, original, 'the lau');
    const second = first && applyVoiceInsertionWithSnapshot(
      first.text,
      first.snapshot,
      'the launch',
      original,
    );
    const final = second && applyVoiceInsertionWithSnapshot(
      second.text,
      second.snapshot,
      'The launch is tomorrow.',
      original,
    );

    expect(first?.text).toBe('Plan the lau today');
    expect(second?.text).toBe('Plan the launch today');
    expect(final?.text).toBe('Plan The launch is tomorrow. today');
    expect(final?.snapshot).toMatchObject({
      text: 'Plan The launch is tomorrow. today',
      start: 5,
      end: 29,
    });
  });

  it('recomputes final boundary spacing from the original caret', () => {
    const original = voiceInsertionSnapshot('Say hello', 3, 3);
    const preview = applyVoiceInsertionWithSnapshot(original.text, original, 'actually');
    const final = preview && applyVoiceInsertionWithSnapshot(
      preview.text,
      preview.snapshot,
      ', actually',
      original,
    );

    expect(preview?.text).toBe('Say actually hello');
    expect(final?.text).toBe('Say, actually hello');
  });

});
