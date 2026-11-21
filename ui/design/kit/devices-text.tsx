// Shared by the Devices proposals (kit/devices-*.tsx): how a paragraph wraps.
/**
 * A paragraph whose last line never holds only a word or two. Chinese breaks between any two
 * characters and `text-wrap: pretty` does not reliably prevent that, so the tail (at least eight
 * characters, or three English words, cut at a word boundary) is one unbreakable run, and an
 * address is never split across lines.
 */
export function Para({ text }: { text: string }) {
  const cjk = /[\u3400-\u9fff]/.test(text);
  const segments = Array.from(new Intl.Segmenter(cjk ? 'zh' : 'en', { granularity: 'word' }).segment(text));
  let start = text.length;
  let words = 0;
  for (let index = segments.length - 1; index >= 0; index -= 1) {
    const segment = segments[index];
    start = segment.index;
    if (segment.isWordLike) words += 1;
    if (cjk ? text.length - start >= 8 && segment.isWordLike : words >= 3) break;
  }
  const keepAddresses = (part: string) => part.split(/([a-z0-9-]+(?:\.[a-z0-9-]+)+)/i).map((piece, index) => (
    index % 2 === 1 ? <span key={index} className="whitespace-nowrap">{piece}</span> : piece
  ));
  if (start <= 0) return <span className="whitespace-nowrap">{text}</span>;
  return <>{keepAddresses(text.slice(0, start))}<span className="whitespace-nowrap">{text.slice(start)}</span></>;
}
