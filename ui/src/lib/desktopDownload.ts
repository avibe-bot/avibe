export type DesktopDownloadPlatform = 'macos' | 'windows' | 'unknown';
export type MacosArchitecture = 'arm64' | 'x64';

export type DesktopDownloadSelection = {
  platform: DesktopDownloadPlatform;
  macosArchitecture: MacosArchitecture;
};

export const DESKTOP_DOWNLOAD_URLS = {
  macosArm64: 'https://dl.avibe.bot/latest/Avibe_aarch64-apple-darwin.dmg',
  macosX64: 'https://dl.avibe.bot/latest/Avibe_x86_64-apple-darwin.dmg',
  windowsX64: 'https://dl.avibe.bot/latest/Avibe_x86_64-pc-windows-msvc.exe',
} as const;

type NavigatorPlatform = Pick<Navigator, 'platform' | 'userAgent' | 'maxTouchPoints'>;

const isIpadDesktopMode = (value: NavigatorPlatform): boolean =>
  value.platform === 'MacIntel' && value.maxTouchPoints > 1;

export function detectDesktopDownloadSelection(
  value: NavigatorPlatform = navigator,
): DesktopDownloadSelection {
  const fingerprint = `${value.platform} ${value.userAgent}`;
  if (isIpadDesktopMode(value) || /iPhone|iPad|iPod/i.test(fingerprint)) {
    return { platform: 'unknown', macosArchitecture: 'arm64' };
  }
  if (/Macintosh|MacIntel|Mac OS X/i.test(fingerprint)) {
    return { platform: 'macos', macosArchitecture: 'arm64' };
  }
  if (/Windows/i.test(fingerprint) && /Win64|WOW64|x86_64|x64|amd64/i.test(fingerprint)) {
    return { platform: 'windows', macosArchitecture: 'arm64' };
  }
  return { platform: 'unknown', macosArchitecture: 'arm64' };
}

export function desktopDownloadUrl(selection: DesktopDownloadSelection): string | null {
  if (selection.platform === 'macos') {
    return selection.macosArchitecture === 'arm64'
      ? DESKTOP_DOWNLOAD_URLS.macosArm64
      : DESKTOP_DOWNLOAD_URLS.macosX64;
  }
  if (selection.platform === 'windows') return DESKTOP_DOWNLOAD_URLS.windowsX64;
  return null;
}
