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

  it('does not expose Limited as a live link before signed-in guest admission exists', () => {
    const limited = {
      ...page('limited'),
      active_url: 'https://show.example.test/p/shared-link/',
    };
    expect(localPath(limited)).toBeNull();
    expect(liveHref(limited)).toBeNull();
    expect(copyHref(limited)).toBeNull();
  });

  it('keeps public share links on the guest-facing route', () => {
    expect(localPath(page('public'))).toBe('/p/shared-link/');
  });
});
