// The part of a reply an Inbox card's preview can show (see
// components/workbench/InboxPreview).

/** Many times what the deepest clamp (three lines of the widest card) shows. */
export const INBOX_PREVIEW_MAX_CHARS = 1200;

// A reference-style link or footnote definition (`[id]: url`, `[^1]: text`).
// The reference may sit in the shown text while its definition is far below.
const DEFINITION_LINE = /^ {0,3}\[[^\]\n]+\]:.*$/gm;

/** The first ``INBOX_PREVIEW_MAX_CHARS`` of ``text``, plus any link definitions
 *  from the rest, so what renders reads exactly as the start of the full reply
 *  does. The cut may fall inside a line, never between the halves of a
 *  surrogate pair. Text within the cap is returned as is. */
export function inboxPreviewSource(text: string): string {
  if (text.length <= INBOX_PREVIEW_MAX_CHARS) return text;
  let end = INBOX_PREVIEW_MAX_CHARS;
  const last = text.charCodeAt(end - 1);
  if (last >= 0xd800 && last <= 0xdbff) end -= 1;
  const definitions = text.slice(end).match(DEFINITION_LINE);
  const head = text.slice(0, end);
  return definitions ? `${head}\n\n${definitions.join('\n')}` : head;
}
