// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import * as React from 'react';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '@/context/ToastProvider';
import i18n from '@/i18n';
import { createPendingWrites } from './asyncLifetime';
import { MANAGE_DESTINATION, type ManageKind } from './manage';
import { ApiCallError, modelsApi } from './modelsApi';
import type { SourceMutationSettlement, TrackSourceMutation } from './mutationSettlement';
import { GuardGapList } from './GuardGapList';
import { REPAIR_DESTINATION, REPAIR_LABEL_KEY, repairAction, type RepairKind } from './repair';
import { SourceDetailPanel } from './SourceDetailPanel';
import {
  COOLDOWN_DETAIL_KEYS,
  ERROR_DETAIL_KEYS,
  NEEDS_ACTION_DETAIL_KEYS,
  SOURCE_STATUSES,
} from './types';
import type { Source, SourceDetailKey, SourceKind, SupplyChannel } from './types';

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
  credential_ref: 'cred_detail',
  state: { status: 'active', retry_at: null, detail_key: null },
  models: [{ id: 'model-a', display_name: null, origin: 'manual', reasoning_efforts: ['high'] }],
};

const heldHops = [{
  backend: 'claude' as const,
  menu_model: 'claude-opus-4-6',
  position: 1,
  source_id: source.id,
  model_id: 'model-a',
}];
const heldGaps = [{
  backend: 'claude' as const,
  model_id: 'claude-opus-4-6',
  agents: ['Release bot'],
}];

const guardRefusal = () => new ApiCallError(
  'source_last_supplier',
  undefined,
  true,
  heldGaps,
  [],
  heldHops,
  409,
);

type UnknownWriteAction = 'edit' | 'delete';
const UNKNOWN_WRITE_CASES = (['edit', 'delete'] as const).flatMap((action) =>
  ([false, true] as const).flatMap((forced) =>
    (['committed', 'absent', 'unread'] as const).map((outcome) => ({ action, forced, outcome }))));

const submitManagementWrite = async (action: UnknownWriteAction, forced: boolean) => {
  await userEvent.click(screen.getByRole('button', { name: /Manage Production key|管理 Production key/i }));
  if (action === 'edit') {
    await userEvent.click(screen.getByRole('menuitem', { name: /^Edit source$|^编辑来源$/i }));
    const name = screen.getByLabelText(/^Display name$|^显示名称$/i);
    await userEvent.clear(name);
    await userEvent.type(name, 'Unknown edit');
    await userEvent.click(screen.getByRole('button', { name: /^Save$|^保存$/i }));
    if (forced) {
      await userEvent.click(await screen.findByRole('button', { name: /^Save anyway$|^仍要保存$/i }));
    }
    return;
  }

  await userEvent.click(screen.getByRole('menuitem', { name: /^Remove source$|^移除来源$/i }));
  await userEvent.click(screen.getByRole('button', { name: /^Remove source$|^移除来源$/i }));
  if (forced) {
    await userEvent.click(await screen.findByRole('button', { name: /^Remove source$|^移除来源$/i }));
  }
};

const noReauth = () => {
  throw new Error('this source has no re-login entry to reach');
};

/**
 * A stopped subscription whose cause IS the credential — `repairAction` rule 1,
 * so the one tap is a re-login. Parameterized by channel because that is the one
 * thing the two blocked subscriptions do not share: what starting the login costs.
 */
const blockedSubscription = (supply_channel: SupplyChannel): Source => ({
  ...source,
  kind: 'subscription',
  supply_channel,
  state: { status: 'needs_action', retry_at: null, detail_key: 'models.source.needs_action.oauth_expired' },
});

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};

let sourceSnapshot = 0;
const beginSourceSnapshot = () => ++sourceSnapshot;
const settlement = (overrides: Partial<SourceMutationSettlement> = {}): SourceMutationSettlement => ({
  source: vi.fn().mockResolvedValue('landed'),
  gone: vi.fn().mockResolvedValue('landed'),
  unread: vi.fn().mockResolvedValue('landed'),
  release: vi.fn(),
  readInventory: async () => ({ snapshot: beginSourceSnapshot(), sources: await modelsApi.listSources() }),
  ...overrides,
});
const immediateTrack: TrackSourceMutation = async (work) => work(source, settlement());
type MutationScheduler = <T>(work: () => Promise<T>) => Promise<T>;
const serializedTrack = (): MutationScheduler => {
  const writes = createPendingWrites(() => {});
  return async <T,>(work: () => Promise<T>): Promise<T> => {
    let result!: T;
    await writes.track(source.id, async () => { result = await work(); });
    return result;
  };
};

const renderPanel = (adoptedBy: Source['adopted_by'] = undefined) => render(
  <ToastProvider>
    <I18nextProvider i18n={i18n}>
      <SourceDetailPanel source={{ ...source, adopted_by: adoptedBy }} trackMutation={immediateTrack} onReauth={noReauth} />
    </I18nextProvider>
  </ToastProvider>,
);

const EchoPanel: React.FC<{ reconcile?: () => Promise<void> | void; scheduler?: MutationScheduler }> = ({ reconcile = vi.fn(), scheduler = async (work) => work() }) => {
  const [current, setCurrent] = React.useState<Source | null>(source);
  const currentRef = React.useRef<Source | null>(current);
  currentRef.current = current;
  const trackMutation: TrackSourceMutation = (work) => scheduler(async () => {
    const latest = currentRef.current;
    if (!latest) throw new Error('Source is gone');
    return work(latest, settlement({
      source: async (echoed) => { currentRef.current = echoed; setCurrent(echoed); await reconcile(); return 'landed'; },
      gone: async () => { currentRef.current = null; setCurrent(null); await reconcile(); return 'landed'; },
      unread: async () => { await reconcile(); return 'landed'; },
    }));
  });
  return current
    ? <SourceDetailPanel source={current} trackMutation={trackMutation} onReauth={noReauth} />
    : <p data-testid="source-gone">Source gone</p>;
};

