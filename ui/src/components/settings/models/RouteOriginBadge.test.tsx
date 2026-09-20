// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { RouteOriginBadge } from './RouteOriginBadge';
import { useState } from 'react';

const ControlledBadge = () => {
  const [open, setOpen] = useState(false);
  return <RouteOriginBadge origin="passthrough" backend="codex" open={open} onOpenChange={setOpen} />;
};

afterEach(cleanup);
describe('route origin help', () => {
  it('keeps dialog provenance noninteractive and outside the collection help state', () => {
    render(<I18nextProvider i18n={i18n}><RouteOriginBadge origin="manual" backend="codex" interactive={false} /></I18nextProvider>);
    expect(screen.getByText('Manual').tagName).toBe('SPAN');
    expect(screen.queryByRole('button')).toBeNull();
    expect(document.querySelector('.model-hub-origin-help')).toBeNull();
  });

  it('opens on focus, dismisses on Escape, and touch does not open the route', async () => {
    const user = userEvent.setup();
    const route = vi.fn();
    render(<I18nextProvider i18n={i18n}><div onClick={route}><ControlledBadge /></div></I18nextProvider>);
    await user.tab();
    expect(await screen.findByText(/eligible API-key providers/)).toBeTruthy();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByText(/eligible API-key providers/)).toBeNull());
    fireEvent.click(screen.getByRole('button', { name: 'Passthrough' }));
    expect(await screen.findByText(/eligible API-key providers/)).toBeTruthy();
    expect(route).not.toHaveBeenCalled();
  });
});
