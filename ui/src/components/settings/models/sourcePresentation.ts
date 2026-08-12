import type { Source } from './types';

export const sourceDetail = (source: Source): string | null => {
  const parts: string[] = [];
  if (source.account_label) parts.push(source.account_label);
  if (source.base_url) {
    try {
      const url = new URL(source.base_url);
      parts.push(`${url.host}${url.pathname === '/' ? '' : url.pathname}`);
    } catch {
      parts.push(source.base_url);
    }
  }
  if (source.masked_credential) parts.push(source.masked_credential);
  return parts.length > 0 ? parts.join(' · ') : null;
};
