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
    expect(resultFooterParts(message(`Answer\n\n${footer}`, { result_footer: footer }))).toEqual({
      body: 'Answer',
      footer,
    });
  });

  it('keeps a clean new message body beside its structured footer', () => {
    const footer = '✅ ⏱️ 2m 24s';
    expect(resultFooterParts(message('Answer', { result_footer: footer }))).toEqual({
      body: 'Answer',
      footer,
    });
  });

  it('recognizes legacy duration and token footer shapes', () => {
    for (const footer of [
      '✅ ⏱️ 0s',
      '⚠️ ⏱️ 2m 4s · 🪙 240k tok',
      '❌ 🪙 12.3k tok',
      '✅ 🪙 1.4M tok',
    ]) {
      expect(resultFooterParts(message(`Answer\n\n${footer}`))).toEqual({ body: 'Answer', footer });
    }
  });

  it('moves a standalone generated footer out of a footer-only completion body', () => {
    const footer = '✅ ⏱️ 5s · 🪙 1.2k tok';
    expect(resultFooterParts(message(footer))).toEqual({ body: '', footer });
  });

  it('does not move authored text or non-Agent result content', () => {
    const text = 'Answer\n\n✅ Looks good';
    expect(resultFooterParts(message(text))).toEqual({ body: text, footer: null });
    const coinText = 'Answer\n\n✅ 🪙 deployment complete';
    expect(resultFooterParts(message(coinText))).toEqual({ body: coinText, footer: null });
    expect(resultFooterParts(message('✅ 🪙 deployment complete'))).toEqual({
      body: '✅ 🪙 deployment complete',
      footer: null,
    });
    expect(resultFooterParts(message(text, {}, { author: 'user', type: 'user' }))).toEqual({
      body: text,
      footer: null,
    });
  });
});
