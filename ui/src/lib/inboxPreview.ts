// The part of a reply an Inbox card's preview can show (see
// components/workbench/InboxPreview).

/** Enough for the deepest clamp (three lines of the widest card) many times over. */
export const INBOX_PREVIEW_MAX_CHARS = 1200;
export const INBOX_PREVIEW_MAX_LINES = 24;

// A reference-style link or footnote definition (`[id]: url`, `[^1]: text`).
// The reference may sit in the shown lines while its definition is far below.
const DEFINITION_LINE = /^ {0,3}\[[^\]\n]+\]:.*$/gm;

/** The leading whole lines of ``text`` within the caps, plus any link
 *  definitions from the rest, so what renders reads exactly as the start of the
 *  full reply does. Text within the caps is returned as is. */
export function inboxPreviewSource(text: string): string {
  let end = 0;
  for (let lines = 0; lines < INBOX_PREVIEW_MAX_LINES && end < text.length; lines += 1) {
    const newline = text.indexOf('\n', end);
    const next = newline === -1 ? text.length : newline + 1;
    if (next > INBOX_PREVIEW_MAX_CHARS) break;
    end = next;
  }
  if (end >= text.length) return text;
  if (end === 0) {
    // One line longer than the cap: cut inside it, never between the halves of
    // a surrogate pair.
    end = INBOX_PREVIEW_MAX_CHARS;
    const last = text.charCodeAt(end - 1);
    if (last >= 0xd800 && last <= 0xdbff) end -= 1;
  }
  const definitions = text.slice(end).match(DEFINITION_LINE);
  const head = text.slice(0, end);
  return definitions ? `${head}\n\n${definitions.join('\n')}` : head;
}
