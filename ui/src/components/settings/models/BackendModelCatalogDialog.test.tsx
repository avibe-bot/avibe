// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { BackendModelCatalogDialog } from './BackendModelCatalogDialog';
import { blankBackendModel, candidateBackendModel } from './backendCatalog';
import { ApiCallError, modelsApi } from './modelsApi';
import type { AgentSupply, BackendModel, ModelCandidate, ModelsDevMatch, RouteHop } from './types';

const model = (id: string, overrides: Partial<BackendModel> = {}): BackendModel => ({
  ...blankBackendModel(),
  id,
  ...overrides,
});

const locked = model('claude-default', { locked: true, routeable: false });

const candidate = (id: string, overrides: Partial<ModelCandidate> = {}): ModelCandidate => ({
  id,
  display_name: null,
  reasoning_efforts: [],
  suppliers: [],
  origin: 'provider',
  ...overrides,
});

/** What the one read behind the picker answers. Stubbed per test rather than by
 *  default: a test that never opens the picker must not be able to pass on a
 *  group it silently supplied. */
const offered = (groups: Partial<Record<'builtin' | 'providers' | 'in_list', ModelCandidate[]>> = {}) => ({
  builtin: groups.builtin ?? [],
  providers: groups.providers ?? [],
  in_list: groups.in_list ?? [],
});

/** The stale-candidate refusal: nothing was committed, and the `changed` map is
 *  the whole answer — hence its own shape rather than a guard refusal. */
const staleCandidates = (changed: Record<string, RouteHop[]>) => new ApiCallError(
  'candidate_suppliers_changed',
  'modelHub.errors.candidate_suppliers_changed',
  true,
  [],
  [],
  [],
  409,
  undefined,
  changed,
);

const enabledAddModels = () => waitFor(() => {
  const button = screen.getByRole('button', { name: 'Add models' }) as HTMLButtonElement;
  expect(button.disabled).toBe(false);
  return button;
});

const agent = (catalog: BackendModel[] | undefined, overrides: Partial<AgentSupply> = {}): AgentSupply => ({
  backend: 'claude',
  cli_present: true,
  mode: 'hub',
  menu_kind: 'fixed',
  sources: { order: [], eligibility: [] },
  routes: {},
  builtin_models: ['legacy-a', 'legacy-b'],
  menu: null,
  ...(catalog ? { catalog_models: catalog } : {}),
  ...overrides,
});

const renderDialog = (overrides: Partial<React.ComponentProps<typeof BackendModelCatalogDialog>> = {}) => {
  const onClose = vi.fn();
  const onSaved = vi.fn();
  const onObserved = vi.fn();
  render(
    <I18nextProvider i18n={i18n}>
      <BackendModelCatalogDialog
        open
        backend="claude"
        canReadSources
        // Nothing named by default: what a hop reads as without the page's
        // Sources is the case every other test here is about, so a test that
        // wants a name says so.
        sourceNames={{}}
        onClose={onClose}
        onSaved={onSaved}
        onObserved={onObserved}
        catalogWrite={{ pending: false, track: async (work) => work() }}
        {...overrides}
      />
    </I18nextProvider>,
  );
  return { onClose, onSaved, onObserved };
};

afterEach(async () => {
  cleanup();
  vi.restoreAllMocks();
  await i18n.changeLanguage('en');
});

