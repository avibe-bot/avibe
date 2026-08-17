import { describe, expect, it } from 'vitest';

import { editorPath, localPath, type ShowPageLinkInfo } from './showPageLinks';

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

  it('keeps Limited and public share links on the guest-facing route', () => {
    expect(localPath(page('limited'))).toBe('/p/shared-link/');
    expect(localPath(page('public'))).toBe('/p/shared-link/');
  });
});
