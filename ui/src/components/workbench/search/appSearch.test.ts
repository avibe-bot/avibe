import { describe, expect, it } from 'vitest';

import { filterAppSearchResults, type AppSearchResult } from './appSearch';

const results: AppSearchResult[] = [
  { key: 'builtin:files', kind: 'builtin', appId: 'files', title: 'Files', searchTitle: 'Files' },
  {
    key: 'show:installed',
    kind: 'showpage',
    appId: 'showpage',
    title: 'Sales dashboard',
    searchTitle: 'Sales dashboard',
    sessionId: 'installed',
  },
  {
    key: 'show:inventory-only',
    kind: 'showpage',
    appId: 'showpage',
    title: 'Incident review',
    searchTitle: 'Incident review',
    sessionId: 'inventory-only',
  },
  {
    key: 'show:untitled',
    kind: 'showpage',
    appId: 'showpage',
    title: 'Untitled session',
    searchTitle: '',
    sessionId: 'untitled',
  },
];

describe('filterAppSearchResults', () => {
  it('matches built-ins and every matching AI page regardless of install state', () => {
    expect(filterAppSearchResults(results, 'files').map((result) => result.key)).toEqual(['builtin:files']);
    expect(filterAppSearchResults(results, 'review').map((result) => result.key)).toEqual([
      'show:inventory-only',
    ]);
  });

  it('normalizes case and surrounding whitespace', () => {
    expect(filterAppSearchResults(results, '  DASHBOARD  ').map((result) => result.key)).toEqual([
      'show:installed',
    ]);
  });

  it('does not match an untitled page by its session id', () => {
    expect(filterAppSearchResults(results, 'untitled')).toEqual([]);
  });
});
