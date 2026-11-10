/** @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

const native = vi.hoisted(() => ({ shell: false }));
vi.mock('../../lib/native', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/native')>()),
  isNativeShell: () => native.shell,
}));
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
  native.shell = false;
});

describe('NotificationMenu', () => {
  it('shows the native push control when the instance can be read', () => {
    native.shell = true;
    render(<MemoryRouter><NotificationMenu /></MemoryRouter>);
    expect(screen.getByTestId('web-push-control')).toBeTruthy();
  });
});
