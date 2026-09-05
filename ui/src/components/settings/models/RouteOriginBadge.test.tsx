// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { RouteOriginBadge } from './RouteOriginBadge';

afterEach(cleanup);
describe('route origin help', () => {
  it('opens on focus, dismisses on Escape, and touch does not open the route', async () => {
    const user = userEvent.setup();
    const route = vi.fn();
    render(<I18nextProvider i18n={i18n}><div onClick={route}><RouteOriginBadge origin="passthrough" backend="codex" /></div></I18nextProvider>);
    await user.tab();
    expect(await screen.findByText(/eligible API-key providers/)).toBeTruthy();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByText(/eligible API-key providers/)).toBeNull());
    fireEvent.click(screen.getByRole('button', { name: 'Passthrough' }));
    expect(await screen.findByText(/eligible API-key providers/)).toBeTruthy();
    expect(route).not.toHaveBeenCalled();
  });
});
