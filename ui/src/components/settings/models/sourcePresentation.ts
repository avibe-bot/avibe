import type { Source } from './types';
import { officialVendorForEndpoint } from './vendorMeta';

export const SOURCE_PROVIDER_COPY_KEYS: Partial<Record<string, string>> = {
  anthropic: 'settings.models.upstream.vendor.anthropic',
  openai: 'settings.models.upstream.vendor.openai',
};

export const sourceProviderIdentity = (source: Pick<Source, 'vendor' | 'base_url'>): string => {
  if (!source.base_url) return source.vendor;
  const officialVendor = officialVendorForEndpoint(source.base_url);
  if (officialVendor) return officialVendor;
  try {
    const host = new URL(source.base_url).host;
    return host;
  } catch {
    return source.vendor;
  }
};

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
