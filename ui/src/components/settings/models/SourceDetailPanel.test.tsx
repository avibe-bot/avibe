// @vitest-environment jsdom
import * as React from 'react';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '@/context/ToastProvider';
import i18n from '@/i18n';
import { modelsApi } from './modelsApi';
import { GuardGapList, SourceDetailPanel } from './SourceDetailPanel';
import type { Source } from './types';

const source: Source = {
  id: 'src_detail',
  last_discovered_at: '2026-08-11T08:00:00Z',
  kind: 'api_key',
  vendor: 'anthropic',
  display_name: 'Production key',
  protocol: 'anthropic',
  base_url: 'https://relay.example/v1',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active', retry_at: null, detail_key: null },
  models: [{ id: 'model-a', display_name: null, provenance: 'manual', reasoning_efforts: ['high'] }],
};

const renderPanel = (adoptedBy: React.ComponentProps<typeof SourceDetailPanel>['adoptedBy'] = undefined) => render(
  <ToastProvider>
    <I18nextProvider i18n={i18n}>
      <SourceDetailPanel source={source} adoptedBy={adoptedBy} onMutation={vi.fn().mockResolvedValue(undefined)} />
    </I18nextProvider>
  </ToastProvider>,
);

const EchoPanel: React.FC<{ reconcile?: () => Promise<void> | void }> = ({ reconcile = vi.fn() }) => {
  const [current, setCurrent] = React.useState(source);
  const onMutation = async (echoed?: Source) => {
    if (echoed) setCurrent(echoed);
    await reconcile();
  };
  return <SourceDetailPanel source={current} onMutation={onMutation} />;
};

