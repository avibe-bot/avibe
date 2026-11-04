/** A Markdown message read as plain text: fences and inline code keep their text, links and
 *  images their labels; headings, quotes, list markers, emphasis, and HTML tags go; whitespace
 *  outside code collapses to single spaces. For a message shown inside a compact surface. */
export const markdownToPlainText = (text: string): string => {
  const code: string[] = [];
  let nextCode = 0;
  const protectCode = (): string => `@@CODE${nextCode++}@@`;
  const plain = text
    .replace(/```[^\n]*\n([\s\S]*?)\n?```|~~~[^\n]*\n([\s\S]*?)\n?~~~/g, (_match, fencedBacktick: string, fencedTilde: string) => {
      const value = fencedBacktick ?? fencedTilde ?? '';
      code.push(value);
      return protectCode();
    })
    .replace(/`([^`\n]+)`/g, (_match, value: string) => {
      code.push(value);
      return protectCode();
    })
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/(^|\n)\s{0,3}(?:#{1,6}|>|[-*+]|\d+\.)[ \t]+/g, '$1')
    .replace(/~~([^~\n]+)~~/g, '$1')
    .replace(/\*\*\*([^*\n]+)\*\*\*/g, '$1')
    .replace(/___([^_\n]+)___/g, '$1')
    .replace(/\*\*([^*\n]+)\*\*/g, '$1')
    .replace(/__([^_\n]+)__/g, '$1')
    .replace(/(?<!\w)\*([^*\n]+)\*(?!\w)/g, '$1')
    .replace(/(?<!\w)_([^_\n]+)_(?!\w)/g, '$1')
    .replace(/<!--[\s\S]*?-->|<\/?[a-z][a-z0-9-]*(?:\s+[^<>]*?)?\/?>/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  return plain.replace(/@@CODE(\d+)@@/g, (_match, index: string) => code[Number(index)] ?? '');
};

/** The most characters an excerpt keeps: more than two lines of the widest surface that shows one,
 *  so the surface's own line clamp, not this cut, decides where it ends. */
export const EXCERPT_MAX_CHARS = 160;

/** A Bot's latest message where a list or card shows it beside the Bot (the strip's hover card,
 *  the switch sheet): plain text on one line, cut to `EXCERPT_MAX_CHARS` with an ellipsis. The
 *  surface clamps it to its lines. */
export const messageExcerpt = (text: string | null | undefined): string => {
  const plain = markdownToPlainText(text ?? '').replace(/\s+/g, ' ').trim();
  const chars = Array.from(plain);
  if (chars.length <= EXCERPT_MAX_CHARS) return plain;
  return `${chars.slice(0, EXCERPT_MAX_CHARS - 1).join('').trimEnd()}…`;
};