const renderEchoPanel = (reconcile = vi.fn(), scheduler?: MutationScheduler) => render(
  <ToastProvider>
    <I18nextProvider i18n={i18n}>
      <EchoPanel reconcile={reconcile} scheduler={scheduler} />
    </I18nextProvider>
  </ToastProvider>,
);

afterEach(() => {
  cleanup();
  sourceSnapshot = 0;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('SourceDetailPanel', () => {
  it('exposes a stable, programmatically focusable heading for committed navigation', () => {
    const headingRef = React.createRef<HTMLHeadingElement>();
    render(
      <I18nextProvider i18n={i18n}>
        <SourceDetailPanel source={source} headingRef={headingRef} trackMutation={immediateTrack} onReauth={noReauth} />
      </I18nextProvider>,
    );

    expect(headingRef.current).toBe(screen.getByRole('heading', { name: source.display_name }));
    expect(headingRef.current?.tabIndex).toBe(-1);
    headingRef.current?.focus();
    expect(document.activeElement).toBe(headingRef.current);
  });

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

  // The capability set is discovered from its total destination Record. A new
  // management kind therefore fails here until the rendered panel exposes a
  // trigger carrying that exact destination.
  it('gives every management capability a reachable declared destination', async () => {
    const view = renderPanel();
    await userEvent.click(screen.getByRole('button', { name: /Manage Production key|管理 Production key/i }));

    const declared = Object.keys(MANAGE_DESTINATION) as ManageKind[];
    const triggers = [...view.container.ownerDocument.querySelectorAll<HTMLElement>('[data-manage-kind]')];
    expect(new Set(triggers.map((trigger) => trigger.dataset.manageKind))).toEqual(new Set(declared));
    for (const kind of declared) {
      const trigger = triggers.find((candidate) => candidate.dataset.manageKind === kind);
      expect(trigger?.dataset.manageDestination).toBe(MANAGE_DESTINATION[kind]);
    }
  });

  it('edits the display name and endpoint through the source mutation queue', async () => {
    const updated = { ...source, display_name: 'Relay key', base_url: 'https://relay.example/v2' };
    const patch = vi.spyOn(modelsApi, 'patchSource').mockResolvedValueOnce({
      source: updated,
      removed_hops: [],
      interrupted: [],
    });
    renderEchoPanel();

    await userEvent.click(screen.getByRole('button', { name: /Manage Production key|管理 Production key/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Edit source$|^编辑来源$/i }));
    const name = screen.getByLabelText(/^Display name$|^显示名称$/i);
    const endpoint = screen.getByLabelText(/^Base URL$/i);
    await userEvent.clear(name);
    await userEvent.type(name, 'Relay key');
    await userEvent.clear(endpoint);
    await userEvent.type(endpoint, 'https://relay.example/v2/');
    await userEvent.click(screen.getByRole('button', { name: /^Save$|^保存$/i }));

    await waitFor(() => expect(patch).toHaveBeenCalledWith(source.id, {
      display_name: 'Relay key',
      base_url: 'https://relay.example/v2/',
    }));
    expect(await screen.findByRole('heading', { name: 'Relay key' })).toBeTruthy();
  });

  it('explains client validation instead of leaving a mute disabled save', async () => {
    renderEchoPanel();
    await userEvent.click(screen.getByRole('button', { name: /Manage Production key|管理 Production key/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Edit source$|^编辑来源$/i }));

    const endpoint = screen.getByLabelText(/^Base URL$/i);
    await userEvent.clear(endpoint);
    await userEvent.type(endpoint, 'https://relay.example/v2?access_token=do-not-store');
    expect(screen.getByText(/Remove credentials|移除凭据/i)).toBeTruthy();
    expect((screen.getByRole('button', { name: /^Save$|^保存$/i }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('leaves URL acceptance to the server and renders its named failure in the editor', async () => {
    const patch = vi.spyOn(modelsApi, 'patchSource')
      .mockRejectedValueOnce(new ApiCallError('discovery_failed'));
    renderEchoPanel();
    await userEvent.click(screen.getByRole('button', { name: /Manage Production key|管理 Production key/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Edit source$|^编辑来源$/i }));

    const endpoint = screen.getByLabelText(/^Base URL$/i);
    await userEvent.clear(endpoint);
    await userEvent.type(endpoint, 'https:relay.example');
    const save = screen.getByRole('button', { name: /^Save$|^保存$/i }) as HTMLButtonElement;
    expect(save.disabled).toBe(false);
    await userEvent.click(save);

    await waitFor(() => expect(patch).toHaveBeenCalledWith(source.id, { base_url: 'https:relay.example' }));
    expect(await screen.findByText(/source was not saved|来源没有保存上/i)).toBeTruthy();
    expect(screen.getByDisplayValue('https:relay.example')).toBeTruthy();
  });

  it('renders the proved protocol through its product-facing locale key', async () => {
    render(
      <I18nextProvider i18n={i18n}>
        <SourceDetailPanel
          source={{ ...source, vendor: 'custom', protocol: 'openai_chat' }}
          trackMutation={immediateTrack}
          onReauth={noReauth}
        />
      </I18nextProvider>,
    );
    await userEvent.click(screen.getByRole('button', { name: /Manage Production key|管理 Production key/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Edit source$|^编辑来源$/i }));

    expect(screen.getByText(/OpenAI Chat Completions/i)).toBeTruthy();
    expect(screen.queryByText('openai_chat')).toBeNull();
  });

  it('holds a committed edit impact envelope until the user completes the report', async () => {
    const updated = { ...source, display_name: 'Impacted source' };
    vi.spyOn(modelsApi, 'patchSource').mockResolvedValueOnce({
      source: updated,
      removed_hops: [{ backend: 'claude', menu_model: 'claude-opus-4-6', position: 1, source_id: source.id, model_id: 'model-a' }],
      interrupted: [{ backend: 'claude', model_id: 'claude-opus-4-6', agents: ['Release bot'] }],
    });
    renderEchoPanel();

    await userEvent.click(screen.getByRole('button', { name: /Manage Production key|管理 Production key/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Edit source$|^编辑来源$/i }));
    const name = screen.getByLabelText(/^Display name$|^显示名称$/i);
    await userEvent.clear(name);
    await userEvent.type(name, updated.display_name);
    await userEvent.click(screen.getByRole('button', { name: /^Save$|^保存$/i }));

    const impactDialog = await screen.findByRole('dialog', { name: /source was updated|来源已更新/i });
    expect(screen.queryByRole('heading', { name: updated.display_name })).toBeNull();
    const done = within(impactDialog)
      .getAllByRole('button', { name: /^Done$|^完成$/i })
      .find((button) => button.classList.contains('model-hub-guard-action'));
    expect(done).toBeTruthy();
    await userEvent.click(done!);
    expect(await screen.findByRole('heading', { name: updated.display_name })).toBeTruthy();
  });

  it('echoes a non-empty server plan exactly when deleting a source', async () => {
    const hops = [{ backend: 'claude' as const, menu_model: 'claude-opus-4-6', position: 2, source_id: source.id, model_id: 'model-a' }];
    const gaps = [{ backend: 'claude' as const, model_id: 'claude-opus-4-6', agents: ['Release bot'] }];
    const requests: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/csrf-token') return Response.json({ csrf_token: 'csrf' });
      if (url.startsWith(`/api/models/sources/${source.id}`)) {
        requests.push({ url, init });
        if (requests.length === 1) {
          return Response.json({ error: 'source_in_route_chain', would_remove_hops: hops, would_interrupt: gaps }, { status: 409 });
        }
        return Response.json({ removed_hops: hops, interrupted: gaps });
      }
      throw new Error(`unexpected request: ${url}`);
    }));
    renderEchoPanel();

    await userEvent.click(screen.getByRole('button', { name: /Manage Production key|管理 Production key/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Remove source$|^移除来源$/i }));
    await userEvent.click(screen.getByRole('button', { name: /^Remove source$|^移除来源$/i }));
    expect(await screen.findAllByText(/claude-opus-4-6/)).toHaveLength(2);
    await userEvent.click(screen.getByRole('button', { name: /^Remove source$|^移除来源$/i }));

    await waitFor(() => expect(requests).toHaveLength(2));
    expect(requests[0].url).not.toContain('force=true');
    expect(requests[0].init?.body).toBeUndefined();
    expect(requests[1].url).toContain('?force=true');
    expect(JSON.parse(String(requests[1].init?.body))).toEqual({
      would_remove_hops: hops,
      would_interrupt: gaps,
    });
    expect(screen.queryByTestId('source-gone')).toBeNull();
    const impactDialog = await screen.findByRole('dialog', { name: /source was removed|来源已移除/i });
    const done = within(impactDialog)
      .getAllByRole('button', { name: /^Done$|^完成$/i })
      .find((button) => button.classList.contains('model-hub-guard-action'));
    expect(done).toBeTruthy();
    await userEvent.click(done!);
    expect(await screen.findByTestId('source-gone')).toBeTruthy();
  });

  it('sends no force when deleting a source with an empty server plan', async () => {
    const requests: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/csrf-token') return Response.json({ csrf_token: 'csrf' });
      if (url === `/api/models/sources/${source.id}`) {
        requests.push({ url, init });
        return Response.json({ removed_hops: [], interrupted: [] });
      }
      throw new Error(`unexpected request: ${url}`);
    }));
    renderEchoPanel();

    await userEvent.click(screen.getByRole('button', { name: /Manage Production key|管理 Production key/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Remove source$|^移除来源$/i }));
    await userEvent.click(screen.getByRole('button', { name: /^Remove source$|^移除来源$/i }));

    expect(await screen.findByTestId('source-gone')).toBeTruthy();
    expect(requests).toHaveLength(1);
    expect(requests[0].url).toBe(`/api/models/sources/${source.id}`);
    expect(requests[0].init?.body).toBeUndefined();
  });

  it('keeps a refused deletion visible and offers a retry', async () => {
    let requests = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/csrf-token') return Response.json({ csrf_token: 'csrf' });
      if (url === `/api/models/sources/${source.id}`) {
        requests += 1;
        return Response.json({ error: 'engine_down' }, { status: 503 });
      }
      throw new Error(`unexpected request: ${url}`);
    }));
    renderEchoPanel();

    await userEvent.click(screen.getByRole('button', { name: /Manage Production key|管理 Production key/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Remove source$|^移除来源$/i }));
    await userEvent.click(screen.getByRole('button', { name: /^Remove source$|^移除来源$/i }));

    expect(await screen.findByRole('heading', { name: source.display_name })).toBeTruthy();
    const retry = await screen.findByRole('button', { name: /^Try again$|^重试$/i });
    await userEvent.click(retry);
    await waitFor(() => expect(requests).toBe(2));
    expect(await screen.findByRole('button', { name: /^Try again$|^重试$/i })).toBeTruthy();
  });

  it('returns an edit guard cancellation to the held draft', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/csrf-token') return Response.json({ csrf_token: 'csrf' });
      if (url === `/api/models/sources/${source.id}`) {
        return Response.json({
          error: 'source_last_supplier',
          would_remove_hops: [{ backend: 'claude', menu_model: 'claude-opus-4-6', position: 1, source_id: source.id, model_id: 'model-a' }],
          would_interrupt: [],
        }, { status: 409 });
      }
      throw new Error(`unexpected request: ${url}`);
    }));
    renderEchoPanel();

    await userEvent.click(screen.getByRole('button', { name: /Manage Production key|管理 Production key/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Edit source$|^编辑来源$/i }));
    const name = screen.getByLabelText(/^Display name$|^显示名称$/i);
    await userEvent.clear(name);
    await userEvent.type(name, 'Held draft');
    await userEvent.click(screen.getByRole('button', { name: /^Save$|^保存$/i }));
    const guardDialog = await screen.findByRole('dialog', { name: /save changes|保存.*更改/i });
    const cancel = within(guardDialog)
      .getAllByRole('button', { name: /^Cancel$|^取消$/i })
      .find((button) => button.classList.contains('model-hub-guard-action'));
    expect(cancel).toBeTruthy();
    await userEvent.click(cancel!);

    expect(await screen.findByDisplayValue('Held draft')).toBeTruthy();
  });

  it('returns a failed forced edit to its held draft and visible error', async () => {
    let attempts = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/csrf-token') return Response.json({ csrf_token: 'csrf' });
      if (url === `/api/models/sources/${source.id}`) {
        attempts += 1;
        if (attempts === 1) {
          return Response.json({
            error: 'source_last_supplier',
            would_remove_hops: [{ backend: 'claude', menu_model: 'claude-opus-4-6', position: 1, source_id: source.id, model_id: 'model-a' }],
            would_interrupt: [],
          }, { status: 409 });
        }
        return Response.json({ error: 'engine_down' }, { status: 503 });
      }
      throw new Error(`unexpected request: ${url}`);
    }));
    renderEchoPanel();

    await userEvent.click(screen.getByRole('button', { name: /Manage Production key|管理 Production key/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Edit source$|^编辑来源$/i }));
    const name = screen.getByLabelText(/^Display name$|^显示名称$/i);
    await userEvent.clear(name);
    await userEvent.type(name, 'Held failure');
    await userEvent.click(screen.getByRole('button', { name: /^Save$|^保存$/i }));
    await userEvent.click(await screen.findByRole('button', { name: /^Save anyway$|^仍要保存$/i }));

    expect(await screen.findByDisplayValue('Held failure')).toBeTruthy();
    expect(screen.getByText(/source was not saved|来源没有保存上/i)).toBeTruthy();
  });

  it('retries a failed forced delete with the exact held server plan', async () => {
    const hops = [{ backend: 'claude' as const, menu_model: 'claude-opus-4-6', position: 1, source_id: source.id, model_id: 'model-a' }];
    const gaps = [{ backend: 'claude' as const, model_id: 'claude-opus-4-6', agents: ['Release bot'] }];
    const requests: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/csrf-token') return Response.json({ csrf_token: 'csrf' });
      if (url.startsWith(`/api/models/sources/${source.id}`)) {
        requests.push({ url, init });
        if (requests.length === 1) return Response.json({ error: 'source_in_route_chain', would_remove_hops: hops, would_interrupt: gaps }, { status: 409 });
        if (requests.length === 2) return Response.json({ error: 'engine_down' }, { status: 503 });
        return Response.json({ removed_hops: [], interrupted: [] });
      }
      throw new Error(`unexpected request: ${url}`);
    }));
    renderEchoPanel();

    await userEvent.click(screen.getByRole('button', { name: /Manage Production key|管理 Production key/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Remove source$|^移除来源$/i }));
    await userEvent.click(screen.getByRole('button', { name: /^Remove source$|^移除来源$/i }));
    await userEvent.click(await screen.findByRole('button', { name: /^Remove source$|^移除来源$/i }));
    await userEvent.click(await screen.findByRole('button', { name: /^Try again$|^重试$/i }));

    await waitFor(() => expect(requests).toHaveLength(3));
    for (const request of requests.slice(1)) {
      expect(request.url).toContain('?force=true');
      expect(JSON.parse(String(request.init?.body))).toEqual({
        would_remove_hops: hops,
        would_interrupt: gaps,
      });
    }
    expect(await screen.findByTestId('source-gone')).toBeTruthy();
  });

  it.each(UNKNOWN_WRITE_CASES)(
    'reconciles an unknown $action write (forced=$forced, outcome=$outcome) from one authoritative read',
    async ({ action, forced, outcome }) => {
      const unknownFailure = action === 'edit'
        ? new ApiCallError('http_502', undefined, false, heldGaps, [], heldHops, 502)
        : new TypeError('response lost');
      if (action === 'edit') {
        const mutation = vi.spyOn(modelsApi, 'patchSource');
        if (forced) mutation.mockRejectedValueOnce(guardRefusal());
        mutation.mockRejectedValueOnce(unknownFailure);
      } else {
        const mutation = vi.spyOn(modelsApi, 'deleteSource');
        if (forced) mutation.mockRejectedValueOnce(guardRefusal());
        mutation.mockRejectedValueOnce(unknownFailure);
      }

      const updated = { ...source, display_name: 'Unknown edit' };
      const inventory = vi.spyOn(modelsApi, 'listSources');
      if (outcome === 'unread') inventory.mockRejectedValueOnce(new Error('inventory unavailable'));
      else if (outcome === 'committed') inventory.mockResolvedValueOnce(action === 'edit' ? [updated] : []);
      else inventory.mockResolvedValueOnce([source]);

      renderEchoPanel();
      await submitManagementWrite(action, forced);
      await waitFor(() => expect(inventory).toHaveBeenCalledOnce());

      if (outcome === 'committed' && forced) {
        const impact = await screen.findByRole('dialog', {
          name: action === 'edit' ? /source was updated|来源已更新/i : /source was removed|来源已移除/i,
        });
        expect(impact.textContent).toContain('claude-opus-4-6');
        const done = within(impact)
          .getAllByRole('button', { name: /^Done$|^完成$/i })
          .find((button) => button.classList.contains('model-hub-guard-action'));
        expect(done).toBeTruthy();
        await userEvent.click(done!);
      }

      if (outcome === 'committed') {
        if (action === 'edit') expect(await screen.findByRole('heading', { name: 'Unknown edit' })).toBeTruthy();
        else expect(await screen.findByTestId('source-gone')).toBeTruthy();
        return;
      }

      const failure = await waitFor(() => {
        const node = document.querySelector<HTMLElement>(`[data-manage-failure="${action}"]`);
        expect(node).toBeTruthy();
        return node!;
      });
      expect(failure.dataset.manageRetryRead).toBe(String(outcome === 'unread'));
      if (outcome === 'unread') expect(failure.textContent).toMatch(/Could not verify|无法确认/i);
      else expect(failure.textContent).toMatch(/was not saved|was not removed|没有保存上|没有移除/i);
    },
  );

  it('rereads instead of issuing another PATCH while an unknown edit remains unresolved', async () => {
    const patch = vi.spyOn(modelsApi, 'patchSource').mockRejectedValueOnce(new TypeError('response lost'));
    const inventory = vi.spyOn(modelsApi, 'listSources')
      .mockRejectedValueOnce(new Error('inventory unavailable'))
      .mockResolvedValueOnce([source]);
    renderEchoPanel();

    await submitManagementWrite('edit', false);
    await waitFor(() => expect(inventory).toHaveBeenCalledOnce());
    const name = screen.getByLabelText(/^Display name$|^显示名称$/i);
    await userEvent.type(name, ' still fenced');
    await userEvent.keyboard('{Escape}');
    expect(screen.getByDisplayValue('Unknown edit still fenced')).toBeTruthy();

    await userEvent.click(screen.getByRole('button', { name: /^Try again$|^重试$/i }));
    await waitFor(() => expect(inventory).toHaveBeenCalledTimes(2));
    expect(patch).toHaveBeenCalledOnce();
  });

  it.each(['edit', 'delete'] as const)(
    'keeps the exact committed $action impact mounted until settlement lands',
    async (action) => {
      const reconcile = vi.fn()
        .mockResolvedValueOnce('degraded')
        .mockResolvedValueOnce('landed');
      const hops = [{ backend: 'claude' as const, menu_model: 'claude-opus-4-6', position: 1, source_id: source.id, model_id: 'model-a' }];
      const gaps = [{ backend: 'claude' as const, model_id: 'claude-opus-4-6', agents: ['Release bot'] }];
      if (action === 'edit') {
        vi.spyOn(modelsApi, 'patchSource').mockResolvedValueOnce({
          source: { ...source, display_name: 'Unknown edit' },
          removed_hops: hops,
          interrupted: gaps,
        });
      } else {
        vi.spyOn(modelsApi, 'deleteSource').mockResolvedValueOnce({
          removed_hops: hops,
          interrupted: gaps,
        });
      }
      const trackMutation: TrackSourceMutation = (work) => work(source, settlement({
        source: reconcile,
        gone: reconcile,
      }));
      render(
        <ToastProvider>
          <I18nextProvider i18n={i18n}>
            <SourceDetailPanel source={source} trackMutation={trackMutation} onReauth={noReauth} />
          </I18nextProvider>
        </ToastProvider>,
      );

      await submitManagementWrite(action, false);
      const impact = await screen.findByRole('dialog', {
        name: action === 'edit' ? /source was updated|来源已更新/i : /source was removed|来源已移除/i,
      });
      const committedEvidence = () => Array.from(impact.querySelectorAll('.model-hub-guard-hop'))
        .map((node) => node.textContent);
      const evidenceBeforeRetry = committedEvidence();
      expect(impact.textContent).toContain(hops[0].menu_model);
      expect(impact.textContent).toContain(gaps[0].agents[0]);

      const done = within(impact).getAllByRole('button', { name: /^Done$|^完成$/i })
        .find((button) => button.classList.contains('model-hub-guard-action'));
      expect(done).toBeTruthy();
      await userEvent.click(done!);
      expect(await within(impact).findByText(/could not be refreshed|暂时无法刷新/i)).toBeTruthy();
      expect(committedEvidence()).toEqual(evidenceBeforeRetry);
      expect(within(impact).getAllByRole('button', { name: /Dismiss unverified result|放弃未验证结果/i })
        .some((button) => button.classList.contains('model-hub-guard-action'))).toBe(true);

      await userEvent.click(within(impact).getByRole('button', { name: /^Try again$|^重试$/i }));
      await waitFor(() => expect(screen.queryByRole('dialog', {
        name: action === 'edit' ? /source was updated|来源已更新/i : /source was removed|来源已移除/i,
      })).toBeNull());
      expect(reconcile).toHaveBeenCalledTimes(2);
    },
  );

  it('enables labeled impact dismissal only after reconciliation degrades', async () => {
    const reconcile = vi.fn().mockResolvedValueOnce('degraded');
    const hops = [{ backend: 'claude' as const, menu_model: 'claude-opus-4-6', position: 1, source_id: source.id, model_id: 'model-a' }];
    vi.spyOn(modelsApi, 'patchSource').mockResolvedValueOnce({
      source: { ...source, display_name: 'Dismissible impact' },
      removed_hops: hops,
      interrupted: [],
    });
    const trackMutation: TrackSourceMutation = (work) => work(source, settlement({ source: reconcile }));
    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SourceDetailPanel source={source} trackMutation={trackMutation} onReauth={noReauth} />
        </I18nextProvider>
      </ToastProvider>,
    );

    await submitManagementWrite('edit', false);
    const impact = await screen.findByRole('dialog', { name: /source was updated|来源已更新/i });
    const closeBeforeLanding = within(impact).getAllByRole('button', { name: /^Done$|^完成$/i })
      .find((button) => button.classList.contains('model-hub-guard-close'));
    expect(closeBeforeLanding).toBeTruthy();
    expect((closeBeforeLanding as HTMLButtonElement).disabled).toBe(true);
    const done = within(impact).getAllByRole('button', { name: /^Done$|^完成$/i })
      .find((button) => button.classList.contains('model-hub-guard-action'));
    await userEvent.click(done!);

    const dismissButtons = await within(impact).findAllByRole('button', { name: /Dismiss unverified result|放弃未验证结果/i });
    expect(dismissButtons.some((button) => button.classList.contains('model-hub-guard-action'))).toBe(true);
    const dismissClose = dismissButtons.find((button) => button.classList.contains('model-hub-guard-close'));
    expect(dismissClose).toBeTruthy();
    expect((dismissClose as HTMLButtonElement).disabled).toBe(false);
    await userEvent.click(dismissClose!);
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /source was updated|来源已更新/i })).toBeNull());
  });

  it.each(['edit', 'delete'] as const)(
    'does not reread after a server-named $action failure',
    async (action) => {
      const namedFailure = new ApiCallError('engine_down', undefined, true);
      if (action === 'edit') vi.spyOn(modelsApi, 'patchSource').mockRejectedValueOnce(namedFailure);
      else vi.spyOn(modelsApi, 'deleteSource').mockRejectedValueOnce(namedFailure);
      const inventory = vi.spyOn(modelsApi, 'listSources');

      renderEchoPanel();
      await submitManagementWrite(action, false);

      const failure = await waitFor(() => {
        const node = document.querySelector<HTMLElement>(`[data-manage-failure="${action}"]`);
        expect(node).toBeTruthy();
        return node!;
      });
      expect(failure.dataset.manageRetryRead).toBe('false');
      expect(inventory).not.toHaveBeenCalled();
    },
  );

  it('omits native refetch because that channel has no stored discovery credential', () => {
    render(<I18nextProvider i18n={i18n}><SourceDetailPanel source={{ ...source, kind: 'subscription', supply_channel: 'native_cli' }} trackMutation={immediateTrack} onReauth={noReauth} /></I18nextProvider>);
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

  it('settles a failed refetch from the authoritative Source inventory', async () => {
    const reconciled = { ...source, state: { status: 'error' as const, retry_at: null, detail_key: 'models.source.error.unclassified' as const } };
    vi.spyOn(modelsApi, 'refreshSource').mockRejectedValueOnce(new Error('lost response'));
    vi.spyOn(modelsApi, 'listSources').mockResolvedValueOnce([reconciled]);
    const applySource = vi.fn().mockResolvedValue(undefined);
    const trackMutation: TrackSourceMutation = (work) => work(source, settlement({ source: applySource }));
    render(<I18nextProvider i18n={i18n}><SourceDetailPanel source={source} trackMutation={trackMutation} onReauth={noReauth} /></I18nextProvider>);

    await userEvent.click(screen.getByRole('button', { name: /^Refetch$|^重新拉取$/i }));

    await waitFor(() => expect(applySource).toHaveBeenCalledWith(reconciled));
  });

  it('applies the refetch Source echo before the collection refresh settles', async () => {
    const echoed = {
      ...source,
      models: [...source.models, { id: 'model-b', display_name: null, origin: 'discovered' as const, reasoning_efforts: [] }],
    };
    const refresh = vi.spyOn(modelsApi, 'refreshSource').mockResolvedValueOnce({ source: echoed, discovered: echoed.models.length });
    const reconcile = vi.fn().mockResolvedValue(undefined);
    renderEchoPanel(reconcile);

    await userEvent.click(screen.getByRole('button', { name: /^Refetch$|^重新拉取$/i }));

    expect(await screen.findByText('model-b')).toBeTruthy();
    expect(refresh).toHaveBeenCalledOnce();
    expect(reconcile).toHaveBeenCalledOnce();
  });

  it('serializes a tier write and a refetch for the same Source', async () => {
    const tierResponse = deferred<Source>();
    const update = vi.spyOn(modelsApi, 'updateModelReasoningEfforts').mockReturnValueOnce(tierResponse.promise);
    const refreshed = {
      ...source,
      models: [...source.models, { id: 'model-b', display_name: null, origin: 'discovered' as const, reasoning_efforts: [] }],
    };
    const refetch = vi.spyOn(modelsApi, 'refreshSource').mockResolvedValueOnce({ source: refreshed, discovered: refreshed.models.length });
    renderEchoPanel(vi.fn(), serializedTrack());

    await userEvent.click(screen.getByRole('button', { name: /high/i }));
    await userEvent.type(screen.getByPlaceholderText(/Enter to add|回车添加/i), 'low{Enter}');
    await waitFor(() => expect(update).toHaveBeenCalledOnce());
    await userEvent.click(screen.getByRole('button', { name: /^Refetch$|^重新拉取$/i }));
    expect(refetch).not.toHaveBeenCalled();

    tierResponse.resolve({
      ...source,
      models: [{ ...source.models[0], reasoning_efforts: ['high', 'low'] }],
    });
    await waitFor(() => expect(refetch).toHaveBeenCalledOnce());
    expect(await screen.findByText('model-b')).toBeTruthy();
  });

  it('orders every full-Source mutation family through the same Source queue', async () => {
    const writes = createPendingWrites(() => {});
    const started: string[] = [];
    const families = ['tier', 'refetch', 'add', 'remove', 'edit', 'delete'];
    const gates = families.map(() => deferred<void>());
    const runs = families.map((name, index) => writes.track(source.id, async () => {
      started.push(name);
      await gates[index].promise;
    }));

    await waitFor(() => expect(started).toEqual(['tier']));
    for (let index = 0; index < gates.length; index += 1) {
      gates[index].resolve();
      await waitFor(() => expect(started).toEqual(families.slice(0, index + 2)));
    }
    await Promise.all(runs);
  });

  it('routes every full-Source mutation family through the shared per-Source queue', () => {
    const detail = readFileSync(join(process.cwd(), 'src/components/settings/models/SourceDetailPanel.tsx'), 'utf8');
    expect(detail).toMatch(/const refetch = \(confirmation\?: GuardConfirmation\)[\s\S]*?return trackMutation\(async \(latest, settlement\)/);
    expect(detail).toMatch(/const addManualModel = \(\)[\s\S]*?return trackMutation\(async \(latest, settlement\)/);
    expect(detail).toMatch(/const remove = \(model: SuppliedModel, confirmation\?: GuardConfirmation\)[\s\S]*?return trackMutation\(async \(latest, settlement\)/);
    expect(detail).toMatch(/const submitEdit = \(draft: SourceEditDraft, patch: SourcePatch, plan: ManageGuardPlan \| null\)[\s\S]*?return trackMutation\(async \(latest, settlement\)/);
    expect(detail).toMatch(/const submitDelete = \(plan: ManageGuardPlan \| null\)[\s\S]*?return trackMutation\(async \(latest, settlement\)/);
    expect(detail).toMatch(/const commit = async[\s\S]*?setSaving\(true\)[\s\S]*?trackMutation\(async \(latest, settlement\)[\s\S]*?tierMutationPayload\(latest/);
  });

  it('routes every JSX management gesture through the stage authority', () => {
    const detail = readFileSync(join(process.cwd(), 'src/components/settings/models/SourceDetailPanel.tsx'), 'utf8');
    const jsx = detail.slice(detail.lastIndexOf('\n  return ('));
    expect(detail).toContain('React.useReducer(transitionManageStage');
    expect(jsx).not.toMatch(/setManageStage|dispatchManageStage\(\{\s*type:\s*['"](?:begin|submit|guard|fail|commit|settled)/);
    expect(jsx).toMatch(/editManageDraft/);
    expect(jsx).toMatch(/cancelManage/);
    expect(jsx).toMatch(/dismissUnresolvedManage/);
    expect(jsx).toMatch(/completeManageImpact/);
  });

  it('keeps Source entities behind one generation authority without an adoption side cache', () => {
    const page = readFileSync(join(process.cwd(), 'src/components/settings/models/SettingsModelsPage.tsx'), 'utf8');
    const sourceRow = readFileSync(join(process.cwd(), 'src/components/settings/models/SourceRow.tsx'), 'utf8');
    expect(page).toMatch(/createLatestEntityAuthorityByKey/);
    expect(page).toMatch(/sourceEntityAuthority\.beginSnapshot\(\)/);
    expect(page).toMatch(/sourceEntityAuthority\.settleSnapshot/);
    expect(page).toMatch(/trackSourceMutation/);
    expect(page).toMatch(/const settlement: SourceMutationSettlement/);
    expect(page).not.toMatch(/activeSourceGenerations/);
    expect(`${page}\n${sourceRow}`).not.toMatch(/adoptionBySource/);
    expect(sourceRow).toMatch(/source\.adopted_by/);
  });

  it('applies the manual-create Source echo without waiting for a collection read', async () => {
    const echoed = {
      ...source,
      models: [...source.models, { id: 'model-b', display_name: null, origin: 'manual' as const, reasoning_efforts: [] }],
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

  it('keeps an existing tier removal clickable while the editor input has focus', async () => {
    const update = vi.spyOn(modelsApi, 'updateModelReasoningEfforts').mockResolvedValueOnce({
      ...source,
      models: [{ ...source.models[0], reasoning_efforts: [] }],
    });
    renderPanel();
    await userEvent.click(screen.getByRole('button', { name: /high/i }));
    await userEvent.type(screen.getByPlaceholderText(/Enter to add|回车添加/i), 'draft');
    await userEvent.click(screen.getByRole('button', { name: /Remove high|移除 high/i }));

    await waitFor(() => expect(update).toHaveBeenCalledWith(source.id, 'model-a', []));
  });

  it('sends a manual-model removal before showing any guarded-change confirm', async () => {
    const remove = vi.spyOn(modelsApi, 'deleteCustomModel').mockResolvedValueOnce(source);
    renderPanel();
    await userEvent.click(screen.getByRole('button', { name: /Remove model-a|移除 model-a/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Remove$|^移除$/i }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(source.id, 'model-a', undefined));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('echoes both arrays from a refetch refusal in the forced retry', async () => {
    const hops = [{ backend: 'claude' as const, menu_model: 'claude-opus-4-6', position: 2, source_id: source.id, model_id: 'model-a' }];
    const gaps = [{ backend: 'claude' as const, model_id: 'claude-opus-4-6', agents: ['Release bot'] }];
    const bodies: unknown[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/csrf-token') return Response.json({ csrf_token: 'csrf' });
      if (String(input).endsWith(`/api/models/sources/${source.id}/refresh`)) {
        bodies.push(JSON.parse(String(init?.body)));
        if (bodies.length === 1) {
          return Response.json({ error: 'source_in_route_chain', would_remove_hops: hops, would_interrupt: gaps }, { status: 409 });
        }
        return Response.json({ source, discovered: source.models.length });
      }
      throw new Error(`unexpected request: ${String(input)}`);
    }));
    renderPanel();

    await userEvent.click(screen.getByRole('button', { name: /^Refetch$|^重新拉取$/i }));
    await userEvent.click(await screen.findByRole('button', { name: /^Refetch anyway$|^仍要拉取$/i }));

    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1]).toEqual({ force: true, would_remove_hops: hops, would_interrupt: gaps });
  });

  it('requires confirmation again when a forced removal receives a new guard plan', async () => {
    const firstHops = [{ backend: 'claude' as const, menu_model: 'menu-a', position: 1, source_id: source.id, model_id: 'model-a' }];
    const nextHops = [{ backend: 'codex' as const, menu_model: 'menu-b', position: 3, source_id: source.id, model_id: 'model-a' }];
    const bodies: unknown[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/csrf-token') return Response.json({ csrf_token: 'csrf' });
      if (String(input).includes(`/api/models/sources/${source.id}/models/model-a`)) {
        bodies.push(JSON.parse(String(init?.body)));
        if (bodies.length === 1) return Response.json({ error: 'source_model_in_route_chain', would_remove_hops: firstHops, would_interrupt: [] }, { status: 409 });
        if (bodies.length === 2) return Response.json({ error: 'source_model_in_route_chain', would_remove_hops: nextHops, would_interrupt: [] }, { status: 409 });
        return Response.json({ source: { ...source, models: [] } });
      }
      throw new Error(`unexpected request: ${String(input)}`);
    }));
    renderPanel();

    await userEvent.click(screen.getByRole('button', { name: /Remove model-a|移除 model-a/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Remove$|^移除$/i }));
    await userEvent.click(await screen.findByRole('button', { name: /^Remove anyway$|^仍要移除$/i }));

    expect(await screen.findByText(/menu-b/)).toBeTruthy();
    expect(bodies[1]).toEqual({ force: true, would_remove_hops: firstHops, would_interrupt: [] });
    await userEvent.click(screen.getByRole('button', { name: /^Remove anyway$|^仍要移除$/i }));
    await waitFor(() => expect(bodies).toHaveLength(3));
    expect(bodies[2]).toEqual({ force: true, would_remove_hops: nextHops, would_interrupt: [] });
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
    const trackMutation: TrackSourceMutation = (work) => work(source, settlement({ source: onMutation }));
    render(<I18nextProvider i18n={i18n}><SourceDetailPanel source={source} trackMutation={trackMutation} onReauth={noReauth} /></I18nextProvider>);

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

  it('settles an unknown deletion as Source gone when the authoritative inventory omits it', async () => {
    const remove = vi.spyOn(modelsApi, 'deleteCustomModel').mockRejectedValueOnce(new TypeError('response lost'));
    vi.spyOn(modelsApi, 'listSources').mockResolvedValueOnce([]);
    const onGone = vi.fn().mockResolvedValue(undefined);
    const trackMutation: TrackSourceMutation = (work) => work(source, settlement({ gone: onGone }));
    render(<I18nextProvider i18n={i18n}><SourceDetailPanel source={source} trackMutation={trackMutation} onReauth={noReauth} /></I18nextProvider>);

    await userEvent.click(screen.getByRole('button', { name: /Remove model-a|移除 model-a/i }));
    await userEvent.click(screen.getByRole('menuitem', { name: /^Remove$|^移除$/i }));

    await waitFor(() => expect(onGone).toHaveBeenCalledWith(source.id, { kind: 'gone', sources: [], snapshot: 1 }));
    expect(remove).toHaveBeenCalledOnce();
    expect(screen.queryByRole('button', { name: /^Try again$|^重试$/i })).toBeNull();
  });

  // The property, not today's button list: every remedy the authority can return
  // reaches the control declared by its total destination Record. Seeding is
  // complete by construction, so a new status/cause/kind/channel is swept without
  // editing the test, while a new RepairKind fails the Record/type checks first.
  it('gives every repair kind a reachable declared destination', () => {
    const kinds = Object.keys({ subscription: 0, api_key: 0 } satisfies Record<SourceKind, 0>) as SourceKind[];
    const channels = Object.keys({ native_cli: 0, hub: 0 } satisfies Record<SupplyChannel, 0>) as SupplyChannel[];
    const causes: (SourceDetailKey | null)[] = [
      null,
      ...COOLDOWN_DETAIL_KEYS,
      ...NEEDS_ACTION_DETAIL_KEYS,
      ...ERROR_DETAIL_KEYS,
    ];
    const shapes = SOURCE_STATUSES.flatMap((status) => causes.flatMap((detail_key) => kinds.flatMap(
      (kind) => channels.map((supply_channel): Source => ({
        ...source,
        kind,
        supply_channel,
        state: { status, retry_at: null, detail_key },
      })),
    )));
    const reached = new Set<RepairKind>();

    for (const shape of shapes) {
      const view = render(
        <I18nextProvider i18n={i18n}>
          <SourceDetailPanel source={shape} trackMutation={immediateTrack} onReauth={vi.fn()} />
        </I18nextProvider>,
      );
      const action = repairAction(shape);
      const offered = view.container.querySelectorAll('[data-repair-kind]');
      expect(offered.length, JSON.stringify({ kind: shape.kind, channel: shape.supply_channel, ...shape.state }))
        .toBe(action ? 1 : 0);
      if (action) {
        reached.add(action);
        expect(offered[0].getAttribute('data-repair-kind')).toBe(action);
        expect(offered[0].getAttribute('data-repair-destination')).toBe(REPAIR_DESTINATION[action]);
      }
      view.unmount();
    }

    expect(reached).toEqual(new Set(Object.keys(REPAIR_LABEL_KEY) as RepairKind[]));
  });

  it('opens key replacement from the revoked hub credential repair tap', async () => {
    render(
      <I18nextProvider i18n={i18n}>
        <SourceDetailPanel
          source={{
            ...source,
            state: {
              status: 'needs_action',
              retry_at: null,
              detail_key: 'models.source.needs_action.credential_revoked',
            },
          }}
          trackMutation={immediateTrack}
          onReauth={vi.fn()}
        />
      </I18nextProvider>,
    );

    await userEvent.click(screen.getByRole('button', { name: /^Replace key$|^更换 Key$/i }));

    expect(await screen.findByRole('dialog', { name: /Replace the API key|更换.*API Key/i })).toBeTruthy();
    expect(screen.getByLabelText(/^New API key$|^新的 API Key$/i)).toBeTruthy();
  });

  it('confirms a native re-login with the cost it pays at start before handing the source up', async () => {
    const native = blockedSubscription('native_cli');
    const onReauth = vi.fn();
    render(<I18nextProvider i18n={i18n}><SourceDetailPanel source={native} trackMutation={immediateTrack} onReauth={onReauth} /></I18nextProvider>);

    await userEvent.click(screen.getByRole('button', { name: /^Sign in$|^重新登录$/i }));

    // The confirm has to come BEFORE the journey opens: the dialog POSTs the
    // re-auth as it mounts, and on native that call is the irreversible half.
    expect(await screen.findByText(/as soon as you start|旧的登录立即失效/i)).toBeTruthy();
    expect(onReauth).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: /^Start sign-in$|^开始登录$/i }));

    expect(onReauth).toHaveBeenCalledWith(native);
  });

  it('warns a hub re-login about the cost it can pay, not the one it cannot', async () => {
    render(<I18nextProvider i18n={i18n}><SourceDetailPanel source={blockedSubscription('hub')} trackMutation={immediateTrack} onReauth={vi.fn()} /></I18nextProvider>);

    await userEvent.click(screen.getByRole('button', { name: /^Sign in$|^重新登录$/i }));

    expect(await screen.findByText(/go through|没成功/i)).toBeTruthy();
    // A hub re-login writes nothing at start, so the native sentence would warn
    // about a loss that does not happen.
    expect(screen.queryByText(/as soon as you start|旧的登录立即失效/i)).toBeNull();
  });

});
