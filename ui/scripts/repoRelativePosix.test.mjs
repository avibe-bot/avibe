import path from 'node:path';

import { describe, expect, it } from 'vitest';

import { repoRelativePosix } from './repoRelativePosix.mjs';

// The out-of-tree import guard compares the paths ``ui/src`` reaches for against the
// ``COPY`` sources in the Dockerfile's ui-builder stage. Those sources are POSIX, and
// so is every join and prefix test the guard does afterwards. ``path.relative`` is not:
// it answers in the host separator. That mismatch is invisible on Linux and macOS, and
// only the desktop Windows package job runs ``npm run build`` on Windows, so it shipped
// and broke a release there. These cases pin the normalization on every host by driving
// ``path.win32`` explicitly, which is the only way to reach Windows' separator from CI.

// The two repo-root catalogs ``ui/src`` imports, as the Dockerfile spells them.
const COPY_SOURCES = ['vibe/message_types.json', 'vibe/data/api_key_vendors.json'];

describe('repoRelativePosix normalizes a host path into the form the guard compares', () => {
  it('rewrites a Windows result into POSIX', () => {
    expect(
      repoRelativePosix('C:\\repo', 'C:\\repo\\vibe\\data\\api_key_vendors.json', path.win32),
    ).toBe('vibe/data/api_key_vendors.json');
  });

  it('leaves a POSIX result exactly as it was', () => {
    expect(repoRelativePosix('/repo', '/repo/vibe/data/api_key_vendors.json', path.posix)).toBe(
      'vibe/data/api_key_vendors.json',
    );
  });

  it('defaults to the running host, so callers pass nothing', () => {
    const root = path.resolve('/repo');
    expect(repoRelativePosix(root, path.join(root, 'vibe', 'message_types.json'))).toBe(
      'vibe/message_types.json',
    );
  });
});

describe('the Windows separator is what made the guard reject a correct Dockerfile', () => {
  // Both catalogs are copied as exact files, so the guard matches them by equality with
  // the COPY source. A backslash target equals none of them and matches no prefix either,
  // which is why the failure read "puts it at no path at all".
  it.each(COPY_SOURCES)('%s only matches its COPY source once normalized', (source) => {
    const windowsRoot = 'C:\\repo';
    const absolute = path.win32.join(windowsRoot, ...source.split('/'));

    const raw = path.win32.relative(windowsRoot, absolute);
    expect(raw).not.toBe(source);
    expect(raw.startsWith(`${source}/`)).toBe(false);

    expect(repoRelativePosix(windowsRoot, absolute, path.win32)).toBe(source);
  });

  it('keeps the expected image path from being mangled', () => {
    const windowsRoot = 'C:\\repo';
    const absolute = 'C:\\repo\\vibe\\data\\api_key_vendors.json';

    // What the guard would have claimed the Dockerfile must provide, before the fix.
    expect(path.posix.join('/app', path.win32.relative(windowsRoot, absolute))).toBe(
      '/app/vibe\\data\\api_key_vendors.json',
    );

    expect(path.posix.join('/app', repoRelativePosix(windowsRoot, absolute, path.win32))).toBe(
      '/app/vibe/data/api_key_vendors.json',
    );
  });
});
