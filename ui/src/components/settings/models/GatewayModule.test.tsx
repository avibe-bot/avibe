// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { GatewayModule } from './GatewayModule';
import { failRegionRead, readyRegion } from './regionRead';
import type { AgentSupply } from './types';

const agent: AgentSupply = {
  backend: 'claude',
  cli_present: true,
  mode: 'direct',
  menu_kind: 'fixed',
  sources: null,
  routes: null,
  supply_status: null,
  model_supply: null,
  builtin_models: [],
  named_agents: [],
};

afterEach(cleanup);

describe('GatewayModule region failure treatment', () => {
  it('fills a narrow overview column without preserving an intrinsic panel width', () => {
    render(
      <I18nextProvider i18n={i18n}>
        <GatewayModule
          supply={readyRegion([])}
          runtime={null}
          runtimeSnapshot={null}
          onRetry={vi.fn()}
          sources={[]}
          chains={{}}
          pendingBackends={new Set()}
          switchFailures={new Set()}
          connectingBackend={null}
          onConnectHub={vi.fn()}
          onSwitchDirect={vi.fn()}
          onOpenModels={vi.fn()}
          onOpenOrder={vi.fn()}
          onOpenRoute={vi.fn()}
          onProbeSettled={vi.fn()}
        />
      </I18nextProvider>,
    );

    const panel = screen.getByRole('heading', { name: /Gateway routes|路由/i }).closest('section');
    expect(panel?.className).toContain('w-full');
    expect(panel?.className).toContain('min-w-0');
    expect(panel?.className).not.toContain('max-h-full');
    expect(panel?.children.item(1)?.className).not.toContain('overflow-y-auto');
  });

  it('keeps the last good Agent rows visible with an F2 retry after a later read fails', async () => {
    const onRetry = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <GatewayModule
          supply={failRegionRead(readyRegion([agent]))}
          runtime={null}
          runtimeSnapshot={null}
          onRetry={onRetry}
          sources={[]}
          chains={{}}
          pendingBackends={new Set()}
          switchFailures={new Set()}
          connectingBackend={null}
          onConnectHub={vi.fn()}
          onSwitchDirect={vi.fn()}
          onOpenModels={vi.fn()}
          onOpenOrder={vi.fn()}
          onOpenRoute={vi.fn()}
          onProbeSettled={vi.fn()}
        />
      </I18nextProvider>,
    );

    expect(screen.getByText(/^Claude Code$/i)).toBeTruthy();
    expect(screen.getByText(/Could not read this backend's supply|没有读到后端列表/i)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: /^Retry$|^重试$/i }));
    expect(onRetry).toHaveBeenCalledOnce();
  });
});
