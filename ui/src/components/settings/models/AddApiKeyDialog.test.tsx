// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { AddApiKeyDialog } from './AddApiKeyDialog';
import { createSourceCollectionReadAuthority } from './collectionReadAuthority';
import { ApiCallError, modelsApi } from './modelsApi';
import type { SourceMutationSettlement, TrackSourceMutation } from './mutationSettlement';
import {
  CONTRACT_VERSION,
  SOURCE_DISPLAY_NAME_MAX_LENGTH,
  SOURCE_PROTOCOLS,
  type RouteHopRef,
  type Source,
  type SourceObservation,
  type SupplyGap,
} from './types';

const observed = (patch: Partial<SourceObservation> = {}): SourceObservation => ({
  contract_version: CONTRACT_VERSION,
  outcome: 'observed',
  reachable: true,
  authenticated: 'authenticated',
  protocol: 'openai_chat',
  discovery: 'succeeded',
  models: ['model-a', 'model-b'],
  ...patch,
});

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
  source: vi.fn().mockResolvedValue(undefined),
  gone: vi.fn().mockResolvedValue(undefined),
  unread: vi.fn().mockResolvedValue(undefined),
  release: vi.fn(),
  readInventory: vi.fn().mockResolvedValue({ snapshot: 1, sources: [blockedSource] }),
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

