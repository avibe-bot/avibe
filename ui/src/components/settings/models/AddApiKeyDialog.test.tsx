// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { AddApiKeyDialog } from './AddApiKeyDialog';
import { API_KEY_VENDOR_PRESETS, apiKeyVendorPreset, CUSTOM_VENDOR } from './apiKeyVendors';
import { createSourceCollectionReadAuthority } from './collectionReadAuthority';
import { ApiCallError, modelsApi } from './modelsApi';
import type {
  SourceMutationLanding,
  SourceMutationLandingReads,
  SourceMutationSettlement,
  TrackSourceMutation,
} from './mutationSettlement';
import {
  type RouteHopRef,
  type Source,
  type SupplyGap,
} from './types';

const source: Source = {
  id: 'src_new',
  last_discovered_at: null,
  kind: 'api_key',
  vendor: 'custom',
  display_name: 'Relay',
  protocol: 'openai_chat',
  base_url: 'https://relay.example/v1',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'standby', retry_at: null, detail_key: null },
  models: [],
};

const blockedSource: Source = {
  ...source,
  id: 'src_revoked',
  display_name: 'Revoked key',
  credential_ref: 'cred_revoked',
  state: {
    status: 'needs_action',
    retry_at: null,
    detail_key: 'models.source.needs_action.credential_revoked',
  },
};

const successfulReplacementSource = (): Source => ({
  ...blockedSource,
  state: { status: 'standby', retry_at: null, detail_key: null },
});

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};

const renderDialog = (onClose = vi.fn(), onAdded = vi.fn()) => {
  render(
    <I18nextProvider i18n={i18n}>
      <AddApiKeyDialog open sourceReads={createSourceCollectionReadAuthority(modelsApi)} onClose={onClose} onAdded={onAdded} />
    </I18nextProvider>,
  );
  return { onClose, onAdded };
};

const replacementSettlement = (
  overrides: Partial<SourceMutationSettlement> = {},
): SourceMutationSettlement => ({
  source: vi.fn().mockResolvedValue({
    verdict: 'landed',
    reads: {} as SourceMutationLandingReads,
    affectedChains: [],
  } satisfies SourceMutationLanding),
  gone: vi.fn().mockResolvedValue({
    verdict: 'landed',
    reads: {} as SourceMutationLandingReads,
    affectedChains: [],
  } satisfies SourceMutationLanding),
  unread: vi.fn().mockResolvedValue({
    verdict: 'landed',
    reads: {} as SourceMutationLandingReads,
    affectedChains: [],
  } satisfies SourceMutationLanding),
  release: vi.fn(),
  readInventory: vi.fn().mockResolvedValue({ snapshot: 1, sources: [blockedSource] }),
  ...overrides,
});

const neverResolvingSettlement = (
  overrides: Partial<SourceMutationSettlement> = {},
): SourceMutationSettlement => ({
  ...replacementSettlement(),
  source: vi.fn().mockReturnValue(new Promise<never>(() => undefined)),
  gone: vi.fn().mockReturnValue(new Promise<never>(() => undefined)),
  unread: vi.fn().mockReturnValue(new Promise<never>(() => undefined)),
  ...overrides,
});

const renderReplacement = (
  current: Source = blockedSource,
  settlement: SourceMutationSettlement = replacementSettlement(),
  onClose = vi.fn(),
) => {
  const trackMutation: TrackSourceMutation = (work) => work(current, settlement);
  render(
    <I18nextProvider i18n={i18n}>
      <AddApiKeyDialog
        mode="replace"
        open
        source={current}
        trackMutation={trackMutation}
        onClose={onClose}
      />
    </I18nextProvider>,
  );
  return { onClose, settlement };
};

const CONFIRM = /Add provider|添加供应商/;

const fillCredentials = async () => {
  const user = userEvent.setup();
  await user.type(screen.getByRole('textbox', { name: /^Base URL$/i }), 'https://relay.example/v1');
  await user.type(screen.getByLabelText(/^API key$/i), 'secret-key');
  return user;
};

