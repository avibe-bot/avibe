import { describe, expect, it } from 'vitest';

import type { WorkbenchMessage } from '@/context/ApiContext';
import { resultFooterParts } from './resultFooter';

const message = (
  text: string,
  content: Record<string, unknown> = {},
  over: Partial<WorkbenchMessage> = {},
) => ({
  author: 'agent',
  type: 'result',
  text,
  content,
  ...over,
}) as WorkbenchMessage;

describe('resultFooterParts', () => {
  it('uses structured content and removes an older folded copy', () => {
    const footer = '✅ ⏱️ 5s · 🪙 1.2k tok';
    const text = `Answer\n\n${footer}`;
    expect(resultFooterParts(message(text, { result_footer: footer }))).toEqual({
      body: 'Answer',
      footer: '⏱️ 5s · 🪙 1.2k tok',
      // Counted in UTF-16 code units, the unit a citation's ranges also use:
      // the coin is a surrogate pair, so this is not the footer's length in
      // characters.
      edits: [{ start: 6, end: text.length, inserted: 0 }],
    });
  });

  it('keeps a clean new message body beside its structured footer', () => {
    const footer = '✅ ⏱️ 2m 24s';
    expect(resultFooterParts(message('Answer', { result_footer: footer }))).toEqual({
      body: 'Answer',
      footer: '⏱️ 2m 24s',
      edits: [],
    });
  });

  it('recognizes legacy duration and token footer shapes', () => {
    for (const [footer, displayedFooter] of [
      ['✅ ⏱️ 0s', '⏱️ 0s'],
      ['⚠️ ⏱️ 2m 4s · 🪙 240k tok', '⚠️ ⏱️ 2m 4s · 🪙 240k tok'],
      ['❌ 🪙 12.3k tok', '❌ 🪙 12.3k tok'],
      ['✅ 🪙 1.4M tok', '🪙 1.4M tok'],
    ]) {
      const text = `Answer\n\n${footer}`;
      expect(resultFooterParts(message(text))).toEqual({
        body: 'Answer',
        footer: displayedFooter,
        edits: [{ start: 6, end: text.length, inserted: 0 }],
      });
    }
  });

  it('moves a standalone generated footer out of a footer-only completion body', () => {
    const footer = '✅ ⏱️ 5s · 🪙 1.2k tok';
    expect(resultFooterParts(message(footer))).toEqual({
      body: '',
      footer: '⏱️ 5s · 🪙 1.2k tok',
      edits: [{ start: 0, end: footer.length, inserted: 0 }],
    });
  });

  it('does not move authored text or non-Agent result content', () => {
    const text = 'Answer\n\n✅ Looks good';
    expect(resultFooterParts(message(text))).toEqual({ body: text, footer: null, edits: [] });
    const coinText = 'Answer\n\n✅ 🪙 deployment complete';
    expect(resultFooterParts(message(coinText)))
      .toEqual({ body: coinText, footer: null, edits: [] });
    expect(resultFooterParts(message('✅ 🪙 deployment complete'))).toEqual({
      body: '✅ 🪙 deployment complete',
      footer: null,
      edits: [],
    });
    expect(resultFooterParts(message(text, {}, { author: 'user', type: 'user' }))).toEqual({
      body: text,
      footer: null,
      edits: [],
    });
  });

  // The stored row is the body a citation's ranges were measured in, and the
  // renderer is handed `body` instead - so `edits` is how those measurements
  // cross this split (see lib/citations). It is only worth carrying if it says
  // what actually happened, which is a property of every case above rather
  // than a case of its own.
  it.each([
    ['a structured footer folded into the text', 'Answer\n\n✅ ⏱️ 5s', { result_footer: '✅ ⏱️ 5s' }],
    ['a structured footer stored apart', 'Answer', { result_footer: '✅ ⏱️ 2m 24s' }],
    ['a legacy footer paragraph', 'Answer\n\n✅ ⏱️ 0s', {}],
    ['a body that is only a footer', '✅ 🪙 1.4M tok', {}],
    ['an emoji body the pattern must not claim', 'Answer\n\n✅ Looks good', {}],
    ['an authored body with no footer at all', 'Just an answer.', {}],
  ])('describes the cut it made in %s', (_why, text, content) => {
    const { body, edits } = resultFooterParts(message(text, content));

    // Every edit here is a removal, so the body is what is left of the stored
    // text once they are taken out - in the stored text's own coordinates.
    let rebuilt = text;
    for (const edit of [...edits].sort((left, right) => right.start - left.start)) {
      expect(edit.inserted).toBe(0);
      rebuilt = rebuilt.slice(0, edit.start) + rebuilt.slice(edit.end);
    }
    expect(rebuilt).toBe(body);
  });
});
