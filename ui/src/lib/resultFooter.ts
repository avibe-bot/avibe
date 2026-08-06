import type { WorkbenchMessage } from '@/context/ApiContext';

const LEGACY_RESULT_FOOTER_RE =
  /^(?:✅|⚠️|❌) (?:⏱️ (?:\d+m )?\d+s(?: · 🪙 \d+(?:\.\d+)?[kM]? tok)?|🪙 \d+(?:\.\d+)?[kM]? tok)$/u;

export type ResultFooterParts = {
  body: string;
  footer: string | null;
};

function webResultFooter(footer: string): string {
  return footer.replace(/^✅\s+/u, '');
}

/** Separate Avibe's generated duration/token summary from an Agent reply body. */
export function resultFooterParts(
  message: Pick<WorkbenchMessage, 'author' | 'type' | 'text' | 'content'>,
): ResultFooterParts {
  if (message.author !== 'agent' || (message.type !== 'result' && message.type !== 'error')) {
    return { body: message.text, footer: null };
  }

  const structured = (message.content as { result_footer?: unknown } | null)?.result_footer;
  if (typeof structured === 'string' && structured.trim()) {
    const footer = structured.trim();
    const suffix = `\n\n${footer}`;
    return {
      body: message.text.endsWith(suffix) ? message.text.slice(0, -suffix.length) : message.text,
      footer: webResultFooter(footer),
    };
  }

  if (LEGACY_RESULT_FOOTER_RE.test(message.text)) {
    return { body: '', footer: webResultFooter(message.text) };
  }

  // Rows created before result_footer became structured still contain the exact
  // generated footer as their final paragraph. Match only that closed format so
  // authored prose ending in ordinary emoji/text is never moved.
  const splitAt = message.text.lastIndexOf('\n\n');
  if (splitAt < 0) return { body: message.text, footer: null };
  const candidate = message.text.slice(splitAt + 2);
  if (!LEGACY_RESULT_FOOTER_RE.test(candidate)) return { body: message.text, footer: null };
  return { body: message.text.slice(0, splitAt), footer: webResultFooter(candidate) };
}
