// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { readyRegion } from './regionRead';
import { SourcesCard } from './SourcesCard';

afterEach(cleanup);

describe('SourcesCard footer', () => {
  it('exposes the upstream info note to keyboard activation and Escape dismissal', async () => {
    const user = userEvent.setup();
    render(
      <I18nextProvider i18n={i18n}>
        <SourcesCard read={readyRegion([])} onRetry={vi.fn()} onOpenSource={vi.fn()} onAddApiKey={vi.fn()} />
      </I18nextProvider>,
    );

    const info = screen.getByRole('button', { name: /What the gateway is|什么是网关/i });
    await user.tab();
    expect(document.activeElement).toBe(info);
    await user.keyboard('[Enter]');
    expect(await screen.findByText(/dispatch layer|调度/i)).toBeTruthy();
    await user.keyboard('[Escape]');
    await waitFor(() => expect(screen.queryByText(/dispatch layer|调度/i)).toBeNull());
  });

  it('draws the two Frame 01 commands without restoring the retired vendor menu', async () => {
    const onAddApiKey = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <SourcesCard read={readyRegion([])} onRetry={vi.fn()} onOpenSource={vi.fn()} onAddApiKey={onAddApiKey} />
      </I18nextProvider>,
    );

    const subscription = screen.getByRole('button', { name: /Add subscription|添加订阅/i });
    expect((subscription as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole('button', { name: /^Add source$|^添加来源$/i })).toBeNull();

    await userEvent.click(screen.getByRole('button', { name: /Add API key|添加 API Key/i }));
    expect(onAddApiKey).toHaveBeenCalledOnce();
  });
});
