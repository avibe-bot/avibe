// A paragraph whose last line never holds only a word or two (ui/design: settings/devices-*):
// used for the dialogs and consequence notes of the Devices pages.
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

const ADDRESS = /[a-z0-9-]+(?:\.[a-z0-9-]+)+/gi;
/** Longer runs than this may still wrap: a 63-character name must not overflow a phone. */
const MAX_KEPT = 32;

/**
 * A paragraph whose last line never holds only a word or two. Chinese breaks between any two
 * characters and `text-wrap: pretty` does not reliably prevent that, so the tail (at least eight
 * characters in Chinese, or three words, cut at a word boundary) is one unbreakable run. An address
 * is never split across lines: a cut that would fall inside one moves to its start. Which rule
 * applies follows the page's language, not the text (a Chinese device name in English copy).
 */
export function KeptTail({ text }: { text: string }) {
  const { i18n } = useTranslation();
  const chinese = i18n.language.startsWith('zh');
  const addresses = Array.from(text.matchAll(ADDRESS), (match) => [match.index, match.index + match[0].length] as const);
  const keepAddresses = (part: string, offset: number) => {
    const nodes: ReactNode[] = [];
    let at = 0;
    for (const [from, to] of addresses) {
      if (from < offset || to > offset + part.length || to - from > MAX_KEPT) continue;
      nodes.push(part.slice(at, from - offset), <span key={from} className="whitespace-nowrap">{part.slice(from - offset, to - offset)}</span>);
      at = to - offset;
    }
    nodes.push(part.slice(at));
    return nodes;
  };
  // Without a word segmenter (older browsers), the text wraps as it always did.
  if (typeof Intl.Segmenter !== 'function') return <>{keepAddresses(text, 0)}</>;
  const segments = Array.from(new Intl.Segmenter(chinese ? 'zh' : 'en', { granularity: 'word' }).segment(text));
  let start = text.length;
  let words = 0;
  for (let index = segments.length - 1; index >= 0; index -= 1) {
    const segment = segments[index];
    start = segment.index;
    if (segment.isWordLike) words += 1;
    if (chinese ? text.length - start >= 8 && segment.isWordLike : words >= 3) break;
  }
  const inside = addresses.find(([from, to]) => start > from && start < to);
  if (inside) start = inside[0];
  if (text.length - start > MAX_KEPT) return <>{keepAddresses(text, 0)}</>;
  if (start <= 0) return <span className="whitespace-nowrap">{text}</span>;
  return <>{keepAddresses(text.slice(0, start), 0)}<span className="whitespace-nowrap">{text.slice(start)}</span></>;
}
