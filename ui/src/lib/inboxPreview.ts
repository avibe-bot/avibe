// The part of a reply an Inbox card's preview can show (see
// components/workbench/InboxPreview).

/** Many times what the deepest clamp (three lines of the widest card) shows. */
export const INBOX_PREVIEW_MAX_CHARS = 1200;

const BLANK_LINE = /^[ \t\r]*$/;
const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const ATX_HEADING = /^ {0,3}#{1,6}(?:[ \t\r]|$)/;
// A reference-style link or footnote definition (`[id]: url`, `[^1]: text`).
// The reference may sit in the shown text while its definition is far below.
const DEFINITION_LINE = /^ {0,3}\[[^\]\n]+\]:.*$/gm;

/** The leading part of ``text`` through the line that holds the
 *  ``INBOX_PREVIEW_MAX_CHARS``th character and on to the next place a cut
 *  cannot change what is kept: before a blank line, an opening code fence or an
 *  ATX heading (each ends a paragraph, and no inline construct such as a link
 *  with a long destination, a data-URL image, emphasis or a code span crosses
 *  one), or between two lines inside a fenced code block. So everything kept
 *  renders exactly as the start of the full reply does. A reply with no such
 *  place after the cap is kept whole, as before. Link definitions from the rest
 *  are appended. Text within the cap is returned as is. */
export function inboxPreviewSource(text: string): string {
  if (text.length <= INBOX_PREVIEW_MAX_CHARS) return text;
  let fence: string | null = null;
  for (let start = 0; start < text.length;) {
    const newline = text.indexOf('\n', start);
    const line = text.slice(start, newline === -1 ? text.length : newline);
    if (
      start >= INBOX_PREVIEW_MAX_CHARS
      && (fence !== null || BLANK_LINE.test(line) || FENCE.test(line) || ATX_HEADING.test(line))
    ) {
      return withDefinitions(text, start, fence);
    }
    const run = FENCE.exec(line)?.[1];
    if (fence === null) {
      if (run) fence = run;
    } else if (run && run[0] === fence[0] && run.length >= fence.length && BLANK_LINE.test(line.trimStart().slice(run.length))) {
      fence = null;
    }
    if (newline === -1) break;
    start = newline + 1;
  }
  return text;
}

function withDefinitions(text: string, end: number, fence: string | null): string {
  const head = text.slice(0, end);
  const definitions = text.slice(end).match(DEFINITION_LINE);
  if (!definitions) return head;
  // Close a fence the cut left open, or the definitions would be code.
  return `${head}${fence ? `${fence}\n` : ''}\n${definitions.join('\n')}`;
}
