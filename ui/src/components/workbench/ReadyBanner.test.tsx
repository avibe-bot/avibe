/* @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ReadyBanner } from './ReadyBanner';
import { shouldShowReadyBanner, type ReadyBannerInput } from './backendReadiness';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, unknown>) => (vars?.backend ? `${key}:${vars.backend}` : key),
  }),
}));

afterEach(cleanup);

const verified: ReadyBannerInput = {
  onboardingCompleted: true,
  readiness: { backend: 'codex', ready: true },
  currentBackend: 'codex',
  dismissed: false,
};

describe('shouldShowReadyBanner', () => {
  it('shows only when the wizard finished AND the backend itself confirms it', () => {
    expect(shouldShowReadyBanner(verified)).toBe(true);
  });

  it.each<[string, Partial<ReadyBannerInput>]>([
    ['the wizard did not just finish', { onboardingCompleted: false }],
    ['nothing has been observed yet', { readiness: null }],
    ['the probe answered not-ready', { readiness: { backend: 'codex', ready: false } }],
    ['a different backend answered', { readiness: { backend: 'claude', ready: true } }],
    ['the running backend is unknown', { currentBackend: null }],
    ['the user dismissed it', { dismissed: true }],
  ])('stays hidden when %s', (_case, override) => {
    expect(shouldShowReadyBanner({ ...verified, ...override })).toBe(false);
  });

  it('treats a pending probe exactly like a refused one — never as an optimistic yes', () => {
    expect(shouldShowReadyBanner({ ...verified, readiness: null }))
      .toBe(shouldShowReadyBanner({ ...verified, readiness: { backend: 'codex', ready: false } }));
  });
});

describe('ReadyBanner', () => {
  it('names the backend that confirmed and dismisses on request', async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn();
    render(<ReadyBanner backend="codex" onDismiss={onDismiss} />);

    expect(screen.getByText('workbench.home.ready:Codex')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'workbench.home.readyDismiss' }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('falls back to the raw name for a backend it has no label for', () => {
    render(<ReadyBanner backend="future-backend" onDismiss={() => {}} />);
    expect(screen.getByText('workbench.home.ready:future-backend')).toBeTruthy();
  });
});
