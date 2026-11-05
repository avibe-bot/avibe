// The part of a reply an Inbox card's preview can show (see
// components/workbench/InboxPreview).

/** Many times what the deepest clamp (three lines of the widest card) shows. */
export const INBOX_PREVIEW_MAX_CHARS = 1200;

// A blank line ends a Markdown block, and no inline construct (a link with a
// long destination, a data-URL image, emphasis, a code span) runs across one.
const BLANK_LINE = /\n[ \t\r]*\n/g;
// A reference-style link or footnote definition (`[id]: url`, `[^1]: text`).
// The reference may sit in the shown text while its definition is far below.
const DEFINITION_LINE = /^ {0,3}\[[^\]\n]+\]:.*$/gm;

/** The leading blocks of ``text``, through the block that holds the
 *  ``INBOX_PREVIEW_MAX_CHARS``th character, plus any link definitions from the
 *  rest. A cut only ever falls between blocks, so everything kept renders
 *  exactly as the start of the full reply does; one block that is very long
 *  is kept whole, as before. Text within the cap is returned as is. */
export function inboxPreviewSource(text: string): string {
  if (text.length <= INBOX_PREVIEW_MAX_CHARS) return text;
  BLANK_LINE.lastIndex = Math.max(0, text.lastIndexOf('\n', INBOX_PREVIEW_MAX_CHARS - 1));
  const blank = BLANK_LINE.exec(text);
  if (!blank) return text;
  const end = blank.index + 1;
  const definitions = text.slice(end).match(DEFINITION_LINE);
  const head = text.slice(0, end);
  return definitions ? `${head}\n${definitions.join('\n')}` : head;
}
