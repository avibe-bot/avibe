// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { SourceRow } from './SourceRow';
import type { Source } from './types';

const source: Source = {
  id: 'src_a', last_discovered_at: null, kind: 'api_key', vendor: 'anthropic', display_name: 'Production',
  protocol: 'anthropic', base_url: null, supply_channel: 'hub', billing: 'metered',
  state: { status: 'standby', retry_at: null, detail_key: null }, masked_credential: 'sk-ant-…1234', models: [],
};

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.useRealTimers();
});

describe('SourceRow', () => {
  it.each(['en', 'zh'])('shows a saved source without a warning or a health claim in %s', (lng) => {
    const locale = i18n.cloneInstance({ lng });
    render(<I18nextProvider i18n={locale}><SourceRow source={{
      ...source, verification_pending: 'vp_fixture', adopted_by: [{ backend: 'codex', menu_model: 'gpt-5' }],
    }} onOpen={vi.fn()} /></I18nextProvider>);
    const label = screen.getByText(locale.t('settings.models.sourceDetail.status.saved'));
    expect(label.classList.contains('text-muted')).toBe(true);
    expect(label.querySelector('.bg-muted')).not.toBeNull();
    expect(screen.queryByText(/Supplying Codex|正在使用 Codex/)).toBeNull();
  });

  it('opens the Source detail without exposing inline source mutations', async () => {
    const onOpen = vi.fn();
    render(<I18nextProvider i18n={i18n}><SourceRow source={source} onOpen={onOpen} /></I18nextProvider>);
    const opener = screen.getByRole('button', { name: /Production/ });
    await userEvent.click(opener);
    expect(onOpen).toHaveBeenCalledWith(source, opener);
    expect(screen.queryByText(/latency/i)).toBeNull();
    expect(screen.getByText('Anthropic Messages')).toBeTruthy();
  });

  it('explains that a healthy source is not currently supplying a route', () => {
    render(<I18nextProvider i18n={i18n}><SourceRow source={source} onOpen={vi.fn()} /></I18nextProvider>);
    expect(screen.getByText(/Available · not currently supplying|可用 · 当前未使用/i)).toBeTruthy();
  });

  it('puts the highlighted API key badge before the neutral protocol without a host prefix', () => {
    const { container } = render(
      <I18nextProvider i18n={i18n}>
        <SourceRow
          source={{ ...source, vendor: 'custom', base_url: 'https://relay.example/v1' }}
          onOpen={vi.fn()}
        />
      </I18nextProvider>,
    );
    const pills = container.querySelectorAll('.model-hub-pill');
    expect(pills).toHaveLength(2);
    expect(pills[0].textContent).toMatch(/API key|API Key/);
    expect(pills[0].classList.contains('model-hub-accent-pill--cyan')).toBe(true);
    expect(pills[1].textContent).toBe('Anthropic Messages');
    expect(pills[1].getAttribute('title')).toBe('Anthropic Messages');
    // The endpoint remains in the independent connection-detail line.
    expect(screen.getByText('relay.example/v1 · sk-ant-…1234')).toBeTruthy();
  });

  it.each(['en', 'zh'])('hides accounts across cards without opening them, and remembers the preference in %s', async (lng) => {
    const locale = i18n.cloneInstance({ lng });
    const onOpen = vi.fn();
    const subscription: Source = {
      ...source, kind: 'subscription', display_name: 'OpenAI 2', vendor: 'openai',
      account_label: '账号@example.com', masked_credential: null,
    };
    const tree = <I18nextProvider i18n={locale}>
      <SourceRow source={subscription} onOpen={onOpen} />
      <SourceRow source={{ ...subscription, id: 'src_b', display_name: 'OpenAI 3', account_label: 'second@example.com' }} onOpen={onOpen} />
    </I18nextProvider>;
    const { container, unmount } = render(tree);
    const user = userEvent.setup();
    expect(container.querySelector('button button')).toBeNull();
    expect(container.querySelector('[data-source-account]')?.textContent).toBe('账号@example.com');
    expect(container.querySelector('.model-hub-pill')?.textContent).toBe(locale.t('settings.models.upstream.kind.subscription'));
    await user.click(screen.getAllByRole('button', { name: locale.t('settings.models.upstream.hideAccount') })[0]);
    expect(onOpen).not.toHaveBeenCalled();
    expect(container.innerHTML).not.toContain('账号@example.com');
    expect(container.innerHTML).not.toContain('second@example.com');
    unmount();
    const remounted = render(tree);
    expect(remounted.container.innerHTML).not.toContain('账号@example.com');
    const reveal = screen.getAllByRole('button', { name: locale.t('settings.models.upstream.showAccount') })[0];
    reveal.focus();
    await user.keyboard('{Enter}');
    expect(screen.getByText('账号@example.com')).toBeTruthy();
    expect(onOpen).not.toHaveBeenCalled();
    const opener = screen.getByRole('button', { name: 'OpenAI 2' });
    await user.click(opener);
    expect(onOpen).toHaveBeenCalledWith(subscription, opener);
  });

  it('does not invent an account or eye control when the provider has no identity metadata', () => {
    const { container } = render(<I18nextProvider i18n={i18n}>
      <SourceRow source={{ ...source, kind: 'subscription', account_label: null }} onOpen={vi.fn()} />
    </I18nextProvider>);
    expect(container.querySelector('[data-source-account]')).toBeNull();
    expect(screen.getAllByRole('button')).toHaveLength(1);
  });

  it('still hides the account when browser storage rejects the preference', async () => {
    const { container } = render(<I18nextProvider i18n={i18n}>
      <SourceRow source={{ ...source, kind: 'subscription', account_label: 'private@example.com' }} onOpen={vi.fn()} />
    </I18nextProvider>);
    const storage = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Storage unavailable', 'QuotaExceededError');
    });
    try {
      await userEvent.click(screen.getByRole('button', { name: i18n.t('settings.models.upstream.hideAccount') }));
      expect(container.innerHTML).not.toContain('private@example.com');
    } finally {
      storage.mockRestore();
      await userEvent.click(screen.getByRole('button', { name: i18n.t('settings.models.upstream.showAccount') }));
    }
    expect(screen.getByText('private@example.com')).toBeTruthy();
  });

  it('uses the authoritative Source adoption to name an active supplying source', () => {
    render(
      <I18nextProvider i18n={i18n}>
        <SourceRow
          source={{ ...source, supply_channel: 'native_cli', state: { ...source.state, status: 'active' }, adopted_by: [{ backend: 'claude', menu_model: 'claude-opus-4-6' }] }}
          onOpen={vi.fn()}
        />
      </I18nextProvider>,
    );
    expect(screen.getByText(/Supplying Claude Code|正在使用 Claude Code/i)).toBeTruthy();
  });

  it('consumes persisted adoption when the source projection carries it', () => {
    render(<I18nextProvider i18n={i18n}><SourceRow source={{ ...source, state: { ...source.state, status: 'standby' }, adopted_by: [{ backend: 'codex', menu_model: 'gpt-5' }] }} onOpen={vi.fn()} /></I18nextProvider>);
    expect(screen.getByText(/Supplying Codex|正在使用 Codex/i)).toBeTruthy();
  });

  it('does not show a cached adoption after that backend switches to direct mode', () => {
    render(
      <I18nextProvider i18n={i18n}>
        <SourceRow
          source={{ ...source, state: { ...source.state, status: 'standby' }, adopted_by: [{ backend: 'codex', menu_model: 'gpt-5' }] }}
          activeBackends={new Set(['claude'])}
          onOpen={vi.fn()}
        />
      </I18nextProvider>,
    );
    expect(screen.queryByText(/Supplying Codex|正在使用 Codex/i)).toBeNull();
    expect(screen.getByText(/Available · not currently supplying|可用 · 当前未使用/i)).toBeTruthy();
  });

  it('advances cooldown copy when its retry deadline passes', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-11T14:00:00Z'));
    render(
      <I18nextProvider i18n={i18n}>
        <SourceRow
          source={{ ...source, state: { status: 'cooldown', retry_at: '2026-08-11T14:01:00Z', detail_key: null } }}
          onOpen={vi.fn()}
        />
      </I18nextProvider>,
    );

    expect(screen.getByText(/retrying automatically after|后自动重试/i)).toBeTruthy();
    act(() => vi.advanceTimersByTime(60_000));
    expect(screen.getByText(/retry is due|已到重试时间/i)).toBeTruthy();
  });
});
