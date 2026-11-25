// Text whose last line never holds only a character or two (ui/design: settings/devices-*): used
// on the Devices pages. A short consequence line keeps a longer tail together; a body paragraph
// fills its lines like any other and only its last few characters stay together.
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

const ADDRESS = /[a-z0-9-]+(?:\.[a-z0-9-]+)+/gi;
/** Longer runs than this may still wrap: a 63-character name must not overflow a phone. */
const MAX_KEPT = 32;
/**
 * A line this short (one or two lines where these pages show it) keeps a long tail, which sets it
 * in two even lines. In a longer paragraph a long unbreakable tail leaves the line before it
 * visibly short, so body text keeps only enough to never end on a lone character: the line before
 * gives up three characters at most.
 */
const SHORT = 40;
/** The longest tail a body paragraph keeps together; past it (an address in the way) it keeps none. */
const MAX_BODY_TAIL = 8;
/** How wide a text sets, in full-width characters: a Chinese character is one, a Latin one half. */
const widthOf = (text: string): number => Array.from(text).reduce((sum, char) => sum + ((char.codePointAt(0) ?? 0) > 0x2e7f ? 1 : 0.5), 0);

/**
 * Text whose last line never holds only a character or two. Chinese breaks between any two
 * characters and `text-wrap: pretty` does not reliably prevent that, so the tail is one
 * unbreakable run, cut at a word boundary: in a short line at least eight characters in Chinese or
 * three words; in a body paragraph at least four characters or two words. An address is never
 * split across lines, in text of any length: a cut that would fall inside one moves to its start.
 * Which rule applies follows the page's language, not the text (a Chinese device name in English
 * copy).
 */
export function KeptTail({ text, fill = false }: {
  text: string;
  /** Fill each line as a body paragraph does, whatever the length: for a line in a full-width row
   *  (a choice's hint, a card's footnote), where a short line's long tail would leave a third of
   *  the row empty on a phone. */
  fill?: boolean;
}) {
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
  const body = fill || widthOf(text) > SHORT;
  const characters = body ? 4 : 8;
  const enough = body ? 2 : 3;
  const segments = Array.from(new Intl.Segmenter(chinese ? 'zh' : 'en', { granularity: 'word' }).segment(text));
  let start = text.length;
  let words = 0;
  for (let index = segments.length - 1; index >= 0; index -= 1) {
    const segment = segments[index];
    start = segment.index;
    if (segment.isWordLike) words += 1;
    if (chinese ? text.length - start >= characters && segment.isWordLike : words >= enough) break;
  }
  const inside = addresses.find(([from, to]) => start > from && start < to);
  if (inside) start = inside[0];
  if (text.length - start > MAX_KEPT) return <>{keepAddresses(text, 0)}</>;
  // A body paragraph does not stop a line short for its tail: a long one is not kept.
  if (body && widthOf(text.slice(start)) > MAX_BODY_TAIL) return <>{keepAddresses(text, 0)}</>;
  if (start <= 0) return <span className="whitespace-nowrap">{text}</span>;
  return <>{keepAddresses(text.slice(0, start), 0)}<span className="whitespace-nowrap">{text.slice(start)}</span></>;
}
