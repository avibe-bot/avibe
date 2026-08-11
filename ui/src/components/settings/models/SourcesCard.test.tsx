// @vitest-environment jsdom
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { SourcesCard } from './SourcesCard';

describe('SourcesCard footer', () => {
  it('draws the two Frame 01 commands without restoring the retired vendor menu', async () => {
    const onAddApiKey = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <SourcesCard sources={[]} readState="ready" onRetry={vi.fn()} onOpenSource={vi.fn()} onAddApiKey={onAddApiKey} />
      </I18nextProvider>,
    );

    const subscription = screen.getByRole('button', { name: /Add subscription|添加订阅/i });
    expect((subscription as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole('button', { name: /^Add source$|^添加来源$/i })).toBeNull();

    await userEvent.click(screen.getByRole('button', { name: /Add API key|添加 API Key/i }));
    expect(onAddApiKey).toHaveBeenCalledOnce();
  });
});