/** Named by its label AND its own contents, so the name starts with 服务商 and
 *  continues with whichever vendor is currently chosen. */
const vendorField = () => screen.getByRole('combobox', { name: /^(Vendor|服务商)(\s|$)/ });
const baseUrlInput = () => screen.getByRole('textbox', { name: /^Base URL$/i }) as HTMLInputElement;

/** A catalog row read from the shipped file, so the case does not restate an id,
 *  a URL, or a pin that the backend owns. Chosen by protocol rather than by name:
 *  the assertions are about a pin being carried, not about which vendor it is. */
const preset = (protocol: string) => {
  const entry = API_KEY_VENDOR_PRESETS.find((row) => row.protocol === protocol);
  if (!entry) throw new Error(`the shipped catalog has no ${protocol} row to exercise the pin with`);
  return entry;
};

/** What a row of the picker reads as — the only handle a user, or a spec, has on it. */
const vendorOptionName = (id: string) => (id === CUSTOM_VENDOR
  ? i18n.t('settings.models.addKey.field.vendor.custom')
  : apiKeyVendorPreset(id)?.label ?? id);

/** The order the menu should read in: the shipped catalog verbatim, then the
 *  entry that is not a vendor. The property is menu order == file order, so the
 *  ranking itself is asserted nowhere — it is a product decision that belongs in
 *  `vibe/data/api_key_vendors.json` alone, and a list restated here would make
 *  every reordering a two-file edit while proving nothing the file cannot say.
 *  What this does catch is the menu re-deriving an order of its own. */
const offeredInOrder = (): string[] => [
  ...API_KEY_VENDOR_PRESETS.map((row) => row.id),
  CUSTOM_VENDOR,
];

const selectVendor = async (user: ReturnType<typeof userEvent.setup>, id: string) => {
  await user.click(vendorField());
  await user.click(await screen.findByRole('option', { name: vendorOptionName(id) }));
};

const clickConfirm = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(await screen.findByRole('button', { name: CONFIRM }));
};