describe('BackendModelCatalogDialog', () => {
  it('enables catalog controls only after the baseline read completes', async () => {
    let finishRead!: (value: AgentSupply) => void;
    const pendingRead = new Promise<AgentSupply>((resolve) => { finishRead = resolve; });
    vi.spyOn(modelsApi, 'getAgentSources').mockReturnValue(pendingRead);
    renderDialog();

    expect(screen.getByRole('heading', { name: 'Claude Code models' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Add models' }).hasAttribute('disabled')).toBe(true);
    expect((screen.getByLabelText('Search name or model ID') as HTMLInputElement).disabled).toBe(true);
    expect(screen.queryByText('alpha')).toBeNull();

    await act(async () => { finishRead(agent([model('alpha')])); });

    expect(screen.getByRole('button', { name: 'Reorder alpha' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Add models' }).hasAttribute('disabled')).toBe(false);
    expect((screen.getByLabelText('Search name or model ID') as HTMLInputElement).disabled).toBe(false);
  });

  it('shows a locked row without any way to edit, remove, or reorder it', async () => {
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([locked, model('alpha')]));
    renderDialog();

    expect(await screen.findByRole('heading', { name: 'Claude Code models' })).toBeTruthy();
    expect(await screen.findByText('claude-default')).toBeTruthy();
    expect(screen.getByText('Default')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Reorder alpha' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Reorder claude-default' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Edit claude-default' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Remove claude-default' })).toBeNull();
    expect(screen.getByText('2 models')).toBeTruthy();
  });

  it('opens the named row\'s editor alone and saves its answer, never on a locked row', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([locked, model('alpha')]));
    const write = vi.spyOn(modelsApi, 'putAgentModels').mockResolvedValue(agent([locked, model('alpha', { display_name: 'Alpha' })]));
    const { onClose, onSaved } = renderDialog({ focus: { modelId: 'alpha', action: 'edit' } });
    expect(await screen.findByRole('button', { name: 'Save model' })).toBeTruthy();
    // The editor is the whole dialog: the list is not shown behind it.
    expect(screen.queryByRole('heading', { name: 'Claude Code models' })).toBeNull();
    await user.type(screen.getByLabelText('Display name'), 'Alpha');
    await user.click(screen.getByRole('button', { name: 'Save model' }));
    await waitFor(() => expect(onClose).toHaveBeenCalledWith(undefined));
    expect(write).toHaveBeenCalledTimes(1);
    expect(onSaved).toHaveBeenCalled();
    cleanup();

    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([locked, model('alpha')]));
    renderDialog({ focus: { modelId: 'claude-default', action: 'edit' } });
    await screen.findByText('claude-default');
    expect(screen.queryByRole('button', { name: 'Save model' })).toBeNull();
  });

  it('closes a focused editor without writing or showing the list', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([model('alpha')]));
    const write = vi.spyOn(modelsApi, 'putAgentModels');
    const { onClose } = renderDialog({ focus: { modelId: 'alpha', action: 'edit' } });
    await user.click(await screen.findByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onClose.mock.calls[0][0]?.removed).toBeFalsy();
    expect(write).not.toHaveBeenCalled();
  });

  it.each([
    ['routed', { beta: { hops: [{ source_id: 'src_a', model_id: 'beta-air' }] } }],
    ['unrouted', {}],
  ])('removes a %s focused row straight from one confirmation', async (_, routes) => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([model('alpha'), model('beta')], { routes }));
    const write = vi.spyOn(modelsApi, 'putAgentModels').mockResolvedValue(agent([model('alpha')]));
    const { onClose } = renderDialog({ focus: { modelId: 'beta', action: 'remove' } });
    const confirm = await screen.findByRole('dialog', { name: 'Remove beta?' });
    if (Object.keys(routes).length > 0) expect(within(confirm).getByRole('alert').textContent).toContain('beta-air');
    expect(screen.queryByText('2 models')).toBeNull();
    expect(write).not.toHaveBeenCalled();
    await user.click(within(confirm).getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(onClose).toHaveBeenCalledWith({ removed: true }));
    expect(write).toHaveBeenCalledTimes(1);
    expect(write.mock.calls[0][1].models.map((entry: BackendModel) => entry.id)).toEqual(['alpha']);
  });

  it('forces a confirmed focused removal through the guard without asking again, even when the plan moves', async () => {
    const user = userEvent.setup();
    const shown = [{ backend: 'claude' as const, menu_model: 'beta', source_id: 'src_a', model_id: 'beta-air', position: 1 }];
    const moved = [{ backend: 'claude' as const, menu_model: 'beta', source_id: 'src_b', model_id: 'beta-max', position: 1 }];
    const gaps = [{ backend: 'claude' as const, model_id: 'beta', agents: [] }];
    const refusal = (hops: typeof shown, interrupt: typeof gaps = []) => new ApiCallError(
      'backend_model_in_route', 'modelHub.errors.backend_model_in_route', true, interrupt, [], hops, 409,
    );
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([model('alpha'), model('beta')], {
      routes: { beta: { hops: [{ source_id: 'src_a', model_id: 'beta-air' }] } },
    }));
    const write = vi.spyOn(modelsApi, 'putAgentModels')
      .mockRejectedValueOnce(refusal(shown))
      .mockRejectedValueOnce(refusal(moved, gaps))
      .mockResolvedValue(agent([model('alpha')]));
    const { onClose } = renderDialog({ focus: { modelId: 'beta', action: 'remove' } });
    const confirm = await screen.findByRole('dialog', { name: 'Remove beta?' });
    await user.click(within(confirm).getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(onClose).toHaveBeenCalledWith({ removed: true }));
    expect(screen.queryByRole('dialog', { name: 'Save the model list?' })).toBeNull();
    expect(write).toHaveBeenCalledTimes(3);
    expect(write.mock.calls[0][1].force).toBeUndefined();
    expect(write.mock.calls[1][1]).toMatchObject({ force: true, would_remove_hops: shown, would_interrupt: [] });
    expect(write.mock.calls[2][1]).toMatchObject({ force: true, would_remove_hops: moved, would_interrupt: gaps });
  });

  it('keeps a confirmed focused removal on screen until its forced resend lands', async () => {
    const user = userEvent.setup();
    const hops = [{ backend: 'claude' as const, menu_model: 'beta', source_id: 'src_a', model_id: 'beta-air', position: 1 }];
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([model('alpha'), model('beta')], {
      routes: { beta: { hops: [{ source_id: 'src_a', model_id: 'beta-air' }] } },
    }));
    let settle: (value: ReturnType<typeof agent>) => void = () => {};
    const write = vi.spyOn(modelsApi, 'putAgentModels')
      .mockRejectedValueOnce(new ApiCallError(
        'backend_model_in_route', 'modelHub.errors.backend_model_in_route', true, [], [], hops, 409,
      ))
      .mockReturnValueOnce(new Promise((resolve) => { settle = resolve; }));
    const { onClose } = renderDialog({ focus: { modelId: 'beta', action: 'remove' } });
    const confirm = await screen.findByRole('dialog', { name: 'Remove beta?' });
    await user.click(within(confirm).getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(write).toHaveBeenCalledTimes(2));
    // The forced resend is still out: the question that started it is the only
    // surface this focused save has, so it stays, inert, until the answer lands.
    expect(screen.getByRole('dialog', { name: 'Remove beta?' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Remove' })).toHaveProperty('disabled', true);
    await act(async () => { settle(agent([model('alpha')])); });
    await waitFor(() => expect(onClose).toHaveBeenCalledWith({ removed: true }));
  });

  it('keeps a focused editor on screen and inert while its save is pending', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([model('alpha')]));
    let settle: (value: ReturnType<typeof agent>) => void = () => {};
    vi.spyOn(modelsApi, 'putAgentModels').mockReturnValue(new Promise((resolve) => { settle = resolve; }));
    const { onClose } = renderDialog({ focus: { modelId: 'alpha', action: 'edit' } });
    await user.click(await screen.findByRole('button', { name: 'Save model' }));
    expect(screen.getByRole('button', { name: 'Save model' })).toHaveProperty('disabled', true);
    expect(onClose).not.toHaveBeenCalled();
    await act(async () => { settle(agent([model('alpha')])); });
    await waitFor(() => expect(onClose).toHaveBeenCalledWith(undefined));
  });

  it('cancels a focused removal without writing', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([model('alpha'), model('beta')]));
    const write = vi.spyOn(modelsApi, 'putAgentModels');
    const { onClose } = renderDialog({ focus: { modelId: 'beta', action: 'remove' } });
    const confirm = await screen.findByRole('dialog', { name: 'Remove beta?' });
    await user.click(within(confirm).getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onClose.mock.calls[0][0]?.removed).toBeFalsy();
    expect(write).not.toHaveBeenCalled();
  });

  it('renders Claude models without inventing a native default choice', async () => {
    await i18n.changeLanguage('zh');
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([
      model('claude-fixture', { display_name: 'Fixture model' }),
    ]));
    renderDialog();

    expect(await screen.findByText('Fixture model')).toBeTruthy();
    expect(screen.queryByText('Claude Code 默认模型')).toBeNull();
    expect(screen.queryByText('Default')).toBeNull();
    expect(screen.getByLabelText('搜索名称或模型 ID')).toBeTruthy();
  });

  it.each([
    ['en', { corner: 'Close', footer: 'Cancel' }],
    ['zh', { corner: '关闭', footer: '取消' }],
  ] as const)('names the corner and footer exits apart in %s', async (language, exit) => {
    await i18n.changeLanguage(language);
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([model('alpha')]));
    const user = userEvent.setup();
    const { onClose } = renderDialog();

    // The model id is the one string here no locale rewrites.
    await screen.findByText('alpha');

    // Both ways out leave without saving, which is why they were once given the
    // same word — and why that word named neither. `getByRole` is singular, so
    // each line below also asserts the name it asks for reaches nothing else in
    // the dialog; the pair then has to be two controls rather than one found
    // twice. A screen reader is under the same constraint: one word announced
    // for the corner and the footer alike describes the dialog as having a
    // single exit, and offers no way to say which one is being read.
    const dialog = within(screen.getByRole('dialog'));
    const exits = [
      dialog.getByRole('button', { name: exit.corner }),
      dialog.getByRole('button', { name: exit.footer }),
    ];
    expect(new Set(exits).size).toBe(exits.length);

    for (const control of exits) await user.click(control);
    expect(onClose).toHaveBeenCalledTimes(exits.length);
  });

  it('shows the catalog and nothing else — no source, route, fallback or mapping control', async () => {
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([model('alpha')], {
      routes: { alpha: { hops: [{ source_id: 'src_a', model_id: 'alpha' }] } },
    }));
    renderDialog();

    await screen.findByRole('button', { name: 'Reorder alpha' });
    expect(screen.queryByText(/source/i)).toBeNull();
    expect(screen.queryByText(/route/i)).toBeNull();
    expect(screen.queryByText(/fallback/i)).toBeNull();
    expect(screen.queryByText(/priority/i)).toBeNull();
    expect(screen.queryByText('src_a')).toBeNull();
  });

  it('MH-MENU-COMPOSE-001: adds what the providers offer, and promises the suppliers it displayed', async () => {
    const user = userEvent.setup();
    const catalog = [locked, model('alpha')];
    const glm = candidate('glm-5.2', {
      display_name: 'GLM 5.2',
      reasoning_efforts: ['low'],
      suppliers: [{ source_id: 'src_a', source_name: 'Primary relay', model_id: 'glm-5.2-air' }],
    });
    const echoed = agent([...catalog, model('glm-5.2')]);
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(catalog));
    vi.spyOn(modelsApi, 'getAgentModelCandidates').mockResolvedValue(offered({ providers: [glm] }));
    const putAgentModels = vi.spyOn(modelsApi, 'putAgentModels').mockResolvedValue(echoed);
    const { onSaved, onClose } = renderDialog();

    await user.click(await enabledAddModels());
    await user.click(await screen.findByRole('checkbox', { name: /GLM 5\.2/ }));
    await user.click(screen.getByRole('button', { name: 'Add 1 model' }));

    // Nothing is written until the list itself is saved.
    expect(putAgentModels).not.toHaveBeenCalled();
    expect(await screen.findByText('3 models')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(putAgentModels).toHaveBeenCalledWith('claude', {
      baseline: catalog,
      models: [...catalog, candidateBackendModel(glm)],
      // Per addition, the projection the picker displayed for it. The server
      // matches the addition itself, so this is what makes the seeded route the
      // one the user agreed to rather than whatever supply exists at commit
      // time. The rows the baseline already holds are matched by nobody, so
      // promising anything about them would describe nothing this write does.
      expected_suppliers: { 'glm-5.2': [{ source_id: 'src_a', model_id: 'glm-5.2-air' }] },
    }));
    expect(onSaved).toHaveBeenCalledWith(echoed);
    expect(onClose).toHaveBeenCalled();
  });

  it('hands a model nobody offers to the editor, and promises nothing about it', async () => {
    const user = userEvent.setup();
    const catalog = [locked, model('alpha')];
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(catalog));
    vi.spyOn(modelsApi, 'getAgentModelCandidates').mockResolvedValue(offered());
    vi.spyOn(modelsApi, 'searchModelsDev').mockResolvedValue([]);
    const putAgentModels = vi.spyOn(modelsApi, 'putAgentModels').mockResolvedValue(agent([...catalog, model('added')]));
    renderDialog();

    await user.click(await enabledAddModels());
    await user.click(await screen.findByRole('button', { name: 'Add custom model…' }));
    await user.type(screen.getByLabelText('Model'), 'added');
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    expect(await screen.findByText('3 models')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Save' }));

    // A row written by hand was never shown a supplier, so the write states no
    // expectation for it: there is nothing for the server to disagree with.
    await waitFor(() => expect(putAgentModels).toHaveBeenCalledWith('claude', {
      baseline: catalog,
      models: [...catalog, model('added')],
    }));
  });

  it('asks which row a typed ID names as the ID it would be saved as', async () => {
    const user = userEvent.setup();
    const FOO = model('foo', {
      native_protocol: 'openai_responses',
      display_name: 'Foo Air',
      context_window: 180000,
      origin: 'manual',
    });
    const catalog = [model('glm-4.7', { native_protocol: 'openai_responses' }), FOO];
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(catalog, {
      backend: 'opencode',
    }));
    // Nothing offered under this query, which is the only state that offers the
    // typed id as a custom model at all.
    vi.spyOn(modelsApi, 'getAgentModelCandidates').mockResolvedValue(offered());
    vi.spyOn(modelsApi, 'searchModelsDev').mockResolvedValue([]);
    const write = vi.spyOn(modelsApi, 'putAgentModels').mockResolvedValue(agent(catalog));
    renderDialog({ backend: 'opencode' });

    await user.click(await screen.findByRole('button', { name: 'Remove Foo Air' }));
    await user.click(screen.getByRole('button', { name: 'Add models' }));
    await waitFor(() => expect((screen.getByLabelText('Search models or providers') as HTMLInputElement).disabled).toBe(false));
    await user.type(await screen.findByLabelText('Search models or providers'), 'foo');
    await user.click(await screen.findByRole('button', { name: 'Add "foo" as a custom model…' }));

    // Their own row, opened as itself: the id fixed, and the description they
    // wrote still in it. The button is the tell — this is an edit, not an add.
    expect((await screen.findByLabelText('Model') as HTMLInputElement).value).toBe('foo');
    expect((screen.getByLabelText('Display name') as HTMLInputElement).value).toBe('Foo Air');
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('180,000');
    await user.type(screen.getByLabelText('Maximum output'), '8000');
    await user.click(screen.getByRole('button', { name: 'Save model' }));
    await user.click(await screen.findByRole('button', { name: 'Save' }));

    // And the row goes back whole, changed only where they changed it. A blank
    // row carrying the same id would have saved this list with the name and the
    // window gone — the write is what the user would have to undo by hand.
    await waitFor(() => expect(write).toHaveBeenCalledWith('opencode', {
      baseline: catalog,
      models: [model('glm-4.7', { native_protocol: 'openai_responses' }), { ...FOO, max_output_tokens: 8000 }],
    }));
  });

  it('keeps the recorded origin of a row re-added under an ID the editor only lands on at the end', async () => {
    const user = userEvent.setup();
    // The row the server already stores, and the creation path it recorded.
    const GROK = model('grok-4.6', {
      origin: 'provider',
      display_name: 'Grok 4.6',
      native_protocol: 'openai_responses',
    });
    const catalog = [model('glm-4.7', { native_protocol: 'openai_responses' }), GROK];
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(catalog, { backend: 'opencode' }));
    vi.spyOn(modelsApi, 'getAgentModelCandidates').mockResolvedValue(offered());
    // models.dev publishes this model under exactly the id the list already
    // holds, which is the whole point: the typed query does not name the saved
    // row, and the chosen suggestion does.
    vi.spyOn(modelsApi, 'searchModelsDev').mockResolvedValue([{
      provider_id: 'xai',
      provider_name: 'xAI',
      model_id: 'grok-4.6',
      models_dev_id: 'xai/grok-4.6',
      display_name: 'Grok 4.6',
      context_window: 256000,
      max_output_tokens: 64000,
      input_modalities: ['text'],
      output_modalities: ['text'],
      supports_tools: true,
      supports_reasoning: true,
      reasoning_efforts: ['low', 'high'],
      native_protocol: 'openai_responses',
    } satisfies ModelsDevMatch]);
    const write = vi.spyOn(modelsApi, 'putAgentModels').mockResolvedValue(agent(catalog));
    renderDialog({ backend: 'opencode' });

    await user.click(await screen.findByRole('button', { name: 'Remove Grok 4.6' }));
    await user.click(screen.getByRole('button', { name: 'Add models' }));
    await waitFor(() => expect((screen.getByLabelText('Search models or providers') as HTMLInputElement).disabled).toBe(false));
    // A query that is not the saved id, so nothing resolves it to the saved row
    // on the way in — the editor opens in add mode and stamps `models_dev`.
    await user.type(await screen.findByLabelText('Search models or providers'), 'grok 4.6');
    await user.click(await screen.findByRole('button', { name: 'Add "grok 4.6" as a custom model…' }));
    await user.click(await screen.findByRole('option', { name: /Grok 4\.6/ }));
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    await user.click(await screen.findByRole('button', { name: 'Save' }));

    // `origin` is how the row was FIRST created and the server holds it
    // immutable, so the only list this can be saved as is one that still says
    // `provider`. Sending `models_dev` is refused outright, and the user is
    // left with a row they cannot re-add by hand at all.
    await waitFor(() => expect(write).toHaveBeenCalled());
    const [, body] = write.mock.calls[0];
    expect(body.models.find((entry) => entry.id === 'grok-4.6')?.origin).toBe('provider');
    // And the fill itself still landed: inheriting the origin is not reverting
    // the row to the one that was removed.
    expect(body.models.find((entry) => entry.id === 'grok-4.6')?.context_window).toBe(256000);
  });

  it('edits an existing row without renaming it', async () => {
    const user = userEvent.setup();
    const catalog = [model('alpha', { context_window: 1000 })];
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(catalog));
    const putAgentModels = vi.spyOn(modelsApi, 'putAgentModels').mockResolvedValue(agent(catalog));
    renderDialog();

    await user.click(await screen.findByRole('button', { name: 'Edit alpha' }));
    expect((screen.getByLabelText('Model') as HTMLInputElement).readOnly).toBe(true);
    await user.clear(screen.getByLabelText('Context window'));
    await user.type(screen.getByLabelText('Context window'), '2000');
    await user.click(screen.getByRole('button', { name: 'Save model' }));
    await user.click(await screen.findByRole('button', { name: 'Save' }));

    await waitFor(() => expect(putAgentModels).toHaveBeenCalledWith('claude', {
      baseline: catalog,
      models: [model('alpha', { context_window: 2000 })],
    }));
  });

  it('removes rows on the click, asks once on save in its own dialog, then echoes the guard byte for byte', async () => {
    const user = userEvent.setup();
    const catalog = [model('alpha'), model('beta'), model('gamma')];
    /**
     * The server's plan, in the server's order: it walks the baseline it was
     * sent, so `beta` comes before `gamma` no matter which the user removed
     * first. What the dialog shows and what the forced save carries are these
     * same bytes. The route on `src_gone` is one the baseline never showed —
     * the server's plan is the whole account, not the dialog's projection.
     */
    const hops = [
      { backend: 'claude' as const, menu_model: 'beta', source_id: 'src_a', model_id: 'beta-air', position: 1 },
      { backend: 'claude' as const, menu_model: 'beta', source_id: 'src_gone', model_id: 'beta-backup', position: 2 },
      { backend: 'claude' as const, menu_model: 'gamma', source_id: 'src_a', model_id: 'gamma-air', position: 1 },
    ];
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(catalog, {
      routes: { beta: { hops: [{ source_id: 'src_a', model_id: 'beta-air' }] } },
    }));
    const write = vi.spyOn(modelsApi, 'putAgentModels')
      .mockRejectedValue(new ApiCallError(
        'backend_model_in_route',
        'modelHub.errors.backend_model_in_route',
        true,
        [],
        [],
        hops,
        409,
      ));
    const { onClose } = renderDialog();

    // The trash removes the row, routed or not: nothing is asked in the list.
    for (const menuModel of ['gamma', 'beta']) {
      await user.click(await screen.findByRole('button', { name: `Remove ${menuModel}` }));
    }
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText('1 model')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'Save' }));
    const guard = await screen.findByRole('dialog', { name: 'Save the model list?' });
    expect(guard.textContent).toContain('3 hops');
    for (const hop of hops) {
      expect(within(guard).getByText(`${hop.model_id} · Order #${hop.position}`)).toBeTruthy();
    }
    // The removed rows stay removed behind the question: nothing re-inserts them.
    expect(screen.queryByText('beta')).toBeNull();
    expect(screen.queryByText('gamma')).toBeNull();

    // Cancel returns to the draft exactly as the user left it. (The header's
    // close button carries the same name and takes the same path.)
    await user.click(within(guard).getAllByRole('button', { name: 'Cancel' }).at(-1)!);
    expect(screen.queryByRole('dialog', { name: 'Save the model list?' })).toBeNull();
    expect(screen.getByText('1 model')).toBeTruthy();
    expect(write).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();

    write.mockReset();
    write
      .mockRejectedValueOnce(new ApiCallError(
        'backend_model_in_route', 'modelHub.errors.backend_model_in_route', true, [], [], hops, 409,
      ))
      .mockResolvedValue(agent([model('alpha')]));
    await user.click(screen.getByRole('button', { name: 'Save' }));
    const again = await screen.findByRole('dialog', { name: 'Save the model list?' });
    await user.click(within(again).getByRole('button', { name: 'Remove anyway' }));
    await waitFor(() => expect(onClose).toHaveBeenCalledWith(undefined));
    expect(write).toHaveBeenCalledTimes(2);
    const [first, second] = write.mock.calls.map(([, body]) => body as Record<string, unknown>);
    // The first attempt claims no agreement: only the server states what a
    // write would take with it.
    expect(Object.keys(first)).toEqual(['baseline', 'models']);
    // The confirmed one is the refusal itself, byte for byte and in the
    // server's order, which is not the order the rows were removed in.
    expect(second.force).toBe(true);
    expect(JSON.stringify(second.would_remove_hops)).toBe(JSON.stringify(hops));
    expect(JSON.stringify(second.would_interrupt)).toBe(JSON.stringify([]));
  });

  it('keeps the confirmed guard mounted and busy until the forced save lands', async () => {
    const user = userEvent.setup();
    const hops = [{ backend: 'claude' as const, menu_model: 'beta', source_id: 'src_a', model_id: 'beta-air', position: 1 }];
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([model('alpha'), model('beta')]));
    let settle: (value: ReturnType<typeof agent>) => void = () => {};
    vi.spyOn(modelsApi, 'putAgentModels')
      .mockRejectedValueOnce(new ApiCallError(
        'backend_model_in_route', 'modelHub.errors.backend_model_in_route', true, [], [], hops, 409,
      ))
      .mockReturnValueOnce(new Promise((resolve) => { settle = resolve; }));
    let pending = false;
    const { onClose } = renderDialog({
      catalogWrite: {
        get pending() { return pending; },
        track: async (work) => { pending = true; try { await work(); } finally { pending = false; } },
      },
    });
    await user.click(await screen.findByRole('button', { name: 'Remove beta' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));
    const guard = await screen.findByRole('dialog', { name: 'Save the model list?' });
    await user.click(within(guard).getByRole('button', { name: 'Remove anyway' }));
    expect(screen.getByRole('dialog', { name: 'Save the model list?' })).toBeTruthy();
    await act(async () => { settle(agent([model('alpha')])); });
    await waitFor(() => expect(onClose).toHaveBeenCalledWith(undefined));
  });

  it('ends a confirmed save whose plan never settles as a failure, not a second question', async () => {
    const user = userEvent.setup();
    const hops = (position: number) => [
      { backend: 'claude' as const, menu_model: 'beta', source_id: 'src_a', model_id: 'beta-air', position },
    ];
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([model('alpha'), model('beta')]));
    let refusals = 0;
    const write = vi.spyOn(modelsApi, 'putAgentModels').mockImplementation(async () => {
      refusals += 1;
      throw new ApiCallError(
        'backend_model_in_route', 'modelHub.errors.backend_model_in_route', true, [], [], hops(refusals), 409,
      );
    });
    const { onClose } = renderDialog();
    await user.click(await screen.findByRole('button', { name: 'Remove beta' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));
    const guard = await screen.findByRole('dialog', { name: 'Save the model list?' });
    await user.click(within(guard).getByRole('button', { name: 'Remove anyway' }));
    expect(await screen.findByText(i18n.t('settings.models.gateway.catalog.saveRouted'))).toBeTruthy();
    // One question, then the initial write plus the bounded resends: the plan
    // that kept moving is reported, not asked about a second time.
    expect(screen.queryByRole('dialog', { name: 'Save the model list?' })).toBeNull();
    expect(write).toHaveBeenCalledTimes(4);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('re-asks with the current suppliers when the ones it displayed went stale', async () => {
    const user = userEvent.setup();
    const catalog = [model('alpha')];
    const shown = candidate('glm-5.2', {
      display_name: 'GLM 5.2',
      suppliers: [{ source_id: 'src_a', source_name: 'Primary relay', model_id: 'glm-5.2-air' }],
    });
    const current = candidate('glm-5.2', {
      display_name: 'GLM 5.2',
      suppliers: [{ source_id: 'src_b', source_name: 'Backup relay', model_id: 'glm-5.2' }],
    });
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(catalog));
    const read = vi.spyOn(modelsApi, 'getAgentModelCandidates')
      .mockResolvedValueOnce(offered({ providers: [shown] }))
      .mockResolvedValue(offered({ providers: [current] }));
    const write = vi.spyOn(modelsApi, 'putAgentModels')
      .mockRejectedValueOnce(staleCandidates({ 'glm-5.2': [{ source_id: 'src_b', model_id: 'glm-5.2' }] }))
      .mockResolvedValue(agent([...catalog, model('glm-5.2')]));
    const { onSaved, onObserved } = renderDialog();

    await user.click(await enabledAddModels());
    await user.click(await screen.findByRole('checkbox', { name: /Primary relay/ }));
    await user.click(screen.getByRole('button', { name: 'Add 1 model' }));
    await user.click(await screen.findByRole('button', { name: 'Save' }));

    // Nothing was committed, so there is nothing to report and no list to
    // re-read: the answer to this refusal is the same question again, asked
    // against the suppliers the server matched this time.
    //
    // The candidates are read twice for that, and both reads have a job. The
    // refusal is reconciled against one — whether the id is still offered is
    // the only thing that withdraws it, and an answer that withdrew every pick
    // would open no picker to read it. The question then takes its own, because
    // the chips it shows are the promise its confirmation sends, and a picker
    // rendering a read it did not take would display one and send the other.
    expect(await screen.findByRole('checkbox', { name: /Backup relay/ })).toBeTruthy();
    expect(read).toHaveBeenCalledTimes(3);
    expect(onObserved).not.toHaveBeenCalled();
    expect(screen.queryByRole('status')).toBeNull();

    // Still picked, because it is still what the user asked for — only the
    // supply behind it changed.
    await user.click(screen.getByRole('button', { name: 'Add 1 model' }));
    await user.click(await screen.findByRole('button', { name: 'Save' }));

    await waitFor(() => expect(write).toHaveBeenLastCalledWith('claude', expect.objectContaining({
      expected_suppliers: { 'glm-5.2': [{ source_id: 'src_b', model_id: 'glm-5.2' }] },
    })));
    expect(onSaved).toHaveBeenCalled();
  });

  it('offers no way in to a role that may not read Sources', async () => {
    // The candidates read names Sources, and the editor is reached through the
    // picker, so the whole add path goes where the page's other Source-reading
    // surfaces go. What is left is still a complete surface: the list, its
    // order, its edits and its removals.
    const read = vi.spyOn(modelsApi, 'getAgentModelCandidates');
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([model('alpha')]));
    renderDialog({ canReadSources: false });

    expect(await screen.findByRole('button', { name: 'Reorder alpha' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Add models' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Edit alpha' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Remove alpha' })).toBeTruthy();
    expect(read).not.toHaveBeenCalled();
  });

  it('reorders with the keyboard, announces the move, and leaves the locked row where it is', async () => {
    const user = userEvent.setup();
    const catalog = [locked, model('alpha'), model('beta')];
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(catalog));
    const putAgentModels = vi.spyOn(modelsApi, 'putAgentModels').mockResolvedValue(agent(catalog));
    renderDialog();

    const grip = await screen.findByRole('button', { name: 'Reorder alpha' });
    grip.focus();
    await user.keyboard('[Space]');
    expect(screen.getByText('Grabbed alpha, position 1 of 2.')).toBeTruthy();

    await user.keyboard('[ArrowDown]');
    expect(screen.getByText('Moved alpha to position 2 of 2.')).toBeTruthy();
    await user.keyboard('[Space]');
    expect(screen.getByText('Dropped alpha at position 2 of 2.')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(putAgentModels).toHaveBeenCalledWith('claude', {
      baseline: catalog,
      models: [locked, model('beta'), model('alpha')],
    }));
  });

  it('restores the pre-grab order when the move is cancelled', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([model('alpha'), model('beta')]));
    renderDialog();

    const grip = await screen.findByRole('button', { name: 'Reorder alpha' });
    grip.focus();
    await user.keyboard('[Space][ArrowDown][Escape]');

    expect(screen.getByText('Cancelled moving alpha and restored its original position.')).toBeTruthy();
    const rows = screen.getAllByRole('button', { name: /^Reorder / });
    expect(rows.map((row) => row.getAttribute('aria-label'))).toEqual(['Reorder alpha', 'Reorder beta']);
    expect(screen.getByRole('button', { name: 'Save' }).hasAttribute('disabled')).toBe(true);
  });

  it('filters by ID or display name and suspends dragging while filtered', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([
      model('alpha', { display_name: 'Bright Alpha' }),
      model('beta'),
    ]));
    renderDialog();

    await waitFor(() => expect((screen.getByLabelText('Search name or model ID') as HTMLInputElement).disabled).toBe(false));
    await user.type(screen.getByLabelText('Search name or model ID'), 'bright');
    expect(screen.queryByRole('button', { name: 'Reorder beta' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Reorder Bright Alpha' }).hasAttribute('disabled')).toBe(true);

    await user.clear(screen.getByLabelText('Search name or model ID'));
    await user.type(screen.getByLabelText('Search name or model ID'), 'nothing');
    expect(screen.getByText('No model matches this search')).toBeTruthy();
  });

  it('falls back to a read-only list when the server predates the catalog', async () => {
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(undefined));
    const putAgentModels = vi.spyOn(modelsApi, 'putAgentModels');
    renderDialog();

    expect(await screen.findByText('legacy-a')).toBeTruthy();
    expect(screen.getByText('legacy-b')).toBeTruthy();
    expect(screen.getByText('This model engine build does not offer an editable model list yet. These are the models it currently exposes.')).toBeTruthy();
    expect(screen.getByText('2 models')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Add models' }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('button', { name: 'Save' }).hasAttribute('disabled')).toBe(true);
    expect(screen.queryByRole('button', { name: /^Reorder / })).toBeNull();
    expect(putAgentModels).not.toHaveBeenCalled();
  });

  it('offers a retry when the catalog cannot be read', async () => {
    const user = userEvent.setup();
    const read = vi.spyOn(modelsApi, 'getAgentSources')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(agent([model('alpha')]));
    renderDialog();

    expect(await screen.findByText("This backend's model list could not be read.")).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByRole('button', { name: 'Reorder alpha' })).toBeTruthy();
    expect(read).toHaveBeenCalledTimes(2);
  });

  it('keeps the draft, re-reads, and reports a save that did not land', async () => {
    const user = userEvent.setup();
    const catalog = [model('alpha'), model('beta')];
    const read = vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(catalog));
    vi.spyOn(modelsApi, 'putAgentModels').mockRejectedValue(
      new ApiCallError('invalid_request', 'modelHub.errors.backend_model_conflict', true),
    );
    const { onSaved, onObserved, onClose } = renderDialog();

    await user.click(await screen.findByRole('button', { name: 'Remove beta' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    // The server named a cause the user can act on, so they read that cause —
    // never the raw key the server sent.
    expect(await screen.findByText('This list changed elsewhere while you were editing. Your changes were replayed onto the newer list; check it and save again.')).toBeTruthy();
    expect(screen.queryByText('modelHub.errors.backend_model_conflict')).toBeNull();
    expect(read).toHaveBeenCalledTimes(2);
    expect(onObserved).toHaveBeenCalledTimes(1);
    expect(onSaved).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    // The removal the user asked for survives the rebase onto the re-read list.
    expect(screen.getByText('1 model')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Reorder beta' })).toBeNull();
  });

  it('names the route that refused a removal the client could not see coming', async () => {
    const user = userEvent.setup();
    // The dialog blocks a removal whose route it knows about. This one was routed
    // elsewhere after the read, so only the server can refuse it.
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([model('alpha'), model('beta')]));
    vi.spyOn(modelsApi, 'putAgentModels').mockRejectedValue(
      new ApiCallError('invalid_request', 'modelHub.errors.backend_model_in_route', true),
    );
    renderDialog();

    await user.click(await screen.findByRole('button', { name: 'Remove beta' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText('A model you removed still has a route. The list was re-read from the server; clear the route first, then remove it.')).toBeTruthy();
  });

  /**
   * The refusals that commit nothing, and the one property they share.
   *
   * Both are the server saying 「not on this plan」: the write did not land, so
   * the draft is still the user's and exactly one agreement inside it is out of
   * date. The failure mode both shipped with was answering that with an edit —
   * dropping the row the picker had asked about, rebasing the removal in as
   * though it had been agreed — which spends the user's work to settle a
   * question about the server's, and leaves the same refusal waiting on the next
   * save. So the members are the fixture: a refusal that commits nothing is
   * added to this list and inherits the whole property, or it is not added and
   * this file is where that shows.
   */
  describe('a refusal that committed nothing', () => {
    const ALPHA = model('alpha', { context_window: 1000 });
    /** A hand edit no refusal is about, so it has to survive every one of them. */
    const EDITED = model('alpha', { context_window: 2000 });
    const CATALOG = [ALPHA, model('beta')];

    const shown = candidate('glm-5.2', {
      display_name: 'GLM 5.2',
      suppliers: [{ source_id: 'src_a', source_name: 'Primary relay', model_id: 'glm-5.2-air' }],
    });
    const current = candidate('glm-5.2', {
      display_name: 'GLM 5.2',
      suppliers: [{ source_id: 'src_b', source_name: 'Backup relay', model_id: 'glm-5.2' }],
    });

    /** Widen one row's context window by hand, from the row's own editor. */
    const widen = async (user: ReturnType<typeof userEvent.setup>, row: string) => {
      await user.click(await screen.findByRole('button', { name: `Edit ${row}` }));
      await user.clear(screen.getByLabelText('Context window'));
      await user.type(screen.getByLabelText('Context window'), '2000');
      await user.click(screen.getByRole('button', { name: 'Save model' }));
    };

    type Member = {
      what: string;
      /** What the server serves. One answer, not a sequence: a refusal that
       *  commits nothing has nothing to re-read, and the runner holds every
       *  member to that — so a member needing a second answer here is a member
       *  that has left this property. */
      read: AgentSupply;
      /** The draft the user builds, hand edit included — placed where this
       *  refusal could spend it, which is the whole question. */
      arrange: (user: ReturnType<typeof userEvent.setup>) => Promise<void>;
      refusal: ApiCallError;
      /** The same question, asked again with what the server actually holds. */
      reasked: () => Promise<unknown>;
      /** The user's answer, through to the save it sends — and only one: a
       *  confirmation that needed another Save press would be a second ask. */
      answer: (user: ReturnType<typeof userEvent.setup>) => Promise<void>;
      /** Exactly the list the answered save sends. Exact, not 「contains」: a row
       *  rebuilt from its candidate keeps the id and loses the edit. */
      models: BackendModel[];
      /** The fields that save adds, beyond the list itself. */
      agreement: Record<string, unknown>;
    };

    /**
     * The plan the server holds.
     *
     * Written once, because it is the fixture for both halves of the property:
     * what the guard dialog shows and what the answered save echoes are the
     * same two arrays. It strands a model on purpose — a refusal that
     * interrupts something is still a question the user may answer, so the ask
     * must carry the interruption rather than be withheld because of it.
     */
    const GUARDED = {
      hops: [{ backend: 'claude' as const, menu_model: 'beta', source_id: 'src_a', model_id: 'beta-air', position: 1 }],
      gaps: [{ backend: 'claude' as const, model_id: 'beta', agents: ['main'] }],
    };

    const MEMBERS: readonly Member[] = [
      {
        what: 'the suppliers the picker displayed went stale',
        read: agent(CATALOG),
        arrange: async (user) => {
          vi.spyOn(modelsApi, 'getAgentModelCandidates')
            .mockResolvedValueOnce(offered({ providers: [shown] }))
            .mockResolvedValue(offered({ providers: [current] }));
          await user.click(await enabledAddModels());
          await user.click(await screen.findByRole('checkbox', { name: /Primary relay/ }));
          await user.click(screen.getByRole('button', { name: 'Add 1 model' }));
          // On the added row itself: the refusal is about its suppliers, and
          // this is the work a rebuild from its candidate would cost.
          await widen(user, 'GLM 5.2');
        },
        refusal: staleCandidates({ 'glm-5.2': [{ source_id: 'src_b', model_id: 'glm-5.2' }] }),
        reasked: async () => {
          const row = await screen.findByRole('checkbox', { name: /Backup relay/ });
          // Offered back as pickable, not filed under 「Already in the list」:
          // the row is in the draft now, so the group that would claim it is
          // search-only and the user would be asked a question with nothing on
          // screen to answer it with.
          expect((row as HTMLInputElement).disabled).toBe(false);
          return row;
        },
        answer: async (user) => {
          await user.click(screen.getByRole('button', { name: 'Add 1 model' }));
          await user.click(await screen.findByRole('button', { name: 'Save' }));
        },
        models: [ALPHA, model('beta'), { ...candidateBackendModel(shown), context_window: 2000 }],
        agreement: { expected_suppliers: { 'glm-5.2': [{ source_id: 'src_b', model_id: 'glm-5.2' }] } },
      },
      {
        what: 'the route guard refused the removal',
        // The list is not read again: the refusal answers the exact write that
        // was sent, so the draft stays on the baseline it was built from.
        read: agent(CATALOG),
        arrange: async (user) => {
          // A removal cannot carry an edit, so the work at stake is the rest of
          // the draft, which the question must leave exactly as it was.
          await widen(user, 'alpha');
          await user.click(await screen.findByRole('button', { name: 'Remove beta' }));
          expect(screen.queryByRole('alert')).toBeNull();
        },
        refusal: new ApiCallError(
          'backend_model_in_route',
          'modelHub.errors.backend_model_in_route',
          true,
          GUARDED.gaps,
          [],
          GUARDED.hops,
          409,
        ),
        // Asked once, in its own dialog, with the whole plan the server named —
        // the same evidence body every guarded Model Hub write shows.
        reasked: async () => {
          const asked = await screen.findByRole('dialog', { name: 'Save the model list?' });
          for (const hop of GUARDED.hops) {
            expect(within(asked).getByText(`${hop.model_id} · Order #${hop.position}`)).toBeTruthy();
          }
          expect(within(asked).getByText('Models that will be left with no source')).toBeTruthy();
          for (const gap of GUARDED.gaps) {
            expect(within(asked).getByText(`Agents pinned to it: ${gap.agents.join(', ')}`)).toBeTruthy();
          }
          expect(asked.textContent).toContain('Some models will be left with no usable source.');
          return asked;
        },
        answer: async (user) => {
          await user.click(screen.getByRole('button', { name: 'Remove anyway' }));
        },
        models: [EDITED],
        // Both arrays, because both were shown: an echo of one would claim a
        // confirmation for half of what the user answered.
        agreement: { force: true, would_remove_hops: GUARDED.hops, would_interrupt: GUARDED.gaps },
      },
    ];

    for (const member of MEMBERS) {
      it(`asks again and keeps the draft when ${member.what}`, async () => {
        const user = userEvent.setup();
        const read = vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(member.read);
        const write = vi.spyOn(modelsApi, 'putAgentModels')
          .mockRejectedValueOnce(member.refusal)
          .mockResolvedValue(agent([EDITED]));
        const { onSaved, onClose } = renderDialog();

        await member.arrange(user);
        await user.click(await screen.findByRole('button', { name: 'Save' }));

        // The question again — never a sentence about a save that never
        // happened, and never a closed dialog.
        expect(await member.reasked()).toBeTruthy();
        expect(screen.queryByRole('status')).toBeNull();
        expect(onSaved).not.toHaveBeenCalled();
        expect(onClose).not.toHaveBeenCalled();
        // And the list is never read a second time: a refusal commits nothing,
        // so the draft still stands on the baseline it was built from, and the
        // agreement below still answers the write that was actually sent.
        expect(read).toHaveBeenCalledTimes(1);

        await member.answer(user);

        // One save per answer, carrying the draft the user built and the
        // agreement the server itself stated. Asserted on the last call because
        // the first is the refused one: an agreement granted without being
        // asked for would have gone out there.
        await waitFor(() => expect(write).toHaveBeenLastCalledWith('claude', expect.objectContaining({
          models: member.models,
          ...member.agreement,
        })));
        expect(write).toHaveBeenCalledTimes(2);
      });
    }

    it('withdraws a candidate the refreshed offer no longer holds, and re-sends the save it was already owed', async () => {
      const user = userEvent.setup();
      // Withdrawal has one source of evidence, and this is it: the refusal still
      // names suppliers for the picked id, and the refreshed offer does not hold
      // the id at all. So the answer is not another question — a row left in the
      // draft with no agreement behind it would go out on the next save as
      // though it had been typed by hand, which is the silent re-send the re-ask
      // exists to prevent.
      vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(CATALOG));
      const read = vi.spyOn(modelsApi, 'getAgentModelCandidates')
        .mockResolvedValueOnce(offered({ providers: [shown] }))
        .mockResolvedValue(offered());
      const write = vi.spyOn(modelsApi, 'putAgentModels')
        .mockRejectedValueOnce(staleCandidates({ 'glm-5.2': [{ source_id: 'src_b', model_id: 'glm-5.2' }] }))
        .mockResolvedValue(agent([EDITED, model('beta')]));
      const { onSaved, onClose } = renderDialog();

      await user.click(await enabledAddModels());
      await user.click(await screen.findByRole('checkbox', { name: /Primary relay/ }));
      await user.click(screen.getByRole('button', { name: 'Add 1 model' }));
      await widen(user, 'alpha');
      await user.click(screen.getByRole('button', { name: 'Save' }));

      // The row leaves, and it leaves without a question: nothing reopens for a
      // candidate the server has stopped offering. One read settled it — the
      // reconciliation takes its own, because a withdrawal that opens no picker
      // would otherwise never be read at all.
      await waitFor(() => expect(screen.queryByText('GLM 5.2')).toBeNull());
      expect(screen.queryByRole('heading', { name: 'Add Claude Code models' })).toBeNull();
      expect(read).toHaveBeenCalledTimes(2);
      // Nor a sentence about it: the row that disappeared and the count that fell
      // are the report, and a failure line here would be the dialog telling the
      // user about its own edit.
      expect(screen.queryByRole('status')).toBeNull();

      // And the save the user pressed is still owed — nothing was committed — so
      // it goes again on the reduced list rather than charging them a second
      // press for a withdrawal they did not make.
      await waitFor(() => expect(write).toHaveBeenCalledTimes(2));
      // The rest of the draft is still the user's, and the write claims nothing
      // beyond the list itself — an `expected_suppliers` entry would be an
      // agreement about a candidate that no longer exists, and there is no other
      // field this save was granted.
      const body = write.mock.lastCall?.[1] as Record<string, unknown>;
      expect(body.models).toEqual([EDITED, model('beta')]);
      expect(Object.keys(body)).toEqual(['baseline', 'models']);
      expect(onSaved).toHaveBeenCalled();
      expect(onClose).toHaveBeenCalled();
    });

    it('keeps a candidate the offer holds with no suppliers, and promises exactly that', async () => {
      const user = userEvent.setup();
      // The other side of the same definition: empty is not withdrawn. A
      // built-in the server still offers with nothing behind it is a candidate
      // whose route starts empty, and a refusal naming it with no suppliers is a
      // statement about supply, not about the offer. So the row stays, the
      // re-ask shows it claiming no supplier, and the save promises the empty
      // list the user agreed to — which is what lets the server seed an empty
      // route instead of matching a supplier nobody offered.
      const bare = candidate('glm-5.2', { display_name: 'GLM 5.2', origin: 'builtin' });
      vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(CATALOG));
      vi.spyOn(modelsApi, 'getAgentModelCandidates').mockResolvedValue(offered({ builtin: [bare] }));
      const write = vi.spyOn(modelsApi, 'putAgentModels')
        .mockRejectedValueOnce(staleCandidates({ 'glm-5.2': [] }))
        .mockResolvedValue(agent([...CATALOG, model('glm-5.2')]));
      const { onSaved } = renderDialog();

      await user.click(await enabledAddModels());
      await user.click(await screen.findByRole('checkbox', { name: /GLM 5\.2/ }));
      await user.click(screen.getByRole('button', { name: 'Add 1 model' }));
      await user.click(await screen.findByRole('button', { name: 'Save' }));

      // Asked again rather than withdrawn, still picked, and showing the model
      // and nothing else: there is no supplier to name, and a chip here would be
      // the dialog claiming one on the server's behalf.
      const reasked = await screen.findByRole('checkbox', { name: /GLM 5\.2/ });
      expect(reasked.getAttribute('aria-checked')).toBe('true');
      expect((reasked as HTMLButtonElement).disabled).toBe(false);
      expect(reasked.textContent).toBe('GLM 5.2glm-5.2');
      expect(onSaved).not.toHaveBeenCalled();

      await user.click(screen.getByRole('button', { name: 'Add 1 model' }));
      await user.click(await screen.findByRole('button', { name: 'Save' }));

      await waitFor(() => expect(write).toHaveBeenCalledTimes(2));
      const body = write.mock.lastCall?.[1] as Record<string, unknown>;
      expect(body.models).toEqual([...CATALOG, candidateBackendModel(bare)]);
      // Promised, not omitted: an addition with no entry is one the server
      // matches by its own reading, and 「nothing supplies this yet」 is a claim
      // only the user's agreement can carry.
      expect(body.expected_suppliers).toEqual({ 'glm-5.2': [] });
    });

    it('promises what the refreshed offer holds, whichever picks the refusal disputes', async () => {
      const user = userEvent.setup();
      // One refusal carrying both answers at once: a pick the offer has dropped
      // and a pick whose suppliers moved. The property is that the next write
      // promises exactly what the server just said is there — so the withdrawn
      // id may appear in neither the list nor the agreement, and the re-asked
      // one may appear in both only with today's suppliers.
      const kimi = candidate('kimi-3', {
        display_name: 'Kimi 3',
        suppliers: [{ source_id: 'src_a', source_name: 'Primary relay', model_id: 'kimi-3-turbo' }],
      });
      const kimiNow = candidate('kimi-3', {
        display_name: 'Kimi 3',
        suppliers: [{ source_id: 'src_b', source_name: 'Backup relay', model_id: 'kimi-3' }],
      });
      vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(CATALOG));
      vi.spyOn(modelsApi, 'getAgentModelCandidates')
        .mockResolvedValueOnce(offered({ providers: [shown, kimi] }))
        // The refreshed offer no longer holds `glm-5.2` at all. That, and not the
        // empty supplier list the refusal names it with, is what withdraws it.
        .mockResolvedValue(offered({ providers: [kimiNow] }));
      const write = vi.spyOn(modelsApi, 'putAgentModels')
        .mockRejectedValueOnce(staleCandidates({
          'glm-5.2': [],
          'kimi-3': [{ source_id: 'src_b', model_id: 'kimi-3' }],
        }))
        .mockResolvedValue(agent([...CATALOG, model('kimi-3')]));
      renderDialog();

      await user.click(await enabledAddModels());
      await user.click(await screen.findByRole('checkbox', { name: /GLM 5\.2/ }));
      await user.click(screen.getByRole('checkbox', { name: /Kimi 3/ }));
      await user.click(screen.getByRole('button', { name: 'Add 2 models' }));
      await user.click(await screen.findByRole('button', { name: 'Save' }));

      // Reconciled before anything reopens: the picker comes back seeded with the
      // one id that is still a question, and the other is already gone from the
      // draft — so this re-ask cannot be answered with an agreement the dialog
      // would have no way to discharge.
      expect(await screen.findByRole('checkbox', { name: /Backup relay/ })).toBeTruthy();
      expect(screen.queryByText('GLM 5.2')).toBeNull();
      await user.click(screen.getByRole('button', { name: 'Add 1 model' }));
      await user.click(await screen.findByRole('button', { name: 'Save' }));

      await waitFor(() => expect(write).toHaveBeenCalledTimes(2));
      const body = write.mock.lastCall?.[1] as Record<string, unknown>;
      expect(body.models).toEqual([...CATALOG, candidateBackendModel(kimi)]);
      expect(body.expected_suppliers).toEqual({ 'kimi-3': [{ source_id: 'src_b', model_id: 'kimi-3' }] });
    });

    it('takes a dismissed re-ask as 「add none of these」, so a refused promise cannot be re-sent', async () => {
      const user = userEvent.setup();
      // The dead end this closes: one seeded id, unchecked, leaves the primary
      // with nothing to confirm — so walking away has to be an answer the dialog
      // acts on, or the refused projection is the only thing the next save can
      // send and the server refuses it again forever.
      vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(CATALOG));
      vi.spyOn(modelsApi, 'getAgentModelCandidates')
        .mockResolvedValueOnce(offered({ providers: [shown] }))
        .mockResolvedValue(offered({ providers: [current] }));
      const write = vi.spyOn(modelsApi, 'putAgentModels')
        .mockRejectedValueOnce(staleCandidates({ 'glm-5.2': [{ source_id: 'src_b', model_id: 'glm-5.2' }] }))
        .mockResolvedValue(agent(CATALOG));
      renderDialog();

      await user.click(await enabledAddModels());
      await user.click(await screen.findByRole('checkbox', { name: /Primary relay/ }));
      await user.click(screen.getByRole('button', { name: 'Add 1 model' }));
      await user.click(await screen.findByRole('button', { name: 'Save' }));

      await screen.findByRole('checkbox', { name: /Backup relay/ });
      // Either control the picker offers to leave is the same answer, and the
      // dialog treats it as one: 「add none of these」. Asked of one name, since
      // the corner control answers to 「Close」 and only the footer to this.
      const dismiss = within(screen.getByRole('dialog', { name: 'Add Claude Code models' }))
        .getByRole('button', { name: 'Cancel' });
      await user.click(dismiss);

      // The row and its promise go together. What is left is the list the server
      // already holds, so there is nothing to save — the surest statement that
      // the refused projection cannot go out again.
      await waitFor(() => expect(screen.queryByText('GLM 5.2')).toBeNull());
      expect(screen.getByRole('button', { name: 'Save' }).hasAttribute('disabled')).toBe(true);
      expect(write).toHaveBeenCalledTimes(1);
    });

    it('takes the custom-model door out of a re-ask as the same 「add none of these」', async () => {
      const user = userEvent.setup();
      // The third way out of a seeded re-ask, and the one that does not answer
      // it: leaving by the editor confirms none of the seeded ids just as
      // walking away does, so their refused projections have to be discharged
      // on the way out. Kept, they are what the next Save would send — an
      // agreement the server has already refused, granted by a door.
      vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(CATALOG));
      vi.spyOn(modelsApi, 'getAgentModelCandidates')
        .mockResolvedValueOnce(offered({ providers: [shown] }))
        .mockResolvedValue(offered({ providers: [current] }));
      vi.spyOn(modelsApi, 'searchModelsDev').mockResolvedValue([]);
      const write = vi.spyOn(modelsApi, 'putAgentModels')
        .mockRejectedValueOnce(staleCandidates({ 'glm-5.2': [{ source_id: 'src_b', model_id: 'glm-5.2' }] }))
        .mockResolvedValue(agent(CATALOG));
      renderDialog();

      await user.click(await enabledAddModels());
      await user.click(await screen.findByRole('checkbox', { name: /Primary relay/ }));
      await user.click(screen.getByRole('button', { name: 'Add 1 model' }));
      await user.click(await screen.findByRole('button', { name: 'Save' }));

      await screen.findByRole('checkbox', { name: /Backup relay/ });
      await user.click(screen.getByRole('button', { name: 'Add custom model…' }));

      // The editor is open on a blank row, and the seeded one left with its
      // promise: what remains is the list the server already holds, so there is
      // nothing to save — the surest statement that the refused projection
      // cannot go out again. Asked back at the catalog, since the door closed
      // the picker on its way out; scoped to the editor, whose footer is the
      // one control in it that carries this word.
      expect((await screen.findByLabelText('Model') as HTMLInputElement).value).toBe('');
      const leave = within(screen.getByRole('dialog', { name: 'Add model' }))
        .getByRole('button', { name: 'Cancel' });
      await user.click(leave);
      await waitFor(() => expect(screen.queryByText('GLM 5.2')).toBeNull());
      expect(screen.getByRole('button', { name: 'Save' }).hasAttribute('disabled')).toBe(true);
      expect(write).toHaveBeenCalledTimes(1);
    });

  });

  it('treats a lost answer as saved once the re-read shows the intent already applied', async () => {
    const user = userEvent.setup();
    const settled = agent([model('alpha')]);
    vi.spyOn(modelsApi, 'getAgentSources')
      .mockResolvedValueOnce(agent([model('alpha'), model('beta')]))
      .mockResolvedValueOnce(settled);
    vi.spyOn(modelsApi, 'putAgentModels').mockRejectedValue(new Error('connection reset'));
    const { onSaved, onClose } = renderDialog();

    await user.click(await screen.findByRole('button', { name: 'Remove beta' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(settled));
    expect(onClose).toHaveBeenCalled();
  });

  it('refuses to call a runtime-refresh failure a save, and still lets the user retry it', async () => {
    const user = userEvent.setup();
    // The server commits the catalog before it asks the backend to load it, so
    // `engine_down` leaves the rows on disk and out of use at once. The re-read
    // finds the intent applied — and that is exactly the answer that must not be
    // mistaken for success, because the route already said what it did.
    const settled = agent([model('alpha')]);
    vi.spyOn(modelsApi, 'getAgentSources')
      .mockResolvedValueOnce(agent([model('alpha'), model('beta')]))
      .mockResolvedValue(settled);
    const write = vi.spyOn(modelsApi, 'putAgentModels')
      .mockRejectedValueOnce(new ApiCallError('engine_down', 'modelHub.errors.engine_down', true))
      .mockResolvedValueOnce(settled);
    const { onSaved, onObserved, onClose } = renderDialog();

    await user.click(await screen.findByRole('button', { name: 'Remove beta' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText('The list was stored, but this backend did not load it, so it is not in use yet. Save again once the backend is back.')).toBeTruthy();
    expect(screen.queryByText('The model list was not saved. It was re-read from the server; check it and try again.')).toBeNull();
    expect(onSaved).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(onObserved).toHaveBeenCalledWith(settled);

    // The re-read left the draft agreeing with the server, so 「nothing to send」
    // must not read as 「nothing to do」: the write is what failed.
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    expect(save.disabled).toBe(false);
    await user.click(save);

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(settled));
    expect(onClose).toHaveBeenCalled();
    expect(write).toHaveBeenCalledTimes(2);
  });

  it('keeps a refused save named as refused when the server state happens to agree', async () => {
    const user = userEvent.setup();
    // Someone else removed beta too, so the re-read matches the draft exactly.
    // That coincidence says nothing about the write this server refused — only
    // the runtime-refresh failure can leave a list stored and unloaded.
    vi.spyOn(modelsApi, 'getAgentSources')
      .mockResolvedValueOnce(agent([model('alpha'), model('beta')]))
      .mockResolvedValue(agent([model('alpha')]));
    vi.spyOn(modelsApi, 'putAgentModels').mockRejectedValue(
      new ApiCallError('invalid_request', 'modelHub.errors.backend_model_conflict', true),
    );
    const { onSaved, onClose } = renderDialog();

    await user.click(await screen.findByRole('button', { name: 'Remove beta' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText('This list changed elsewhere while you were editing. Your changes were replayed onto the newer list; check it and save again.')).toBeTruthy();
    expect(screen.queryByText(/The list was stored/)).toBeNull();
    expect(onSaved).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('keeps a reorder-only draft open when an inconclusive save did not land', async () => {
    const user = userEvent.setup();
    const catalog = [model('alpha'), model('beta')];
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(catalog));
    vi.spyOn(modelsApi, 'putAgentModels').mockRejectedValue(new Error('connection reset'));
    const { onSaved, onObserved, onClose } = renderDialog();

    const grip = await screen.findByRole('button', { name: 'Reorder alpha' });
    grip.focus();
    await user.keyboard('[Space][ArrowDown][Space]');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onObserved).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('status').textContent).toBeTruthy();
    expect(onSaved).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getAllByRole('button', { name: /^Reorder / }).map((button) => button.getAttribute('aria-label')))
      .toEqual(['Reorder beta', 'Reorder alpha']);
  });
});
