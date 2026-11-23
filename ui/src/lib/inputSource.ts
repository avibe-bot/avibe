import { isDesktopShell } from './desktopShell';
import { isNativeShell } from './native';

/** Presentation hints only; the instance prefers the authenticated device registry. */
export function inputTerminalHint(): string {
  if (isDesktopShell()) return 'mac_app';
  if (isNativeShell()) return window.avibeNative?.platform === 'ios' ? 'iphone_app' : 'unknown';
  return 'browser';
}