beforeEach(async () => {
  await i18n.changeLanguage('en');
  // The 服务商 picker anchors a popover over its trigger and scrolls the
  // highlighted row into view; jsdom implements neither measurement.
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('AddApiKeyDialog · save reconciliation', () => {
  it('saves directly with a visible protocol and never performs automatic detection', async () => {
    const observe = vi.spyOn(modelsApi, 'observeApiKeySource');
    const create = vi.spyOn(modelsApi, 'createApiKeySource').mockResolvedValue({
      source, added_to: [], adopted_by: [],
    });
    const { onAdded } = renderDialog();
    const user = await fillCredentials();
    expect(screen.getByRole('button', { name: 'OpenAI Chat Completions' }).getAttribute('aria-pressed')).toBe('true');
    await clickConfirm(user);
    await waitFor(() => expect(onAdded).toHaveBeenCalledOnce());
    expect(create).toHaveBeenCalledWith(expect.objectContaining({
      protocol: 'openai_chat', save_unverified: true, key: 'secret-key',
    }));
    expect(observe).not.toHaveBeenCalled();
  });

  it.each(API_KEY_VENDOR_PRESETS)('saves $id with its catalog protocol without a connectivity gate', async (entry) => {
    const observe = vi.spyOn(modelsApi, 'observeApiKeySource');
    const create = vi.spyOn(modelsApi, 'createApiKeySource').mockResolvedValue({
      source, added_to: [], adopted_by: [],
    });
    renderDialog();
    const user = userEvent.setup();
    await selectVendor(user, entry.id);
    await user.type(screen.getByLabelText(/^API key$/i), 'secret-key');
    await clickConfirm(user);
    await waitFor(() => expect(create).toHaveBeenCalledOnce());
    expect(create.mock.calls[0][0]).toMatchObject({
      vendor: entry.id, protocol: entry.protocol, base_url: entry.official_base_url, save_unverified: true,
    });
    expect(observe).not.toHaveBeenCalled();
  });

  it('lets the user choose a custom protocol and keeps validation failures editable', async () => {
    const create = vi.spyOn(modelsApi, 'createApiKeySource')
      .mockRejectedValueOnce(new ApiCallError('discovery_failed', undefined, true, [], [], [], 400))
      .mockResolvedValueOnce({ source, added_to: [], adopted_by: [] });
    renderDialog();
    const user = await fillCredentials();
    await user.click(screen.getByRole('button', { name: 'Anthropic Messages' }));
    await clickConfirm(user);
    await screen.findByRole('button', { name: /^Retry$/ });
    await user.type(baseUrlInput(), '/changed');
    await clickConfirm(user);
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    expect(create.mock.calls[1][0]).toMatchObject({
      protocol: 'anthropic', base_url: 'https://relay.example/v1/changed', save_unverified: true,
    });
  });

  it('does not submit missing credentials or an overlong display name', async () => {
    const create = vi.spyOn(modelsApi, 'createApiKeySource');
    renderDialog();
    expect(screen.getByRole('button', { name: CONFIRM }).hasAttribute('disabled')).toBe(true);
    const user = await fillCredentials();
    await user.type(screen.getByRole('textbox', { name: /Name|名称/i }), 'n'.repeat(65));
    expect(screen.getByRole('button', { name: CONFIRM }).hasAttribute('disabled')).toBe(true);
    expect(create).not.toHaveBeenCalled();
  });

  it('reconciles a lost source-create response by nonce without posting twice', async () => {
    let nonce: string | undefined;
    const create = vi.spyOn(modelsApi, 'createApiKeySource').mockImplementationOnce(async (draft) => {
      nonce = draft.client_nonce;
      throw new TypeError('response lost');
    });
    const list = vi.spyOn(modelsApi, 'listSources').mockImplementation(async () => [{
      ...source,
      client_nonce: nonce,
      adopted_by: [],
    }]);
    const { onAdded } = renderDialog();
    const user = await fillCredentials();

    await clickConfirm(user);
    await user.click(await screen.findByRole('button', { name: /^Retry$|^重试$/i }));

    await waitFor(() => expect(onAdded).toHaveBeenCalledOnce());
    expect(list).toHaveBeenCalledOnce();
    expect(create).toHaveBeenCalledOnce();
    expect(nonce).toMatch(/^scn_[a-z0-9]{16,64}$/);
  });

  it('retries source creation only after the nonce is confirmed absent', async () => {
    const create = vi.spyOn(modelsApi, 'createApiKeySource')
      .mockRejectedValueOnce(new TypeError('response lost'))
      .mockResolvedValueOnce({ source, added_to: [], adopted_by: [] });
    const list = vi.spyOn(modelsApi, 'listSources').mockResolvedValueOnce([]);
    renderDialog();
    const user = await fillCredentials();

    await clickConfirm(user);
    await user.click(await screen.findByRole('button', { name: /^Retry$|^重试$/i }));

    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    expect(list).toHaveBeenCalledOnce();
    expect(create.mock.calls[1][0].client_nonce).toBe(create.mock.calls[0][0].client_nonce);
  });

  it('cannot create or navigate after cancelling a pending lost-response reconciliation', async () => {
    const pendingInventory = deferred<Source[]>();
    const create = vi.spyOn(modelsApi, 'createApiKeySource').mockRejectedValueOnce(new TypeError('response lost'));
    vi.spyOn(modelsApi, 'listSources').mockReturnValueOnce(pendingInventory.promise);
    const { onAdded, onClose } = renderDialog();
    const user = await fillCredentials();

    await clickConfirm(user);
    await user.click(await screen.findByRole('button', { name: /^Retry$|^重试$/i }));
    await user.click(screen.getAllByRole('button', { name: /^Cancel$|^取消$/i }).at(-1)!);
    pendingInventory.resolve([]);
    await pendingInventory.promise;

    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(create).toHaveBeenCalledOnce();
    expect(onAdded).not.toHaveBeenCalled();
  });

  it('cannot replay a reconciled Source after the dialog attempt is cancelled', async () => {
    const pendingInventory = deferred<Source[]>();
    let nonce: string | undefined;
    vi.spyOn(modelsApi, 'createApiKeySource').mockImplementationOnce(async (draft) => {
      nonce = draft.client_nonce;
      throw new TypeError('response lost');
    });
    vi.spyOn(modelsApi, 'listSources').mockReturnValueOnce(pendingInventory.promise);
    const { onAdded } = renderDialog();
    const user = await fillCredentials();

    await clickConfirm(user);
    await user.click(await screen.findByRole('button', { name: /^Retry$|^重试$/i }));
    await user.click(screen.getAllByRole('button', { name: /^Cancel$|^取消$/i }).at(-1)!);
    pendingInventory.resolve([{ ...source, client_nonce: nonce }]);
    await pendingInventory.promise;

    expect(onAdded).not.toHaveBeenCalled();
  });

});

describe('AddApiKeyDialog · replacement', () => {
  it('reaches the credential PUT with only the trimmed key', async () => {
    const requests: Array<{ input: string; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === '/api/csrf-token') return Response.json({ csrf_token: 'csrf' });
      requests.push({ input: path, init });
      return Response.json({
        source: successfulReplacementSource(),
        removed_hops: [],
        interrupted: [],
      });
    }));
    const settled = replacementSettlement();
    renderReplacement(blockedSource, settled);
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/^New API key$|^新的 API Key$/i), '  sk-replacement  ');
    await user.click(screen.getByRole('button', { name: /^Replace$|^更换$/i }));

    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0].input).toBe(`/api/models/sources/${blockedSource.id}/credential`);
    expect(requests[0].init?.method).toBe('PUT');
    expect(JSON.parse(String(requests[0].init?.body))).toEqual({ key: 'sk-replacement' });
    expect(settled.source).toHaveBeenCalledWith(expect.objectContaining({ state: {
      status: 'standby',
      retry_at: null,
      detail_key: null,
    } }));
  });

  it('uses the existing guard shape before force and reports the committed impact', async () => {
    const hop: RouteHopRef = {
      backend: 'claude',
      menu_model: 'sonnet',
      position: 1,
      source_id: blockedSource.id,
      model_id: 'claude-sonnet-4-5',
    };
    const gap: SupplyGap = { backend: 'claude', model_id: 'claude-sonnet-4-5', agents: ['pm-claude'] };
    const replace = vi.spyOn(modelsApi, 'replaceCredential')
      .mockRejectedValueOnce(new ApiCallError(
        'source_model_in_route_chain',
        undefined,
        true,
        [gap],
        [],
        [hop],
      ))
      .mockResolvedValueOnce({
        source: successfulReplacementSource(),
        removed_hops: [hop],
        interrupted: [gap],
      });
    renderReplacement();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/^New API key$|^新的 API Key$/i), 'sk-force');
    await user.click(screen.getByRole('button', { name: /^Replace$|^更换$/i }));

    expect(await screen.findByRole('dialog', { name: /Replace key for|更换.*Key/i })).toBeTruthy();
    expect(screen.getByText(/pm-claude/)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: /^Replace anyway$|^仍要更换$/i }));

    await waitFor(() => expect(replace).toHaveBeenCalledTimes(2));
    expect(replace.mock.calls).toEqual([
      [blockedSource.id, { key: 'sk-force' }],
      [blockedSource.id, {
        key: 'sk-force',
        force: true,
        would_remove_hops: [hop],
        would_interrupt: [gap],
      }],
    ]);
    expect(await screen.findByText(/^Removed hops$|^已移除的路由项$/i)).toBeTruthy();
    expect(screen.getByText(/now have no usable source|现在没有可用供应商/i)).toBeTruthy();
  });

  it('requires confirmation again when the server recomputes a different replacement plan', async () => {
    const firstHop: RouteHopRef = {
      backend: 'claude',
      menu_model: 'sonnet',
      position: 1,
      source_id: blockedSource.id,
      model_id: 'claude-sonnet-4-5',
    };
    const nextHop: RouteHopRef = {
      backend: 'codex',
      menu_model: 'gpt-5',
      position: 2,
      source_id: blockedSource.id,
      model_id: 'gpt-5.4',
    };
    const replace = vi.spyOn(modelsApi, 'replaceCredential')
      .mockRejectedValueOnce(new ApiCallError(
        'source_model_in_route_chain',
        undefined,
        true,
        [],
        [],
        [firstHop],
      ))
      .mockRejectedValueOnce(new ApiCallError(
        'source_model_in_route_chain',
        undefined,
        true,
        [],
        [],
        [nextHop],
      ))
      .mockResolvedValueOnce({
        source: successfulReplacementSource(),
        removed_hops: [nextHop],
        interrupted: [],
      });
    renderReplacement();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/^New API key$|^新的 API Key$/i), 'sk-changing-plan');
    await user.click(screen.getByRole('button', { name: /^Replace$|^更换$/i }));
    await user.click(await screen.findByRole('button', { name: /^Replace anyway$|^仍要更换$/i }));

    expect(await screen.findByText(/^gpt-5\.4/)).toBeTruthy();
    expect(replace.mock.calls[1][1]).toEqual({
      key: 'sk-changing-plan',
      force: true,
      would_remove_hops: [firstHop],
      would_interrupt: [],
    });

    await user.click(screen.getByRole('button', { name: /^Replace anyway$|^仍要更换$/i }));

    await waitFor(() => expect(replace).toHaveBeenCalledTimes(3));
    expect(replace.mock.calls[2][1]).toEqual({
      key: 'sk-changing-plan',
      force: true,
      would_remove_hops: [nextHop],
      would_interrupt: [],
    });
  });

  it.each([
    { path: 'response', outcome: 'repaired' },
    { path: 'response', outcome: 'impact' },
    { path: 'inventory', outcome: 'repaired' },
    { path: 'inventory', outcome: 'impact' },
  ] as const)(
    'publishes a $outcome outcome from $path evidence without waiting for reconciliation',
    async ({ path, outcome }) => {
      const current = successfulReplacementSource();
      const hop: RouteHopRef = {
        backend: 'claude',
        menu_model: 'sonnet',
        position: 1,
        source_id: blockedSource.id,
        model_id: 'claude-sonnet-4-5',
      };
      const gap: SupplyGap = {
        backend: 'claude',
        model_id: 'claude-sonnet-4-5',
        agents: ['pm-claude'],
      };
      const replace = vi.spyOn(modelsApi, 'replaceCredential');
      if (outcome === 'impact') {
        replace.mockRejectedValueOnce(new ApiCallError(
          'source_model_in_route_chain',
          undefined,
          true,
          [gap],
          [],
          [hop],
        ));
      }
      if (path === 'response') {
        replace.mockResolvedValueOnce({
          source: current,
          removed_hops: outcome === 'impact' ? [hop] : [],
          interrupted: outcome === 'impact' ? [gap] : [],
        });
      } else {
        replace.mockRejectedValueOnce(new ApiCallError('bad_response', undefined, false));
      }
      const settled = neverResolvingSettlement({
        readInventory: vi.fn().mockResolvedValue({ snapshot: 2, sources: [current] }),
      });
      const closeTimer = vi.spyOn(window, 'setTimeout');
      renderReplacement(blockedSource, settled);
      const user = userEvent.setup();

      await user.type(screen.getByLabelText(/^New API key$|^新的 API Key$/i), 'sk-terminal');
      await user.click(screen.getByRole('button', { name: /^Replace$|^更换$/i }));
      if (outcome === 'impact') {
        await user.click(await screen.findByRole('button', { name: /^Replace anyway$|^仍要更换$/i }));
      }

      if (outcome === 'impact') {
        expect(await screen.findByText(/^Removed hops$|^已移除的路由项$/i)).toBeTruthy();
        expect(screen.getByText(/pm-claude/)).toBeTruthy();
        expect(replace.mock.calls.at(-1)?.[1]).toEqual({
          key: 'sk-terminal',
          force: true,
          would_remove_hops: [hop],
          would_interrupt: [gap],
        });
      } else {
        expect(await screen.findByText(i18n.t('settings.models.repair.repaired'))).toBeTruthy();
      }

      const close = screen.getByRole('button', { name: /^Close$|^关闭$/i }) as HTMLButtonElement;
      expect(close.disabled).toBe(false);
      expect(screen.queryByRole('button', { name: /^Retry$|^重试$/i })).toBeNull();
      expect(settled.source).toHaveBeenCalledWith(current);
      expect(settled.unread).not.toHaveBeenCalled();
      if (path === 'inventory') expect(settled.readInventory).toHaveBeenCalledOnce();
      else expect(settled.readInventory).not.toHaveBeenCalled();
      expect(closeTimer.mock.calls.filter(([, delay]) => delay === 1400)).toHaveLength(
        outcome === 'repaired' ? 1 : 0,
      );
    },
  );

  it('keeps Retry when an ambiguous replacement still observes the blocked source', async () => {
    vi.spyOn(modelsApi, 'replaceCredential').mockRejectedValueOnce(
      new ApiCallError('bad_response', undefined, false),
    );
    const settled = neverResolvingSettlement({
      readInventory: vi.fn().mockResolvedValue({ snapshot: 2, sources: [blockedSource] }),
    });
    const closeTimer = vi.spyOn(window, 'setTimeout');
    renderReplacement(blockedSource, settled);
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/^New API key$|^新的 API Key$/i), 'sk-uncommitted');
    await user.click(screen.getByRole('button', { name: /^Replace$|^更换$/i }));

    expect(await screen.findByText(/Couldn't replace the key|更换失败/i)).toBeTruthy();
    expect(screen.getByRole('button', { name: /^Retry$|^重试$/i })).toBeTruthy();
    expect(screen.queryByText(i18n.t('settings.models.repair.repaired'))).toBeNull();
    expect(screen.queryByText(/^Removed hops$|^已移除的路由项$/i)).toBeNull();
    expect(settled.readInventory).toHaveBeenCalledOnce();
    expect(settled.source).not.toHaveBeenCalled();
    expect(settled.unread).not.toHaveBeenCalled();
    expect(closeTimer.mock.calls.filter(([, delay]) => delay === 1400)).toHaveLength(0);
  });

  it.each([
    { path: 'response', failureClass: 'authoritative-terminal', retries: false },
    { path: 'inventory-missing', failureClass: 'authoritative-terminal', retries: false },
    { path: 'inventory-unread', failureClass: 'inconclusive', retries: true },
  ] as const)(
    'publishes the $path terminal failure before any trailing settlement resolves',
    async ({ path, failureClass, retries }) => {
      vi.spyOn(modelsApi, 'replaceCredential').mockRejectedValueOnce(new ApiCallError(
        path === 'response' ? 'source_not_found' : 'bad_response',
        undefined,
        path === 'response',
      ));
      const inventory = { snapshot: 3, sources: [] as Source[] };
      const settled = neverResolvingSettlement({
        readInventory: path === 'inventory-unread'
          ? vi.fn().mockRejectedValue(new Error('inventory unavailable'))
          : vi.fn().mockResolvedValue(inventory),
      });
      renderReplacement(blockedSource, settled);
      const user = userEvent.setup();

      await user.type(screen.getByLabelText(/^New API key$|^新的 API Key$/i), 'sk-failure');
      await user.click(screen.getByRole('button', { name: /^Replace$|^更换$/i }));

      const failure = await screen.findByText(/Couldn't replace the key|更换失败/i);
      expect(failure.closest('[data-failure-class]')?.getAttribute('data-failure-class')).toBe(failureClass);
      expect(screen.getAllByRole('button', { name: /^Cancel$|^取消$/i }))
        .toSatisfy((buttons: HTMLButtonElement[]) => buttons.every((button) => !button.disabled));
      expect(screen.queryByRole('button', { name: /^Retry$|^重试$/i }) !== null).toBe(retries);
      expect(screen.queryByText(i18n.t('settings.models.repair.repaired'))).toBeNull();
      expect(settled.source).not.toHaveBeenCalled();

      if (path === 'response') {
        expect(settled.readInventory).not.toHaveBeenCalled();
        expect(settled.gone).toHaveBeenCalledWith(blockedSource.id);
      } else if (path === 'inventory-missing') {
        expect(settled.readInventory).toHaveBeenCalledOnce();
        expect(settled.gone).toHaveBeenCalledWith(blockedSource.id, inventory);
      } else {
        expect(settled.readInventory).toHaveBeenCalledOnce();
        expect(settled.unread).toHaveBeenCalledOnce();
      }
    },
  );

  it.each([
    ['discovery_failed', 'retryable-provider', true],
    ['engine_down', 'inconclusive', true],
    ['source_not_found', 'authoritative-terminal', false],
  ] as const)(
    'renders %s through the shared %s failure taxonomy',
    async (code, failureClass, retries) => {
      vi.spyOn(modelsApi, 'replaceCredential').mockRejectedValueOnce(new ApiCallError(code));
      renderReplacement();
      const user = userEvent.setup();

      await user.type(screen.getByLabelText(/^New API key$|^新的 API Key$/i), 'sk-failure');
      await user.click(screen.getByRole('button', { name: /^Replace$|^更换$/i }));

      const failure = await screen.findByText(/Couldn't replace the key|更换失败/i);
      expect(failure.closest('[data-failure-class]')?.getAttribute('data-failure-class')).toBe(failureClass);
      expect(screen.queryByRole('button', { name: /^Retry$|^重试$/i }) !== null).toBe(retries);
    },
  );
});

