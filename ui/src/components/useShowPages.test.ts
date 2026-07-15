import { describe, expect, it } from 'vitest';

import { replaceShowPageTitleIfCurrent, type ShowPage } from './useShowPages';

const page = (title: string | null): ShowPage => ({
  session_id: 'session-1',
  visibility: 'private',
  title,
  platform: null,
  agent: null,
  path: '/tmp/show',
  icon_version: null,
  active_url: null,
  private_url: null,
  public_url: null,
  url_available: false,
  share_id: null,
  offline: false,
  offline_at: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
});

describe('replaceShowPageTitleIfCurrent', () => {
  it('applies a matching optimistic rollback', () => {
    expect(replaceShowPageTitleIfCurrent([page('Draft')], 'session-1', 'Draft', 'Original')[0].title)
      .toBe('Original');
  });

  it('preserves a newer title when an older request settles', () => {
    const pages = [page('Newer title')];
    expect(replaceShowPageTitleIfCurrent(pages, 'session-1', 'Draft', 'Original')).toBe(pages);
  });
});
