import { describe, expect, it } from 'vitest';

import { showDockId } from '../../context/DockContext';
import { mobileRouteForDockId, showAppRoutePath } from './mobileDock';

describe('showAppRoutePath', () => {
  it('builds the full-screen Show Page route', () => {
    expect(showAppRoutePath('abc123')).toBe('/apps/show/abc123');
  });

  it('URL-encodes ids with slashes and spaces', () => {
    expect(showAppRoutePath('a b/c')).toBe('/apps/show/a%20b%2Fc');
  });
});

describe('mobileRouteForDockId', () => {
  it('maps every built-in id to its /apps route', () => {
    expect(mobileRouteForDockId('files')).toBe('/apps/files');
    expect(mobileRouteForDockId('terminal')).toBe('/apps/terminal');
    expect(mobileRouteForDockId('editor')).toBe('/apps/editor');
    expect(mobileRouteForDockId('library')).toBe('/apps/library');
  });

  it('maps a show:<id> pin to the full-screen Show Page route', () => {
    expect(mobileRouteForDockId(showDockId('sess42'))).toBe('/apps/show/sess42');
  });

  it('encodes the session id embedded in a pin dock id', () => {
    expect(mobileRouteForDockId(showDockId('s/p ace'))).toBe('/apps/show/s%2Fp%20ace');
  });
});
