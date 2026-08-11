const DEFAULT_FAVICON_PATH = '/logo.png';
const UNREAD_FAVICON_PATH = '/logo-unread.png';
const FAVICON_SELECTOR = 'link[rel="icon"]';
const BASE_HREF_ATTRIBUTE = 'data-avibe-base-href';

/** Keep the recognizable brand favicon and add only the unread-state red dot. */
export function syncFaviconBadge(
  count: number,
  targetDocument: Document | null = typeof document === 'undefined' ? null : document,
): void {
  if (!targetDocument) return;

  let link = targetDocument.querySelector<HTMLLinkElement>(FAVICON_SELECTOR);
  if (!link) {
    link = targetDocument.createElement('link');
    link.rel = 'icon';
    link.type = 'image/png';
    link.href = DEFAULT_FAVICON_PATH;
    targetDocument.head?.appendChild(link);
  }

  const baseHref = link.getAttribute(BASE_HREF_ATTRIBUTE) || link.getAttribute('href') || DEFAULT_FAVICON_PATH;
  link.setAttribute(BASE_HREF_ATTRIBUTE, baseHref);
  link.href = count > 0 ? UNREAD_FAVICON_PATH : baseHref;
}
