const DEFAULT_FAVICON_PATH = '/logo.png';
const FAVICON_SELECTOR = 'link[rel="icon"]';
const BASE_HREF_ATTRIBUTE = 'data-avibe-base-href';

export function formatFaviconBadgeCount(count: number): string {
  const normalized = Number.isFinite(count) ? Math.max(0, Math.trunc(count)) : 0;
  return normalized > 99 ? '99+' : String(normalized);
}
/** Build a small self-contained favicon so the browser can render the badge. */
export function createFaviconBadgeDataUrl(count: number): string {
  const label = formatFaviconBadgeCount(count);
  const fontSize = label.length > 2 ? 15 : 19;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#1b0f45"/><path d="M6 34h10c5 0 5-14 10-14s5 25 10 25 5-33 10-33 5 22 10 22h2" fill="none" stroke="#fff" stroke-linecap="round" stroke-linejoin="round" stroke-width="5"/><circle cx="51" cy="13" r="12" fill="#ff5368" stroke="#080812" stroke-width="3"/><text x="51" y="18" fill="#fff" font-family="Arial,sans-serif" font-size="${fontSize}" font-weight="700" text-anchor="middle">${label}</text></svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

/** Keep the regular favicon until a badge is needed, then restore it exactly. */
export function syncFaviconBadge(count: number, targetDocument: Document | null = typeof document === 'undefined' ? null : document): void {
  if (!targetDocument) return;

  let link = targetDocument.querySelector<HTMLLinkElement>(FAVICON_SELECTOR);
  if (!link) {
    link = targetDocument.createElement('link');
    link.rel = 'icon';
    link.href = DEFAULT_FAVICON_PATH;
    targetDocument.head?.appendChild(link);
  }

  const baseHref = link.getAttribute(BASE_HREF_ATTRIBUTE) || link.getAttribute('href') || DEFAULT_FAVICON_PATH;
  link.setAttribute(BASE_HREF_ATTRIBUTE, baseHref);
  link.href = count > 0 ? createFaviconBadgeDataUrl(count) : baseHref;
}
