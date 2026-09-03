// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { AddApiKeyDialog } from './AddApiKeyDialog';
import { createSourceCollectionReadAuthority } from './collectionReadAuthority';
import { ApiCallError, modelsApi } from './modelsApi';
import type {
  SourceMutationLanding,
  SourceMutationLandingReads,
  SourceMutationSettlement,
  TrackSourceMutation,
} from './mutationSettlement';
import {
  CONTRACT_VERSION,
  SOURCE_DISPLAY_NAME_MAX_LENGTH,
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

const DETECT = /^Detect$|^检测$/;
const CONFIRM = /Confirm & add|确认添加/;
const RETRY = /^Retry$|^重试$/i;

const fillCredentials = async () => {
  const user = userEvent.setup();
  await user.type(screen.getByRole('textbox', { name: /^Base URL$/i }), 'https://relay.example/v1');
  await user.type(screen.getByLabelText(/^API key$/i), 'secret-key');
  return user;
};

const openManualProtocol = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(screen.getByRole('button', { name: /Manually specify interface type|手动指定接口类型/i }));
};

const clickDetect = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(screen.getByRole('button', { name: DETECT }));
};

const clickConfirm = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(await screen.findByRole('button', { name: CONFIRM }));
};

beforeEach(async () => {
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('AddApiKeyDialog', () => {
  it('detects without persisting, names the protocol, and drops the report when credentials change', async () => {
    vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValueOnce(observed());
    const create = vi.spyOn(modelsApi, 'createApiKeySource');
    renderDialog();
    const user = await fillCredentials();

    await clickDetect(user);
    expect(await screen.findByText(/OpenAI Chat Completions/)).toBeTruthy();
    expect(screen.getByText(/Fetched 2 models|拉到 2 个模型/i)).toBeTruthy();
    expect(screen.queryByText('model-a')).toBeNull();
    expect(screen.getByRole('button', { name: CONFIRM })).toBeTruthy();
    expect(create).not.toHaveBeenCalled();

    await user.type(screen.getByRole('textbox', { name: /^Base URL$/i }), '/changed');
    expect(screen.queryByText(/Fetched 2 models|拉到 2 个模型/i)).toBeNull();
    expect(screen.getByRole('button', { name: DETECT })).toBeTruthy();
  });

  it('puts protocol-family glyphs on concrete interface names and never on Auto detect', async () => {
    vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValueOnce(
      observed({ protocol: 'anthropic' }),
    );
    renderDialog();
    expect(screen.getByText(/Identified automatically once Base URL and API key are filled|填好 Base URL 和 API Key 后自动识别/)).toBeTruthy();
    const user = await fillCredentials();
    await openManualProtocol(user);

    expect(screen.getByRole('button', { name: /Auto detect|自动探测/i }).querySelector('svg')).toBeNull();
    expect(screen.getByRole('button', { name: 'Anthropic Messages' }).querySelector('svg')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'OpenAI Responses' }).querySelector('svg')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'OpenAI Chat Completions' }).querySelector('svg')).toBeTruthy();

    await clickDetect(user);
    await waitFor(() => {
      const identified = document.querySelector('.model-hub-add-key-strip--success');
      expect(identified?.textContent).toMatch(/Anthropic Messages/);
      expect(identified?.querySelector('.model-hub-add-key-protocol-glyph')).toBeTruthy();
    });
  });

  it('sends one manually selected interface on the first observation and create', async () => {
    const observe = vi.spyOn(modelsApi, 'observeApiKeySource')
      .mockResolvedValueOnce(observed({ protocol: 'openai_responses' }));
    const create = vi.spyOn(modelsApi, 'createApiKeySource').mockResolvedValueOnce({
      source,
      added_to: [],
      adopted_by: [],
    });
    renderDialog();
    const user = await fillCredentials();

    await openManualProtocol(user);
    expect(screen.getByRole('button', { name: /Auto detect|自动探测/i }).getAttribute('aria-pressed')).toBe('true');
    await user.click(screen.getByRole('button', { name: 'OpenAI Responses' }));
    await clickDetect(user);
    await clickConfirm(user);

    await waitFor(() => expect(create).toHaveBeenCalledOnce());
    expect(observe.mock.calls[0][0].protocol).toBe('openai_responses');
    expect(create.mock.calls[0][0].protocol).toBe('openai_responses');
  });

  it('turns an ambiguous auto-detection into one exact manual retry, then confirms', async () => {
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

    await clickDetect(user);
    const retry = await screen.findByRole('button', { name: /^Retry$|^重试$/i });
    expect((retry as HTMLButtonElement).disabled).toBe(true);
    await user.click(screen.getByRole('button', { name: 'OpenAI Responses' }));
    await user.click(retry);

    expect(await screen.findByRole('button', { name: CONFIRM })).toBeTruthy();
    expect(create).not.toHaveBeenCalled();
    await clickConfirm(user);

    await waitFor(() => expect(create).toHaveBeenCalledOnce());
    const secondObservation = observe.mock.calls[1][0];
    expect(secondObservation.protocol).toBe('openai_responses');
    expect(create.mock.calls[0][0].protocol).toBe('openai_responses');
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

    await clickDetect(user);
    await screen.findByRole('button', { name: /Add anyway|仍要添加/i });
    await user.click(screen.getByRole('button', { name: /^Retry$|^重试$/i }));
    await waitFor(() => expect(observe).toHaveBeenCalledTimes(2));
    expect(create).not.toHaveBeenCalled();
    expect(screen.queryByText(/The list could not be read|清单没能读出来/i)).toBeNull();

    await user.click(await screen.findByRole('button', { name: /Add anyway|仍要添加/i }));
    await waitFor(() => expect(create).toHaveBeenCalledOnce());
    expect(create.mock.calls[0][0].protocol).toBe('openai_chat');
    expect(create.mock.calls[0][0].accept_unavailable_inventory).toBe(true);
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

    await clickDetect(user);
    await screen.findByRole('button', { name: /Add anyway|仍要添加/i });
    await user.click(screen.getByRole('button', { name: /^Retry$|^重试$/i }));

    await waitFor(() => expect(observe).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/cannot tell which interface|无法判断是哪种接口/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Add anyway|仍要添加/i })).toBeNull();
  });

  it('preserves unavailable-inventory consent across lost-response reconciliation', async () => {
    vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValueOnce(
      observed({ discovery: 'failed', models: [] }),
    );
    const create = vi.spyOn(modelsApi, 'createApiKeySource')
      .mockRejectedValueOnce(new TypeError('response lost'))
      .mockResolvedValueOnce({ source, added_to: [], adopted_by: [] });
    vi.spyOn(modelsApi, 'listSources').mockResolvedValueOnce([]);
    renderDialog();
    const user = await fillCredentials();

    await clickDetect(user);
    await user.click(await screen.findByRole('button', { name: /Add anyway|仍要添加/i }));
    await user.click(await screen.findByRole('button', { name: /^Retry$|^重试$/i }));

    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    expect(create.mock.calls.map(([draft]) => draft.accept_unavailable_inventory)).toEqual([
      true,
      true,
    ]);
    expect(create.mock.calls[1][0].client_nonce).toBe(create.mock.calls[0][0].client_nonce);
  });

  it('aborts an in-flight detect and returns to the form without dismissing', async () => {
    let wasAborted = false;
    vi.spyOn(modelsApi, 'observeApiKeySource').mockImplementation((_draft, signal) => new Promise((_resolve, reject) => {
      signal?.addEventListener('abort', () => {
        wasAborted = true;
        reject(new DOMException('aborted', 'AbortError'));
      }, { once: true });
    }));
    const { onClose } = renderDialog();
    const user = await fillCredentials();

    await clickDetect(user);
    await user.click(screen.getAllByRole('button', { name: /^Cancel$|^取消$/i }).at(-1)!);

    expect(wasAborted).toBe(true);
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: DETECT })).toBeTruthy();
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

    await clickDetect(user);
    await clickConfirm(user);
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

    await clickDetect(user);
    await clickConfirm(user);
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

    await clickDetect(user);
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
    vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValueOnce(observed());
    vi.spyOn(modelsApi, 'createApiKeySource').mockImplementationOnce(async (draft) => {
      nonce = draft.client_nonce;
      throw new TypeError('response lost');
    });
    vi.spyOn(modelsApi, 'listSources').mockReturnValueOnce(pendingInventory.promise);
    const { onAdded } = renderDialog();
    const user = await fillCredentials();

    await clickDetect(user);
    await clickConfirm(user);
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
    const detect = screen.getByRole('button', { name: DETECT }) as HTMLButtonElement;

    await user.type(name, '   ');
    expect(detect.disabled).toBe(true);
    await user.clear(name);
    await user.type(name, 'x'.repeat(SOURCE_DISPLAY_NAME_MAX_LENGTH + 1));
    expect(detect.disabled).toBe(true);
    await user.click(detect);
    expect(observe).not.toHaveBeenCalled();
  });

  it('counts supplementary display-name characters as JSON Schema code points', async () => {
    renderDialog();
    const user = await fillCredentials();
    const name = screen.getByRole('textbox', { name: /^Name|^名称/i });
    const detect = screen.getByRole('button', { name: DETECT }) as HTMLButtonElement;

    await user.type(name, '😀'.repeat(SOURCE_DISPLAY_NAME_MAX_LENGTH));
    expect(detect.disabled).toBe(false);
    await user.type(name, '😀');
    expect(detect.disabled).toBe(true);
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

    await clickDetect(user);
    await clickConfirm(user);

    expect(await screen.findByText(i18n.t('settings.models.addKey.fail.unclassified'))).toBeTruthy();
    expect(screen.queryByText('modelHub.errors.discovery_failed')).toBeNull();
    expect((name as HTMLInputElement).disabled).toBe(false);
    expect(screen.queryByText(/not confirmed saved|确认.*保存/i)).toBeNull();
    expect(list).not.toHaveBeenCalled();
    await user.clear(name);
    await user.type(name, 'Fixed relay');
    expect(screen.getByRole('button', { name: DETECT })).toBeTruthy();
    expect(create).toHaveBeenCalledOnce();
  });

  it('retires the evidence of every exit that would persist without observing again', async () => {
    // One rule, stated over how a state exits rather than over which states exist:
    // ①″'s report and the protocol a server-named refusal still holds were each
    // proved against this endpoint, this credential and this probe constraint, so
    // changing any of those three ends both. Every combination is driven through
    // the UI so neither arm can quietly become its own special case.
    type Act = (user: ReturnType<typeof userEvent.setup>) => Promise<void>;
    const exits: Act[] = [
      async (user) => {
        await clickDetect(user);
        await screen.findByRole('button', { name: CONFIRM });
      },
      async (user) => {
        await clickDetect(user);
        await clickConfirm(user);
        await screen.findByRole('button', { name: RETRY });
      },
    ];
    const connectionEdits: Act[] = [
      async (user) => { await user.type(screen.getByRole('textbox', { name: /^Base URL$/i }), '/v2'); },
      async (user) => { await user.type(screen.getByLabelText(/^API key$/i), 'x'); },
      async (user) => {
        await openManualProtocol(user);
        await user.click(screen.getByRole('button', { name: 'Anthropic Messages' }));
      },
    ];

    for (const reachExit of exits) {
      for (const edit of connectionEdits) {
        vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValue(observed());
        const create = vi.spyOn(modelsApi, 'createApiKeySource').mockRejectedValue(
          new ApiCallError('invalid_source', 'modelHub.errors.discovery_failed', true, [], [], [], 422),
        );
        renderDialog();
        const user = await fillCredentials();
        await reachExit(user);
        const persistedBefore = create.mock.calls.length;

        await edit(user);

        expect(screen.getByRole('button', { name: DETECT })).toBeTruthy();
        expect(screen.queryByRole('button', { name: CONFIRM })).toBeNull();
        expect(screen.queryByRole('button', { name: RETRY })).toBeNull();
        expect(create.mock.calls.length).toBe(persistedBefore);

        cleanup();
        vi.restoreAllMocks();
      }
    }
  });

  it('keeps a state whose own primary re-observes across the same edit', async () => {
    // The counterpart of the rule above, and the reason it is written over exits:
    // ④'s 重试 reads the fields as they now stand, so naming an interface there is
    // how the user answers it — not evidence to retire.
    vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValue(observed({
      outcome: 'ambiguous',
      authenticated: 'unknown',
      protocol: null,
      discovery: 'not_attempted',
      models: [],
    }));
    renderDialog();
    const user = await fillCredentials();

    await clickDetect(user);
    await user.click(await screen.findByRole('button', { name: 'Anthropic Messages' }));

    expect(screen.getByRole('button', { name: RETRY })).toBeTruthy();
    expect(screen.queryByRole('button', { name: DETECT })).toBeNull();
  });

  it('names the interface Detect will send while the disclosure is closed', async () => {
    // The collapsed row and the expanded selector are one selection rendered two
    // ways, so the row states the active constraint rather than the default it
    // replaced, and the promise of automatic identification goes with it.
    const observe = vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValue(observed());
    renderDialog();
    const user = await fillCredentials();
    const autoRow = document.querySelector('.model-hub-add-key-protocol-active');
    expect(autoRow?.textContent).toMatch(/Auto detect|自动探测/);
    expect(autoRow?.querySelector('.model-hub-add-key-protocol-glyph')).toBeNull();

    await openManualProtocol(user);
    expect(document.querySelector('.model-hub-add-key-protocol-active')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Anthropic Messages' }));
    await openManualProtocol(user);

    const chosenRow = document.querySelector('.model-hub-add-key-protocol-active');
    expect(chosenRow?.textContent).toMatch(/Anthropic Messages/);
    expect(chosenRow?.querySelector('.model-hub-add-key-protocol-glyph')).toBeTruthy();
    expect(screen.queryByText(i18n.t('settings.models.addKey.protocol.idleHint'))).toBeNull();

    await clickDetect(user);
    await screen.findByRole('button', { name: CONFIRM });
    expect(observe.mock.calls[0][0].protocol).toBe('anthropic');
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

    await clickDetect(user);
    await clickConfirm(user);

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

    await clickDetect(user);
    await clickConfirm(user);

    expect(await screen.findByText(/model list did not come back|没拿到它的模型清单/i)).toBeTruthy();
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

    await clickDetect(user);
    await clickConfirm(user);

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
