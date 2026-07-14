import type { AppId } from '../../../apps/registry';

export type AppSearchResult = {
  key: string;
  kind: 'builtin' | 'showpage';
  appId: AppId;
  title: string;
  searchTitle: string;
  sessionId?: string;
  /** The AI page icon's opaque cache token, when it has one (§7.1f). */
  iconVersion?: string | null;
};

export function filterAppSearchResults(results: AppSearchResult[], query: string): AppSearchResult[] {
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized) return [];
  return results.filter((result) => result.searchTitle.toLocaleLowerCase().includes(normalized));
}
