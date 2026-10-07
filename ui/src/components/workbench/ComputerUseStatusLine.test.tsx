/* @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ComputerUseStatusLine, computerUseStatusCopy } from './ComputerUseStatusLine';

const apiFetch = vi.hoisted(() => vi.fn());

vi.mock('../../lib/apiFetch', () => ({ apiFetch }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

afterEach(() => {
  cleanup();
  apiFetch.mockReset();
});

describe('computerUseStatusCopy', () => {
  it('maps permission reasons to localized copy without exposing raw reason codes', () => {
    const copy = computerUseStatusCopy(
      { status: 'needs_permission', reason: 'screen_recording' },
      (key) => key,
    );
    expect(copy.label).toBe('workbench.home.computerUse.needsPermission');
    expect(copy.detail).toBe('workbench.home.computerUse.screenRecordingDetail');
    expect(copy.detail).not.toContain('screen_recording');
  });

  it('keeps unknown permission reasons localized', () => {
    const copy = computerUseStatusCopy(
      { status: 'needs_permission', reason: 'future_permission' },
      (key) => key,
    );
    expect(copy.detail).toBe('workbench.home.computerUse.unknownPermissionDetail');
    expect(copy.detail).not.toContain('future_permission');
  });

  it('distinguishes unavailable runtime from an unsupported old runtime', () => {
    expect(computerUseStatusCopy(
      { status: 'needs_runtime', reason: 'runtime_unavailable' },
      (key) => key,
    ).detail).toContain('runtimeUnavailableDetail');
    expect(computerUseStatusCopy(
      { status: 'needs_runtime', reason: 'runtime_too_old' },
      (key) => key,
    ).detail).toContain('runtimeTooOldDetail');
  });
});

describe('ComputerUseStatusLine', () => {
  it('renders one read-only line per observed status and refreshes without cache', async () => {
    apiFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ status: 'needs_permission', reason: 'screen_recording' }),
    });

    render(<ComputerUseStatusLine />);

    await waitFor(() => {
      expect(screen.getByTestId('computer-use-status')).toBeTruthy();
    });
    expect(screen.getAllByTestId('computer-use-status')).toHaveLength(1);
    expect(screen.getByTestId('computer-use-status').getAttribute('data-computer-use-status')).toBe(
      'needs_permission',
    );
    expect(screen.getByText('workbench.home.computerUse.screenRecordingDetail')).toBeTruthy();
    expect(screen.queryByText('screen_recording')).toBeNull();
    expect(apiFetch).toHaveBeenCalledWith(
      '/api/desktop/computer-use/status',
      { cache: 'no-store' },
    );
  });
});
