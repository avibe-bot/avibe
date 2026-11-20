// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const shell = vi.hoisted(() => ({ desktop: false, nativeAppWindow: false, loads: 0 }));
vi.mock('../lib/desktopShell', () => ({
  isDesktopShell: () => shell.desktop,
  isNativeAppWindow: () => shell.nativeAppWindow,
}));
vi.mock('./DesktopComposeMount', () => {
  shell.loads += 1;
  return { DesktopComposeMount: () => <div data-testid="compose-receiver" /> };
});

import { DesktopCompose } from './DesktopCompose';

afterEach(() => {
  cleanup();
  shell.desktop = false;
  shell.nativeAppWindow = false;
});

// The receiver's code is off the first screen, so what must hold is where it still arrives:
// in a desktop shell's Workbench document, and nowhere a browser pays for it.
describe('DesktopCompose', () => {
  it('leaves the receiver code unfetched in a browser and in a native app window', async () => {
    const before = shell.loads;
    render(<DesktopCompose />);
    shell.desktop = true;
    shell.nativeAppWindow = true;
    render(<DesktopCompose />);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByTestId('compose-receiver')).toBeNull();
    expect(shell.loads).toBe(before);
  });

  it('mounts the receiver in a desktop shell once its code has loaded', async () => {
    shell.desktop = true;
    render(<DesktopCompose />);
    expect(await screen.findByTestId('compose-receiver')).toBeTruthy();
  });
});
