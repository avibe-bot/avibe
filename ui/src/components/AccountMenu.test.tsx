/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AccountMenu } from './AccountMenu';

const auth = vi.hoisted(() => ({
  email: 'alex@example.com' as string | null,
  signingOut: false,
  signOut: vi.fn(),
}));

vi.mock('../lib/useAuthAccount', () => ({
  useAuthAccount: () => auth,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

beforeEach(() => {
  auth.email = 'alex@example.com';
  auth.signingOut = false;
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('AccountMenu', () => {
  it('closes its open menu and marks Escape as consumed', () => {
    render(<AccountMenu />);
    fireEvent.click(screen.getByRole('button', { name: 'appShell.accountMenuLabel' }));

    const menuItem = screen.getByRole('menuitem');
    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
    act(() => menuItem.dispatchEvent(event));

    expect(event.defaultPrevented).toBe(true);
    expect(screen.queryByRole('menu')).toBeNull();
  });
});
