/** @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock('../../context/BotsContext', () => ({
  useBots: () => ({ enabled: false, bots: [], displayName: () => '' }),
}));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_read_instance: true } }),
}));
vi.mock('../../context/NotificationPreferencesContext', () => ({
  useNotificationPreferences: () => ({
    preferences: { enabled: true, bots: true, sessions: true },
    update: vi.fn(),
  }),
}));
vi.mock('./WebPushControl', () => ({
  WebPushControl: () => <div data-testid="web-push-control" />,
}));

import { NotificationMenu } from './NotificationMenu';

afterEach(() => {
  cleanup();
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
});

describe('NotificationMenu', () => {
  it('shows the Web Push control when the instance can be read in a browser', () => {
    render(<MemoryRouter><NotificationMenu /></MemoryRouter>);
    expect(screen.getByTestId('web-push-control')).toBeTruthy();
  });

  it('hides the Web Push control in the desktop shell', () => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    render(<MemoryRouter><NotificationMenu /></MemoryRouter>);
    expect(screen.queryByTestId('web-push-control')).toBeNull();
  });
});