const renderEchoPanel = (reconcile = vi.fn()) => render(
  <ToastProvider>
    <I18nextProvider i18n={i18n}>
      <EchoPanel reconcile={reconcile} />
    </I18nextProvider>
  </ToastProvider>,
);

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('SourceDetailPanel', () => {
  it('keeps the detail surface to inventory, entry kind, tiers, and refetch', () => {
    renderPanel();
    expect(screen.queryByText(/latency|延迟|enrollment|protocol|协议/i)).toBeNull();
    expect(screen.queryByText(/^Standby$|^待命$/i)).toBeNull();
    expect(screen.getByText('model-a')).toBeTruthy();
    expect(screen.getByText(/relay\.example/)).toBeTruthy();
  });

  it('shows in-use only when the response carries adoption for this source', () => {
    renderPanel([{ backend: 'claude', menu_model: 'claude-opus-4-6' }]);
    expect(screen.getByText(/^In use$|^使用中$/i)).toBeTruthy();
  });

  it('omits native refetch because that channel has no stored discovery credential', () => {
    render(<I18nextProvider i18n={i18n}><SourceDetailPanel source={{ ...source, kind: 'subscription', supply_channel: 'native_cli' }} onMutation={vi.fn().mockResolvedValue(undefined)} /></I18nextProvider>);
    expect(screen.queryByRole('button', { name: /^Refetch$|^重新拉取$/i })).toBeNull();
  });

  it('discards an uncommitted tier on Escape', async () => {
    renderPanel();
    await userEvent.click(screen.getByRole('button', { name: /high/i }));
    const input = screen.getByPlaceholderText(/Enter to add|回车添加/i);
    await userEvent.type(input, 'draft{Escape}');
    expect(screen.queryByDisplayValue('draft')).toBeNull();
  });

  it('opens the manual model draft in the table and keeps Add disabled while blank', async () => {
    renderPanel();
    await userEvent.click(screen.getAllByRole('button', { name: /^Add model$|^添加模型$/i })[0]);
    const input = screen.getByPlaceholderText(/^Model ID$|^型号 ID$/i);
    expect(input).toBeTruthy();
    const draft = input.closest('[data-manual-model-draft]');
    expect(draft).toBeTruthy();
    const add = within(draft as HTMLElement).getByRole('button', { name: /^Add model$|^添加模型$/i });
    expect((add as HTMLButtonElement).disabled).toBe(true);
  });

  it('keeps a failed refetch visible beside the inventory it could not replace', async () => {
    vi.spyOn(modelsApi, 'refreshSource').mockRejectedValueOnce(new Error('lost response'));
    renderPanel();
    await userEvent.click(screen.getByRole('button', { name: /^Refetch$|^重新拉取$/i }));
    expect(await screen.findByText(/fetch did not come back|拉取没有回来/i)).toBeTruthy();
  });

  it('applies the refetch Source echo before the collection refresh settles', async () => {
    const echoed = {
      ...source,
      models: [...source.models, { id: 'model-b', display_name: null, provenance: 'discovered' as const, reasoning_efforts: [] }],
    };
    const refresh = vi.spyOn(modelsApi, 'refreshSource').mockResolvedValueOnce({ source: echoed, discovered: echoed.models.length });
    const reconcile = vi.fn().mockResolvedValue(undefined);
    renderEchoPanel(reconcile);

    await userEvent.click(screen.getByRole('button', { name: /^Refetch$|^重新拉取$/i }));

    expect(await screen.findByText('model-b')).toBeTruthy();
    expect(refresh).toHaveBeenCalledOnce();
    expect(reconcile).toHaveBeenCalledOnce();
  });

  it('applies the manual-create Source echo without waiting for a collection read', async () => {
    const echoed = {
      ...source,
      models: [...source.models, { id: 'model-b', display_name: null, provenance: 'manual' as const, reasoning_efforts: [] }],
    };
    vi.spyOn(modelsApi, 'addCustomModel').mockResolvedValueOnce(echoed);
    renderEchoPanel();

    await userEvent.click(screen.getAllByRole('button', { name: /^Add model$|^添加模型$/i })[0]);
    const draft = screen.getByPlaceholderText(/^Model ID$|^型号 ID$/i).closest('[data-manual-model-draft]');
    await userEvent.type(within(draft as HTMLElement).getByPlaceholderText(/^Model ID$|^型号 ID$/i), 'model-b');
    await userEvent.click(within(draft as HTMLElement).getByRole('button', { name: /^Add model$|^添加模型$/i }));

    expect(await screen.findByText('model-b')).toBeTruthy();
  });

  it('applies the manual-delete Source echo without waiting for a collection read', async () => {
    vi.spyOn(modelsApi, 'deleteCustomModel').mockResolvedValueOnce({ ...source, models: [] });
    renderEchoPanel();

    await userEvent.click(screen.getByRole('button', { name: /Remove model-a|移除 model-a/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Remove$|^移除$/i }));

    await waitFor(() => expect(screen.queryByText('model-a')).toBeNull());
  });

  it('keeps a rejected tier draft and offers an inline retry', async () => {
    vi.spyOn(modelsApi, 'updateModelReasoningEfforts').mockRejectedValueOnce(new Error('write failed'));
    renderPanel();
    await userEvent.click(screen.getByRole('button', { name: /high/i }));
    const input = screen.getByPlaceholderText(/Enter to add|回车添加/i);
    await userEvent.type(input, 'draft{Enter}');
    expect(await screen.findByText(/tier was not saved|档位没保存上/i)).toBeTruthy();
    expect((input as HTMLInputElement).value).toBe('draft');
    expect(screen.getByRole('button', { name: /^Try again$|^重试$/i })).toBeTruthy();
  });

  it('sends a manual-model removal before showing any guarded-change confirm', async () => {
    const remove = vi.spyOn(modelsApi, 'deleteCustomModel').mockResolvedValueOnce(source);
    renderPanel();
    await userEvent.click(screen.getByRole('button', { name: /Remove model-a|移除 model-a/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Remove$|^移除$/i }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(source.id, 'model-a', false));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('names every model and Agent that a guarded change would interrupt', () => {
    render(
      <I18nextProvider i18n={i18n}>
        <GuardGapList gaps={[{ backend: 'claude', model_id: 'claude-opus-4-6', agents: ['Release bot', 'Triage'] }]} />
      </I18nextProvider>,
    );

    expect(screen.getByText(/Claude Code · claude-opus-4-6/)).toBeTruthy();
    expect(screen.getByText(/Release bot.*Triage/)).toBeTruthy();
  });

  it('re-reads after an unconfirmed manual deletion before offering another DELETE', async () => {
    const remove = vi.spyOn(modelsApi, 'deleteCustomModel').mockRejectedValueOnce(new TypeError('response lost'));
    const list = vi.spyOn(modelsApi, 'listSources').mockResolvedValueOnce([{ ...source, models: [] }]);
    const onMutation = vi.fn().mockResolvedValue(undefined);
    render(<I18nextProvider i18n={i18n}><SourceDetailPanel source={source} onMutation={onMutation} /></I18nextProvider>);

    await userEvent.click(screen.getByRole('button', { name: /Remove model-a|移除 model-a/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Remove$|^移除$/i }));

    await waitFor(() => expect(onMutation).toHaveBeenCalledWith(expect.objectContaining({ models: [] })));
    expect(list).toHaveBeenCalledOnce();
    expect(remove).toHaveBeenCalledOnce();
    expect(screen.queryByRole('button', { name: /^Try again$|^重试$/i })).toBeNull();
  });

  it('applies the reconciled deletion Source before the follow-up refresh settles', async () => {
    vi.spyOn(modelsApi, 'deleteCustomModel').mockRejectedValueOnce(new TypeError('response lost'));
    vi.spyOn(modelsApi, 'listSources').mockResolvedValueOnce([{ ...source, models: [] }]);
    let finishRefresh: (() => void) | undefined;
    const refresh = vi.fn(() => new Promise<void>((resolve) => { finishRefresh = resolve; }));
    renderEchoPanel(refresh);

    await userEvent.click(screen.getByRole('button', { name: /Remove model-a|移除 model-a/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Remove$|^移除$/i }));

    await waitFor(() => expect(screen.queryByText('model-a')).toBeNull());
    expect(refresh).toHaveBeenCalledOnce();
    finishRefresh?.();
  });

  it('offers a second DELETE only after the authoritative list proves the model remains', async () => {
    const remove = vi.spyOn(modelsApi, 'deleteCustomModel')
      .mockRejectedValueOnce(new TypeError('response lost'))
      .mockResolvedValueOnce({ ...source, models: [] });
    const list = vi.spyOn(modelsApi, 'listSources').mockResolvedValueOnce([source]);
    renderPanel();

    await userEvent.click(screen.getByRole('button', { name: /Remove model-a|移除 model-a/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Remove$|^移除$/i }));
    await userEvent.click(await screen.findByRole('button', { name: /^Try again$|^重试$/i }));

    await waitFor(() => expect(remove).toHaveBeenCalledTimes(2));
    expect(list).toHaveBeenCalledOnce();
  });
});
