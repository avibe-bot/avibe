// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { cleanup, render, screen } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { RouteChainDialog } from './RouteChainDialog';
import type { AgentChain, AgentSupply, Source } from './types';

const agent: AgentSupply = { backend: 'claude', mode: 'hub', menu_kind: 'fixed' };
const sources: Source[] = [
  { id: 'src_a', last_discovered_at: null, kind: 'api_key', vendor: 'anthropic', display_name: 'aihub', protocol: 'anthropic', supply_channel: 'hub', billing: 'metered', state: { status: 'active', retry_at: null, detail_key: null }, models: [] },
  { id: 'src_b', last_discovered_at: null, kind: 'subscription', vendor: 'anthropic', display_name: 'Claude subscription', protocol: 'anthropic', supply_channel: 'native_cli', billing: 'monthly', state: { status: 'active', retry_at: null, detail_key: null }, models: [] },
];
const chain: AgentChain = {
  contract_version: 5,
  backend: 'claude',
  model_id: 'opus-5',
  current: { source_id: 'src_b', model_id: 'opus-5' },
  chain: [
    { source_id: 'src_a', model_id: 'claude-opus-5', channel: 'hub', health: 'cooldown', runnable: false, reason: null, retry_at: '2099-01-01T00:00:00Z' },
    { source_id: 'src_b', model_id: 'opus-5', channel: 'native_cli', health: 'healthy', runnable: true, reason: null, retry_at: null },
  ],
  supply_state: 'ok',
};

afterEach(cleanup);

describe('RouteChainDialog', () => {
  it('renders the contracted chain order and current hop', () => {
    render(<I18nextProvider i18n={i18n}><RouteChainDialog selection={{ agent, modelId: 'opus-5', read: { kind: 'ready', chain } }} sources={sources} onClose={vi.fn()} /></I18nextProvider>);

    expect(screen.getByText('opus-5 · Route chain')).toBeTruthy();
    const current = document.querySelector('[data-current="true"]');
    expect(current?.textContent).toContain('Claude subscription');
    expect(current?.textContent).toContain('opus-5');
  });

  it('draws every write control disabled while G-32 remains unresolved', () => {
    render(<I18nextProvider i18n={i18n}><RouteChainDialog selection={{ agent, modelId: 'opus-5', read: { kind: 'ready', chain } }} sources={sources} onClose={vi.fn()} /></I18nextProvider>);

    for (const name of ['Remove hop', 'Add a hop', 'Reorder by source order', 'Save']) {
      const controls = screen.getAllByRole('button', { name });
      expect(controls.length).toBeGreaterThan(0);
      expect(controls.every((control) => (control as HTMLButtonElement).disabled)).toBe(true);
    }
    expect(screen.getAllByRole('button', { name: 'Cancel' }).every((control) => !(control as HTMLButtonElement).disabled)).toBe(true);
  });

  it('has no mutation client or write callback wired into the frame', () => {
    const source = readFileSync(join(__dirname, 'RouteChainDialog.tsx'), 'utf8');
    const api = readFileSync(join(__dirname, 'modelsApi.ts'), 'utf8');

    expect(source).not.toMatch(/modelsApi|putAgentChain|onCommit|onSave/);
    expect(api).not.toMatch(/putAgentChain|\/chain[^\n]*jsonInit\(['"]PUT/);
  });
});
