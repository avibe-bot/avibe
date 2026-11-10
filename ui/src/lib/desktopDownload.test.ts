import { describe, expect, it } from 'vitest';

import {
  DESKTOP_DOWNLOAD_URLS,
  desktopDownloadUrl,
  detectDesktopDownloadSelection,
} from './desktopDownload';

const browser = (platform: string, userAgent: string, maxTouchPoints = 0) => ({
  platform,
  userAgent,
  maxTouchPoints,
});

describe('desktop download platform detection', () => {
  it('preselects macOS with Apple Silicon as the safe default', () => {
    expect(detectDesktopDownloadSelection(browser('MacIntel', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)'))).toEqual({
      platform: 'macos',
      macosArchitecture: 'arm64',
    });
  });

  it('recognizes Windows x64 without guessing Windows ARM', () => {
    expect(detectDesktopDownloadSelection(browser('Win32', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'))).toEqual({
      platform: 'windows',
      macosArchitecture: 'arm64',
    });
    expect(detectDesktopDownloadSelection(browser('Win32', 'Mozilla/5.0 (Windows NT 10.0; ARM64)')).platform).toBe('unknown');
  });

  it('does not treat iPad desktop mode as macOS', () => {
    expect(detectDesktopDownloadSelection(browser('MacIntel', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15)', 5)).platform).toBe('unknown');
  });

  it('leaves unsupported systems unselected', () => {
    expect(detectDesktopDownloadSelection(browser('Linux x86_64', 'Mozilla/5.0 (X11; Linux x86_64)')).platform).toBe('unknown');
  });
});

describe('desktop download contracts', () => {
  it('maps each supported selection to the exact stable installer URL', () => {
    expect(desktopDownloadUrl({ platform: 'macos', macosArchitecture: 'arm64' })).toBe(DESKTOP_DOWNLOAD_URLS.macosArm64);
    expect(desktopDownloadUrl({ platform: 'macos', macosArchitecture: 'x64' })).toBe(DESKTOP_DOWNLOAD_URLS.macosX64);
    expect(desktopDownloadUrl({ platform: 'windows', macosArchitecture: 'arm64' })).toBe(DESKTOP_DOWNLOAD_URLS.windowsX64);
  });

  it('does not produce a download URL for an unknown platform', () => {
    expect(desktopDownloadUrl({ platform: 'unknown', macosArchitecture: 'arm64' })).toBeNull();
  });
});
