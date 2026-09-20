import { describe, expect, it } from 'vitest';

import { copyHref, editorPath, liveHref, localPath, type ShowPageLinkInfo } from './showPageLinks';

const page = (visibility: string): ShowPageLinkInfo => ({
  session_id: 'ses/author',
  visibility,
  active_url: null,
  share_id: 'shared-link',
});

describe('Show Page link routes', () => {
  it('keeps authenticated editor frames on the author route for every online mode', () => {
    expect(editorPath(page('private'))).toBe('/show/ses%2Fauthor/');
    expect(editorPath(page('limited'))).toBe('/show/ses%2Fauthor/');
    expect(editorPath(page('public'))).toBe('/show/ses%2Fauthor/');
    expect(editorPath(page('offline'))).toBeNull();
    expect(editorPath(page('unexpected'))).toBeNull();
  });

  it('keeps Limited share links on the signed-in guest route', () => {
    const limited = {
      ...page('limited'),
      public_url: 'https://show.example.test/p/shared-link/',
    };
    expect(localPath(limited)).toBe('/p/shared-link/');
    expect(liveHref(limited)).toBe('https://show.example.test/p/shared-link/');
    expect(copyHref(limited)).toBe('https://show.example.test/p/shared-link/');
  });

  it('prefers the Cloud-qualified Limited URL over the local fallback', () => {
    const limited = {
      ...page('limited'),
      active_url: '/p/shared-link/',
      public_url: 'https://alice.avibe.bot/p/shared-link/',
    };

    expect(copyHref(limited)).toBe('https://alice.avibe.bot/p/shared-link/');
  });

  it('does not expose a Limited link without a Cloud-qualified URL', () => {
    const limited = {
      ...page('limited'),
      active_url: '/p/shared-link/',
      public_url: null,
    };

    expect(liveHref(limited)).toBeNull();
    expect(copyHref(limited)).toBeNull();
  });

  it('keeps public share links on the guest-facing route', () => {
    expect(localPath(page('public'))).toBe('/p/shared-link/');
  });
});
