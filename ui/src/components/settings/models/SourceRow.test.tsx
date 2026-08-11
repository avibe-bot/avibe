// @vitest-environment jsdom
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { SourceRow } from './SourceRow';
import type { Source } from './types';

const source: Source = {
  id: 'src_a', last_discovered_at: null, kind: 'api_key', vendor: 'anthropic', display_name: 'Production',
  protocol: 'anthropic', base_url: null, supply_channel: 'hub', billing: 'metered',
  state: { status: 'standby', retry_at: null, detail_key: null }, masked_credential: 'sk-ant-…1234', models: [],
};

describe('SourceRow', () => {
  it('opens the Source detail without exposing inline source mutations', async () => {
    const onOpen = vi.fn();
    render(<I18nextProvider i18n={i18n}><SourceRow source={source} onOpen={onOpen} /></I18nextProvider>);
    await userEvent.click(screen.getByRole('button', { name: /Production/ }));
    expect(onOpen).toHaveBeenCalledWith(source);
    expect(screen.queryByText(/latency/i)).toBeNull();
  });

  it('uses response-scoped adoption to name an active supplying source', () => {
    render(
      <I18nextProvider i18n={i18n}>
        <SourceRow
          source={{ ...source, supply_channel: 'native_cli', state: { ...source.state, status: 'active' } }}
          adoptedBy={[{ backend: 'claude', menu_model: 'claude-opus-4-6' }]}
          onOpen={vi.fn()}
        />
      </I18nextProvider>,
    );
    expect(screen.getByText(/Supplying Claude Code|正在供给 Claude Code/i)).toBeTruthy();
  });
});
