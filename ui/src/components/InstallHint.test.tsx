/** @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const native = vi.hoisted(() => ({ shell: false }));
vi.mock('@/lib/native', () => ({ isNativeShell: () => native.shell }));
vi.mock('@/lib/platform', () => ({
  isRealMobileSafari: () => true,
  isStandalonePwa: () => false,
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import { InstallHint } from './InstallHint';

beforeEach(() => {
  native.shell = false;
  window.localStorage.clear();
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn(() => ({ matches: true })),
  });
});

afterEach(() => cleanup());

describe('InstallHint', () => {
  it('shows in eligible mobile Safari', async () => {
    render(<InstallHint />);

    await waitFor(() => expect(screen.getByText('installHint.cta')).toBeTruthy());
  });

  it('stays hidden in the native shell', async () => {
    native.shell = true;
    render(<InstallHint />);

    await waitFor(() => expect(screen.queryByText('installHint.cta')).toBeNull());
  });
});
