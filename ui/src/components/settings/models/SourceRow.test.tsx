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
  vi.useRealTimers();
});

describe('SourceRow', () => {
  it('opens the Source detail without exposing inline source mutations', async () => {
    const onOpen = vi.fn();
    render(<I18nextProvider i18n={i18n}><SourceRow source={source} onOpen={onOpen} /></I18nextProvider>);
    const opener = screen.getByRole('button', { name: /Production/ });
    await userEvent.click(opener);
    expect(onOpen).toHaveBeenCalledWith(source, opener);
    expect(screen.queryByText(/latency/i)).toBeNull();
    expect(screen.getByText('Anthropic · Anthropic Messages')).toBeTruthy();
  });

  it('explains that a healthy source is not currently supplying a route', () => {
    render(<I18nextProvider i18n={i18n}><SourceRow source={source} onOpen={vi.fn()} /></I18nextProvider>);
    expect(screen.getByText(/Available · not currently supplying|可用 · 当前未使用/i)).toBeTruthy();
  });

  it('labels a custom upstream by host and protocol', () => {
    render(
      <I18nextProvider i18n={i18n}>
        <SourceRow
          source={{ ...source, vendor: 'custom', base_url: 'https://relay.example/v1' }}
          onOpen={vi.fn()}
        />
      </I18nextProvider>,
    );
    expect(screen.getByText('relay.example · Anthropic Messages')).toBeTruthy();
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
