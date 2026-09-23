import type { WorkbenchMessage } from '@/context/ApiContext';
import type { TextEdit } from '@/lib/citations';

const LEGACY_RESULT_FOOTER_RE =
  /^(?:✅|⚠️|❌) (?:⏱️ (?:\d+m )?\d+s(?: · 🪙 \d+(?:\.\d+)?[kM]? tok)?|🪙 \d+(?:\.\d+)?[kM]? tok)$/u;

export type ResultFooterParts = {
  body: string;
  footer: string | null;
  /** How `body` was cut out of `message.text`, so measurements taken against
   *  the stored row (a citation's spans) can be carried onto the body the
   *  renderer is handed. Empty when the two are the same text. */
  edits: TextEdit[];
};

function webResultFooter(footer: string): string {
  return footer.replace(/^✅\s+/u, '');
}

/** The whole `[at, message.text.length)` tail, removed. */
function cut(at: number, length: number): TextEdit[] {
  return [{ start: at, end: length, inserted: 0 }];
}

/** Separate Avibe's generated duration/token summary from an Agent reply body. */
export function resultFooterParts(
  message: Pick<WorkbenchMessage, 'author' | 'type' | 'text' | 'content'>,
): ResultFooterParts {
  if (message.author !== 'agent' || (message.type !== 'result' && message.type !== 'error')) {
    return { body: message.text, footer: null, edits: [] };
  }

  const structured = (message.content as { result_footer?: unknown } | null)?.result_footer;
  if (typeof structured === 'string' && structured.trim()) {
    const footer = structured.trim();
    const suffix = `\n\n${footer}`;
    const stripped = message.text.endsWith(suffix);
    return {
      body: stripped ? message.text.slice(0, -suffix.length) : message.text,
      footer: webResultFooter(footer),
      edits: stripped ? cut(message.text.length - suffix.length, message.text.length) : [],
    };
  }

  if (LEGACY_RESULT_FOOTER_RE.test(message.text)) {
    return { body: '', footer: webResultFooter(message.text), edits: cut(0, message.text.length) };
  }

  // Rows created before result_footer became structured still contain the exact
  // generated footer as their final paragraph. Match only that closed format so
  // authored prose ending in ordinary emoji/text is never moved.
  const splitAt = message.text.lastIndexOf('\n\n');
  if (splitAt < 0) return { body: message.text, footer: null, edits: [] };
  const candidate = message.text.slice(splitAt + 2);
  if (!LEGACY_RESULT_FOOTER_RE.test(candidate)) return { body: message.text, footer: null, edits: [] };
  return {
    body: message.text.slice(0, splitAt),
    footer: webResultFooter(candidate),
    edits: cut(splitAt, message.text.length),
  };
}