describe('AddApiKeyDialog · vendor accessibility', () => {
  it('puts a mark on every row and on the field once a vendor is chosen', async () => {
    const entry = preset('anthropic');
    renderDialog();
    const user = userEvent.setup();

    await user.click(vendorField());
    const rows = await screen.findAllByRole('option');
    // The catalog's own order, with 自定义 at the bottom: it is the absence of a
    // vendor, so it belongs after them rather than ranked among them.
    const offered = offeredInOrder();
    expect(rows).toHaveLength(offered.length);
    expect(offered.map((id) => rows.indexOf(screen.getByRole('option', { name: vendorOptionName(id) }))))
      .toEqual(offered.map((_, position) => position));
    for (const row of rows) {
      expect(row.querySelector('.model-hub-add-key-vendor-glyph'), row.textContent ?? '').toBeTruthy();
    }

    await user.click(screen.getByRole('option', { name: entry.label }));
    expect(screen.queryAllByRole('option')).toEqual([]);
    expect(vendorField().textContent).toContain(entry.label);
    expect(vendorField().querySelector('.model-hub-add-key-vendor-glyph')).toBeTruthy();
  });

  // The highlight has to be readable, not merely visible: while the panel is
  // open, focus sits on an element that IS a combobox and NAMES the row the
  // arrow keys are on, so a screen reader announces each vendor as it is
  // reached. A picker that consumed the arrow keys on a roleless container
  // would pass the sighted half of this test and tell a screen-reader user
  // nothing — which is what the shared control's search input is for.
  it('announces the highlighted row and takes a keyboard to a vendor', async () => {
    const [top, next] = offeredInOrder();
    const reached = apiKeyVendorPreset(next);
    renderDialog();
    const user = userEvent.setup();

    await user.click(vendorField());
    const rows = await screen.findAllByRole('option');
    const focused = () => document.activeElement as HTMLElement;
    expect(focused().getAttribute('role')).toBe('combobox');
    expect(document.getElementById(focused().getAttribute('aria-controls') ?? '')?.getAttribute('role'))
      .toBe('listbox');
    expect(rows[0].getAttribute('aria-selected')).toBe('true');
    expect(rows[0].textContent).toContain(vendorOptionName(top));

    // Arrowing moves the highlight AND the focused combobox's pointer to it.
    const highlighted = () => document.getElementById(focused().getAttribute('aria-activedescendant') ?? '');
    await user.keyboard('{ArrowDown}');
    await waitFor(() => expect(highlighted()?.textContent).toContain(vendorOptionName(next)));
    expect(highlighted()?.getAttribute('role')).toBe('option');
    await user.keyboard('{Enter}');
    await waitFor(() => expect(vendorField().textContent).toContain(vendorOptionName(next)));
    expect(baseUrlInput().value).toBe(reached?.official_base_url);
  });

  it('names the field with its label and with the vendor it is holding', async () => {
    // A `<button>` is not labelable, so the visible 服务商 label never reaches the
    // trigger's accessible name and the field has to carry one. It carries the
    // label AND the selection: a name replaces the trigger's contents rather
    // than adding to them, so the label alone would announce the field as 服务商
    // and never say which vendor it holds — the one thing a native select
    // always said.
    const entry = preset('anthropic');
    const named = (vendor: string) => new RegExp(
      `^${i18n.t('settings.models.addKey.field.vendor')}\\s+${vendorOptionName(vendor)}$`,
    );
    renderDialog();
    const user = userEvent.setup();

    expect(screen.getByRole('combobox', { name: named(CUSTOM_VENDOR) })).toBe(vendorField());

    await selectVendor(user, entry.id);

    expect(screen.getByRole('combobox', { name: named(entry.id) })).toBe(vendorField());
  });

  it('treats picking the vendor already picked as no choice at all', async () => {
    // The switch is destructive by design — it resets the address, drops the
    // interface and retires the detection — so it must fire on a CHANGE, not on
    // a click. Reopening the menu and confirming what is already there is how a
    // user checks what they chose.
    const entry = preset('openai_chat');
    renderDialog();
    const user = userEvent.setup();

    await selectVendor(user, entry.id);
    await user.type(baseUrlInput(), '/edge');
    const edited = baseUrlInput().value;
    expect(edited).not.toBe(entry.official_base_url);

    await selectVendor(user, entry.id);

    expect(baseUrlInput().value).toBe(edited);
    expect(vendorField().textContent).toContain(entry.label);
  });

  it('narrows the catalog by name and offers nothing that is not in it', async () => {
    const entry = preset('anthropic');
    renderDialog();
    const user = userEvent.setup();

    await user.click(vendorField());
    const search = screen.getByPlaceholderText(/Search vendors|搜索服务商/);
    await user.type(search, entry.label);
    await waitFor(() => expect(screen.getAllByRole('option').map((row) => row.textContent))
      .toEqual([expect.stringContaining(entry.label)]));

    // A vendor is a catalog row and the request sends its id, so a typed name
    // that matches none is a dead end rather than a value to adopt — the
    // opposite of the model pickers this control also serves.
    await user.clear(search);
    await user.type(search, 'not-a-vendor');
    await waitFor(() => expect(screen.queryAllByRole('option')).toEqual([]));
    expect(screen.getByText(/No vendor by that name|没有同名的服务商/)).toBeTruthy();
  });

});
