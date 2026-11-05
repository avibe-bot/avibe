/** @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const native = vi.hoisted(() => ({ shell: false }));
vi.mock('../../lib/native', () => ({ isNativeShell: () => native.shell }));
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

beforeEach(() => {
  native.shell = false;
});

afterEach(() => cleanup());

describe('NotificationMenu', () => {
  it('shows browser Web Push outside the native shell', () => {
    render(<MemoryRouter><NotificationMenu /></MemoryRouter>);
    expect(screen.getByTestId('web-push-control')).toBeTruthy();
  });

  it('hides browser Web Push in the native shell', () => {
    native.shell = true;
    render(<MemoryRouter><NotificationMenu /></MemoryRouter>);
    expect(screen.queryByTestId('web-push-control')).toBeNull();
  });
});