const fillCredentials = async () => {
  const user = userEvent.setup();
  await user.type(screen.getByRole('textbox', { name: /^Base URL$/i }), 'https://relay.example/v1');
  await user.type(screen.getByLabelText(/^API key$/i), 'secret-key');
  return user;
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('AddApiKeyDialog', () => {
  it('pulls models without persisting and drops the report when credentials change', async () => {
    vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValueOnce(observed());
    const create = vi.spyOn(modelsApi, 'createApiKeySource');
    renderDialog();
    const user = await fillCredentials();

    await user.click(screen.getByRole('button', { name: /Fetch models|拉取型号/i }));
    expect(await screen.findByText(/Fetched 2 models|拉到 2 个型号/i)).toBeTruthy();
    expect(create).not.toHaveBeenCalled();

    await user.type(screen.getByRole('textbox', { name: /^Base URL$/i }), '/changed');
    expect(screen.queryByText(/Fetched 2 models|拉到 2 个型号/i)).toBeNull();
  });

  it('uses the interface choice only as the next complete probe order', async () => {
    const observe = vi.spyOn(modelsApi, 'observeApiKeySource')
      .mockResolvedValueOnce(observed({
        outcome: 'ambiguous',
        authenticated: 'unknown',
        protocol: null,
        discovery: 'not_attempted',
        models: [],
      }))
      .mockResolvedValueOnce(observed({ protocol: 'openai_responses' }));
    const create = vi.spyOn(modelsApi, 'createApiKeySource').mockResolvedValueOnce({
      source,
      added_to: [],
      adopted_by: [],
    });
    const { onAdded } = renderDialog();
    const user = await fillCredentials();

    await user.click(screen.getByRole('button', { name: /^Add$|^添加$/i }));
    const retry = await screen.findByRole('button', { name: /^Retry$|^重试$/i });
    expect((retry as HTMLButtonElement).disabled).toBe(true);
    await user.click(screen.getByRole('button', { name: 'OpenAI Responses' }));
    await user.click(retry);

    await waitFor(() => expect(create).toHaveBeenCalledOnce());
    const secondObservation = observe.mock.calls[1][0];
    expect(secondObservation.protocol_order?.[0]).toBe('openai_responses');
    expect(new Set(secondObservation.protocol_order)).toEqual(new Set(SOURCE_PROTOCOLS));
    expect(create.mock.calls[0][0]).not.toHaveProperty('protocol');
    expect(create.mock.calls[0][0].protocol_order?.[0]).toBe('openai_responses');
    expect(onAdded).toHaveBeenCalledWith({ source, added_to: [], adopted_by: [] });
  });

  it('retries the complete observation from inventory failure and persists only through Add anyway', async () => {
    const inventory = observed({ discovery: 'failed', models: [] });
    const observe = vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValue(inventory);
    const create = vi.spyOn(modelsApi, 'createApiKeySource').mockResolvedValue({
      source,
      added_to: [],
      adopted_by: [],
    });
    renderDialog();
    const user = await fillCredentials();

    await user.click(screen.getByRole('button', { name: /^Add$|^添加$/i }));
    await screen.findByRole('button', { name: /Add anyway|仍要添加/i });
    await user.click(screen.getByRole('button', { name: /^Retry$|^重试$/i }));
    await waitFor(() => expect(observe).toHaveBeenCalledTimes(2));
    expect(create).not.toHaveBeenCalled();
    expect(screen.queryByText(/The list could not be read|清单没能读出来/i)).toBeNull();

    await user.click(await screen.findByRole('button', { name: /Add anyway|仍要添加/i }));
    await waitFor(() => expect(create).toHaveBeenCalledOnce());
    expect(create.mock.calls[0][0]).not.toHaveProperty('protocol');
    expect(create.mock.calls[0][0].protocol_order?.[0]).toBe('openai_chat');
  });

  it('adopts the new observation result when inventory retry moves to interface undetermined', async () => {
    const inventory = observed({ discovery: 'failed', models: [] });
    const ambiguous = observed({
      outcome: 'ambiguous',
      authenticated: 'unknown',
      protocol: null,
      discovery: 'not_attempted',
      models: [],
    });
    const observe = vi.spyOn(modelsApi, 'observeApiKeySource')
      .mockResolvedValueOnce(inventory)
      .mockResolvedValueOnce(ambiguous);
    renderDialog();
    const user = await fillCredentials();

    await user.click(screen.getByRole('button', { name: /^Add$|^添加$/i }));
    await screen.findByRole('button', { name: /Add anyway|仍要添加/i });
    await user.click(screen.getByRole('button', { name: /^Retry$|^重试$/i }));

    await waitFor(() => expect(observe).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/cannot tell which interface|无法判断是哪种接口/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Add anyway|仍要添加/i })).toBeNull();
  });

  it('aborts an in-flight pull and returns to the form without dismissing', async () => {
    let wasAborted = false;
    vi.spyOn(modelsApi, 'observeApiKeySource').mockImplementation((_draft, signal) => new Promise((_resolve, reject) => {
      signal?.addEventListener('abort', () => {
        wasAborted = true;
        reject(new DOMException('aborted', 'AbortError'));
      }, { once: true });
    }));
    const { onClose } = renderDialog();
    const user = await fillCredentials();

    await user.click(screen.getByRole('button', { name: /Fetch models|拉取型号/i }));
    await user.click(screen.getAllByRole('button', { name: /^Cancel$|^取消$/i }).at(-1)!);

    expect(wasAborted).toBe(true);
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /Fetch models|拉取型号/i })).toBeTruthy();
  });

  it('reconciles a lost source-create response by nonce without posting twice', async () => {
    let nonce: string | undefined;
    vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValueOnce(observed());
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

    await user.click(screen.getByRole('button', { name: /^Add$|^添加$/i }));
    await user.click(await screen.findByRole('button', { name: /^Retry$|^重试$/i }));

    await waitFor(() => expect(onAdded).toHaveBeenCalledOnce());
    expect(list).toHaveBeenCalledOnce();
    expect(create).toHaveBeenCalledOnce();
    expect(nonce).toMatch(/^scn_[a-z0-9]{16,64}$/);
  });

  it('retries source creation only after the nonce is confirmed absent', async () => {
    vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValueOnce(observed());
    const create = vi.spyOn(modelsApi, 'createApiKeySource')
      .mockRejectedValueOnce(new TypeError('response lost'))
      .mockResolvedValueOnce({ source, added_to: [], adopted_by: [] });
    const list = vi.spyOn(modelsApi, 'listSources').mockResolvedValueOnce([]);
    renderDialog();
    const user = await fillCredentials();

    await user.click(screen.getByRole('button', { name: /^Add$|^添加$/i }));
    await user.click(await screen.findByRole('button', { name: /^Retry$|^重试$/i }));

    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    expect(list).toHaveBeenCalledOnce();
    expect(create.mock.calls[1][0].client_nonce).toBe(create.mock.calls[0][0].client_nonce);
  });

  it('cannot create or navigate after cancelling a pending lost-response reconciliation', async () => {
    const pendingInventory = deferred<Source[]>();
    vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValueOnce(observed());
    const create = vi.spyOn(modelsApi, 'createApiKeySource').mockRejectedValueOnce(new TypeError('response lost'));
    vi.spyOn(modelsApi, 'listSources').mockReturnValueOnce(pendingInventory.promise);
    const { onAdded, onClose } = renderDialog();
    const user = await fillCredentials();

    await user.click(screen.getByRole('button', { name: /^Add$|^添加$/i }));
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
    vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValueOnce(observed());
    vi.spyOn(modelsApi, 'createApiKeySource').mockImplementationOnce(async (draft) => {
      nonce = draft.client_nonce;
      throw new TypeError('response lost');
    });
    vi.spyOn(modelsApi, 'listSources').mockReturnValueOnce(pendingInventory.promise);
    const { onAdded } = renderDialog();
    const user = await fillCredentials();

    await user.click(screen.getByRole('button', { name: /^Add$|^添加$/i }));
    await user.click(await screen.findByRole('button', { name: /^Retry$|^重试$/i }));
    await user.click(screen.getAllByRole('button', { name: /^Cancel$|^取消$/i }).at(-1)!);
    pendingInventory.resolve([{ ...source, client_nonce: nonce }]);
    await pendingInventory.promise;

    expect(onAdded).not.toHaveBeenCalled();
  });

  it('rejects a trimmed blank or overlong display name before observation', async () => {
    const observe = vi.spyOn(modelsApi, 'observeApiKeySource');
    renderDialog();
    const user = await fillCredentials();
    const name = screen.getByRole('textbox', { name: /^Name|^名称/i });
    const add = screen.getByRole('button', { name: /^Add$|^添加$/i }) as HTMLButtonElement;

    await user.type(name, '   ');
    expect(add.disabled).toBe(true);
    await user.clear(name);
    await user.type(name, 'x'.repeat(SOURCE_DISPLAY_NAME_MAX_LENGTH + 1));
    expect(add.disabled).toBe(true);
    await user.click(add);
    expect(observe).not.toHaveBeenCalled();
  });

  it('counts supplementary display-name characters as JSON Schema code points', async () => {
    renderDialog();
    const user = await fillCredentials();
    const name = screen.getByRole('textbox', { name: /^Name|^名称/i });
    const add = screen.getByRole('button', { name: /^Add$|^添加$/i }) as HTMLButtonElement;

    await user.type(name, '😀'.repeat(SOURCE_DISPLAY_NAME_MAX_LENGTH));
    expect(add.disabled).toBe(false);
    await user.type(name, '😀');
    expect(add.disabled).toBe(true);
  });

  it('keeps a server-named create validation failure editable and out of save reconciliation', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === '/api/csrf-token') return Response.json({ csrf_token: 'csrf' });
      return Response.json({ ok: false, error: 'invalid_source', detail: 'modelHub.errors.discovery_failed' }, { status: 422 });
    }));
    let validationError: unknown;
    try {
      await modelsApi.createApiKeySource({ kind: 'api_key', vendor: 'custom', base_url: 'https://relay.example/v1', key: 'secret-key' });
    } catch (error) {
      validationError = error;
    }
    vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValueOnce(observed());
    const create = vi.spyOn(modelsApi, 'createApiKeySource').mockRejectedValue(validationError);
    const list = vi.spyOn(modelsApi, 'listSources');
    renderDialog();
    const user = await fillCredentials();
    const name = screen.getByRole('textbox', { name: /^Name|^名称/i });
    await user.type(name, 'Relay');

    await user.click(screen.getByRole('button', { name: /^Add$|^添加$/i }));

    expect(await screen.findByText(i18n.t('settings.models.addKey.fail.unclassified'))).toBeTruthy();
    expect(screen.queryByText('modelHub.errors.discovery_failed')).toBeNull();
    expect((name as HTMLInputElement).disabled).toBe(false);
    expect(screen.queryByText(/not confirmed saved|确认.*保存/i)).toBeNull();
    expect(list).not.toHaveBeenCalled();
    await user.clear(name);
    await user.type(name, 'Fixed relay');
    expect(screen.getByRole('button', { name: /^Add$|^添加$/i })).toBeTruthy();
    expect(create).toHaveBeenCalledOnce();
  });

  it('renders the safe observation cause when create rejects after probing', async () => {
    const failure = new ApiCallError(
      'discovery_failed',
      'modelHub.errors.discovery_failed',
      true,
      [],
      [],
      [],
      422,
      observed({
        outcome: 'authentication_failed',
        authenticated: 'rejected',
        protocol: null,
        discovery: 'not_attempted',
        models: [],
      }),
    );
    vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValue(observed());
    vi.spyOn(modelsApi, 'createApiKeySource').mockRejectedValue(failure);
    renderDialog();
    const user = await fillCredentials();

    await user.click(screen.getByRole('button', { name: /^Add$|^添加$/i }));

    expect(await screen.findByText(i18n.t('settings.models.addKey.fail.auth'))).toBeTruthy();
    expect(screen.queryByText(i18n.t('settings.models.addKey.fail.unclassified'))).toBeNull();
  });

  it('re-enters the classified observation state when create rejects with inventory evidence', async () => {
    const failure = new ApiCallError(
      'discovery_failed',
      'modelHub.errors.discovery_failed',
      true,
      [],
      [],
      [],
      422,
      observed({ discovery: 'failed', models: [] }),
    );
    vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValue(observed());
    vi.spyOn(modelsApi, 'createApiKeySource').mockRejectedValue(failure);
    renderDialog();
    const user = await fillCredentials();

    await user.click(screen.getByRole('button', { name: /^Add$|^添加$/i }));

    expect(await screen.findByText(/model list did not come back|没拿到它的型号清单/i)).toBeTruthy();
    expect(screen.getByRole('button', { name: /Add anyway|仍要添加/i })).toBeTruthy();
    expect(screen.queryByText(i18n.t('settings.models.addKey.fail.unclassified'))).toBeNull();
  });

  it('re-enters the classified observation state when create rejects with ambiguous evidence', async () => {
    const failure = new ApiCallError(
      'discovery_failed',
      'modelHub.errors.discovery_failed',
      true,
      [],
      [],
      [],
      422,
      observed({
        outcome: 'ambiguous',
        reachable: true,
        authenticated: 'authenticated',
        protocol: null,
        discovery: 'not_attempted',
        models: [],
      }),
    );
    vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValue(observed());
    vi.spyOn(modelsApi, 'createApiKeySource').mockRejectedValue(failure);
    renderDialog();
    const user = await fillCredentials();

    await user.click(screen.getByRole('button', { name: /^Add$|^添加$/i }));

    expect(await screen.findByText(/cannot tell which interface|无法判断是哪种接口/i)).toBeTruthy();
    expect(screen.queryByText(i18n.t('settings.models.addKey.fail.unclassified'))).toBeNull();
  });

  it('reaches the credential PUT with only the trimmed key', async () => {
    const requests: Array<{ input: string; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === '/api/csrf-token') return Response.json({ csrf_token: 'csrf' });
      requests.push({ input: path, init });
      return Response.json({
        source: { ...blockedSource, state: { status: 'active' } },
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
    expect(settled.source).toHaveBeenCalledWith(expect.objectContaining({ state: { status: 'active' } }));
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
        source: { ...blockedSource, state: { status: 'active' } },
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
      [blockedSource.id, { key: 'sk-force', force: true }],
    ]);
    expect(await screen.findByText(/^Removed hops$|^已移除的跳$/i)).toBeTruthy();
    expect(screen.getByText(/now have no usable source|现在没有可用来源/i)).toBeTruthy();
  });

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
