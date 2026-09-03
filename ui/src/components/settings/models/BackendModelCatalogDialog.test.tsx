// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { BackendModelCatalogDialog } from './BackendModelCatalogDialog';
import { blankBackendModel, candidateBackendModel } from './backendCatalog';
import { ApiCallError, modelsApi } from './modelsApi';
import type { AgentSupply, BackendModel, ModelCandidate, RouteHop } from './types';

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

/** The confirmation lives inside the row it is about, and its Cancel is one of
 *  three on screen. */
const confirmation = () => within(screen.getByRole('alert').parentElement as HTMLElement);

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
  it('shows a locked row without any way to edit, remove, or reorder it', async () => {
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([locked, model('alpha')]));
    renderDialog();

    expect(await screen.findByRole('heading', { name: 'Claude Code models' })).toBeTruthy();
    expect(screen.getByText('claude-default')).toBeTruthy();
    expect(screen.getByText('Default')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Reorder alpha' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Reorder claude-default' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Edit claude-default' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Remove claude-default' })).toBeNull();
    expect(screen.getByText('2 models')).toBeTruthy();
  });

  it('localizes the server-owned Claude default row instead of rendering backend copy', async () => {
    await i18n.changeLanguage('zh');
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent([
      model('default', { display_name: null, locked: true, routeable: false }),
    ]));
    renderDialog();

    expect(await screen.findByText('Claude Code 默认模型')).toBeTruthy();
    expect(screen.getByText('default')).toBeTruthy();
    expect(screen.queryByText('Default')).toBeNull();
    expect(screen.getByLabelText('搜索名称或模型 ID')).toBeTruthy();
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

  it('adds what the providers offer, and promises the suppliers it displayed', async () => {
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

    await user.click(await screen.findByRole('button', { name: 'Add models' }));
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

    await user.click(await screen.findByRole('button', { name: 'Add models' }));
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
    // The id the user types is not yet the id the row is saved under: on OpenCode
    // an unrecognized provider resolves to `custom/`, and the editor applies that
    // rule when it commits. So the lookup behind this door has to be asked about
    // the resolved id. Asked about the raw one it misses the row the user already
    // has and opens a blank one, which then commits over that same saved id and
    // drops everything they had described about it.
    const FOO = model('custom/foo', {
      display_name: 'Foo Air',
      context_window: 180000,
      origin: 'manual',
    });
    const catalog = [model('zai/glm-4.7'), FOO];
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(catalog, {
      backend: 'opencode',
      standard_vendors: ['zai'],
    }));
    // Nothing offered under this query, which is the only state that offers the
    // typed id as a custom model at all.
    vi.spyOn(modelsApi, 'getAgentModelCandidates').mockResolvedValue(offered());
    vi.spyOn(modelsApi, 'searchModelsDev').mockResolvedValue([]);
    const write = vi.spyOn(modelsApi, 'putAgentModels').mockResolvedValue(agent(catalog));
    renderDialog({ backend: 'opencode' });

    await user.click(await screen.findByRole('button', { name: 'Remove Foo Air' }));
    await user.click(screen.getByRole('button', { name: 'Add models' }));
    await user.type(await screen.findByLabelText('Search models or providers'), 'foo');
    await user.click(await screen.findByRole('button', { name: 'Add "foo" as a custom model…' }));

    // Their own row, opened as itself: the id fixed, and the description they
    // wrote still in it. The button is the tell — this is an edit, not an add.
    expect((await screen.findByLabelText('Model') as HTMLInputElement).value).toBe('custom/foo');
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
      models: [model('zai/glm-4.7'), { ...FOO, max_output_tokens: 8000 }],
    }));
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

  it('shows what a removal takes with it, then echoes the guard byte for byte whatever order it was clicked in', async () => {
    const user = userEvent.setup();
    const catalog = [model('alpha'), model('beta'), model('gamma')];
    /**
     * The server's plan, in the server's order: it walks the baseline it was
     * sent, so `beta` comes before `gamma` no matter which the user clicked
     * first. Written once, because it is the fixture for every half of the
     * property below — what each question shows, and what the forced save
     * carries, are these same bytes.
     */
    const hops = [
      { backend: 'claude' as const, menu_model: 'beta', source_id: 'src_a', model_id: 'beta-air', position: 1 },
      { backend: 'claude' as const, menu_model: 'beta', source_id: 'src_gone', model_id: 'beta-backup', position: 2 },
      { backend: 'claude' as const, menu_model: 'gamma', source_id: 'src_a', model_id: 'gamma-air', position: 1 },
    ];
    const routed = (menuModel: string) => hops.filter((hop) => hop.menu_model === menuModel);
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(catalog, {
      routes: Object.fromEntries(['beta', 'gamma'].map((menuModel) => [
        menuModel,
        { hops: routed(menuModel).map(({ source_id, model_id }) => ({ source_id, model_id })) },
      ])),
    }));
    const write = vi.spyOn(modelsApi, 'putAgentModels')
      .mockRejectedValueOnce(new ApiCallError(
        'backend_model_in_route',
        'modelHub.errors.backend_model_in_route',
        true,
        [],
        [],
        hops,
        409,
      ))
      .mockResolvedValue(agent([model('alpha')]));
    renderDialog();

    // A route is the only thing a removal takes with it that the user did not
    // name, so it is the only removal that asks — and it asks on the click,
    // from the routes the dialog already holds, not after a round-trip.
    await user.click(await screen.findByRole('button', { name: 'Remove alpha' }));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText('2 models')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'Remove beta' }));
    const asked = screen.getByRole('alert');
    // The shared guard body, as the Route dialog shows it: every hop the plan
    // names, at the order it sits at, and however many there are.
    expect(asked.textContent).toContain('2 hops');
    for (const hop of routed('beta')) {
      expect(within(asked).getByText(`${hop.model_id} · Order #${hop.position}`)).toBeTruthy();
    }
    // Whether anything is stranded is the guard's answer, not this dialog's, so
    // the preview says what it knows and stays silent about the rest. Both
    // readings are withheld, not just the alarming one: 「still has another
    // source available」 is the dangerous half here, because it is a promise
    // about supply this dialog cannot see, made in the confirmation the user
    // decides on.
    expect(within(asked).queryByText('Models that will be left with no source')).toBeNull();
    expect(asked.textContent).not.toContain('These models still have another source available.');
    expect(asked.textContent).not.toContain('Some models will be left with no usable source.');
    expect(screen.getByText('2 models')).toBeTruthy();

    // Asking is not doing.
    await user.click(confirmation().getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText('2 models')).toBeTruthy();

    // Answered in the order the user chose, which is the reverse of the order
    // the server will report them in.
    const clicked = ['gamma', 'beta'];
    for (const menuModel of clicked) {
      await user.click(screen.getByRole('button', { name: `Remove ${menuModel}` }));
      await user.click(confirmation().getByRole('button', { name: 'Remove' }));
    }
    expect(screen.getByText('0 models')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(write).toHaveBeenCalledTimes(2));
    // Nothing was asked twice: the guard named the consequences the user had
    // already accepted, so the retry is the dialog's own business rather than a
    // second confirmation of the same removal.
    expect(screen.queryByRole('alert')).toBeNull();
    const [first, second] = write.mock.calls.map(([, body]) => body as Record<string, unknown>);
    // The first attempt claims no agreement at all. Only the server states what
    // a write would take with it, so until it has, there is nothing to echo.
    expect(Object.keys(first)).toEqual(['baseline', 'models']);
    // The second is the refusal itself, byte for byte and in the server's own
    // order — byte-equal because that is how the server reads it, element by
    // element against what it sent, so the same consequences in another order
    // is a different answer and not the one it asked for.
    expect(second.force).toBe(true);
    expect(JSON.stringify(second.would_remove_hops)).toBe(JSON.stringify(hops));
    expect(JSON.stringify(second.would_interrupt)).toBe(JSON.stringify([]));
    // And the fixture earns that: the order the rows were clicked in is not the
    // order the echo carries, so nothing reassembled from the questions could
    // have passed the line above.
    expect(clicked).not.toEqual([...new Set(hops.map((hop) => hop.menu_model))]);
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

    await user.click(await screen.findByRole('button', { name: 'Add models' }));
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

    await user.type(await screen.findByLabelText('Search name or model ID'), 'bright');
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
      answer: (user: ReturnType<typeof userEvent.setup>) => Promise<void>;
      /** Exactly the list the answered save sends. Exact, not 「contains」: a row
       *  rebuilt from its candidate keeps the id and loses the edit. */
      models: BackendModel[];
      /** The fields that save adds, beyond the list itself. */
      agreement: Record<string, unknown>;
    };

    /**
     * The plan the server holds and the client could not see.
     *
     * Written once, because it is the fixture for both halves of the property:
     * what the confirmation shows and what the answered save echoes are the same
     * two arrays. It strands a model on purpose — a refusal that interrupts
     * something is still a question the user may answer, so the ask must carry
     * the interruption rather than be withheld because of it.
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
          await user.click(await screen.findByRole('button', { name: 'Add models' }));
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
        },
        models: [ALPHA, model('beta'), { ...candidateBackendModel(shown), context_window: 2000 }],
        agreement: { expected_suppliers: { 'glm-5.2': [{ source_id: 'src_b', model_id: 'glm-5.2' }] } },
      },
      {
        what: 'a route the client could not see guarded the removal',
        // No route in the read at all: it was created after the baseline, which
        // is why the client removed the row without asking and only the server
        // could refuse it. And why the list is not read again — the refusal
        // answers the exact write that was sent, so the draft is rebased onto
        // the same baseline it was built from.
        read: agent(CATALOG),
        arrange: async (user) => {
          // A removal cannot carry an edit, so the work at stake is the rest of
          // the draft: this refusal hands the removal back, and rebasing it in
          // is where an edit goes missing.
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
        // Asked through the same confirmation that records what a save may
        // force, and showing the whole plan the server named rather than the
        // empty one the client had — the same evidence body the Route dialog
        // shows, so a removal here and a route change there answer the same
        // question with the same words.
        reasked: async () => {
          const asked = await screen.findByRole('alert');
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
          await user.click(confirmation().getByRole('button', { name: 'Remove' }));
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
        await user.click(await screen.findByRole('button', { name: 'Save' }));

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

      await user.click(await screen.findByRole('button', { name: 'Add models' }));
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

      await user.click(await screen.findByRole('button', { name: 'Add models' }));
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

    it('restores a refused removal to its own place, so cancelling it leaves nothing edited', async () => {
      const user = userEvent.setup();
      // A removal the server refuses was never a decision, so undoing it may not
      // cost the row its place: the requested list is the one it is already gone
      // from, and appending it back would answer 「are you sure?」 with a
      // reordered catalog the user never asked for. Cancelling then has to leave
      // the draft equal to the baseline, rows and order — the only state that
      // can honestly report itself as unedited.
      const rowOrder = () => screen.getAllByRole('button', { name: /^Reorder / })
        .map((row) => row.getAttribute('aria-label')?.replace('Reorder ', ''));
      vi.spyOn(modelsApi, 'getAgentSources')
        .mockResolvedValue(agent([model('alpha'), model('beta'), model('gamma')]));
      const write = vi.spyOn(modelsApi, 'putAgentModels').mockRejectedValue(new ApiCallError(
        'backend_model_in_route',
        'modelHub.errors.backend_model_in_route',
        true,
        [],
        [],
        GUARDED.hops,
        409,
      ));
      const { onSaved, onClose } = renderDialog();

      await user.click(await screen.findByRole('button', { name: 'Remove beta' }));
      expect(rowOrder()).toEqual(['alpha', 'gamma']);
      await user.click(screen.getByRole('button', { name: 'Save' }));

      // Handed back into its own row, between the two it sat between — not after
      // the rows that outlived it.
      expect(await screen.findByRole('alert')).toBeTruthy();
      expect(rowOrder()).toEqual(['alpha', 'beta', 'gamma']);

      await user.click(confirmation().getByRole('button', { name: 'Cancel' }));
      expect(rowOrder()).toEqual(['alpha', 'beta', 'gamma']);
      // And nothing left to send, because nothing is different: a draft still
      // reporting itself as edited would offer a write that repeats the
      // baseline back to the server.
      expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
      expect(write).toHaveBeenCalledTimes(1);
      expect(onSaved).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();
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

      await user.click(await screen.findByRole('button', { name: 'Add models' }));
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

      await user.click(await screen.findByRole('button', { name: 'Add models' }));
      await user.click(await screen.findByRole('checkbox', { name: /Primary relay/ }));
      await user.click(screen.getByRole('button', { name: 'Add 1 model' }));
      await user.click(await screen.findByRole('button', { name: 'Save' }));

      await screen.findByRole('checkbox', { name: /Backup relay/ });
      // Either control the picker offers to leave is the same answer, and the
      // dialog treats it as one: 「add none of these」.
      const [dismiss] = within(screen.getByRole('dialog', { name: 'Add Claude Code models' }))
        .getAllByRole('button', { name: 'Cancel' });
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

      await user.click(await screen.findByRole('button', { name: 'Add models' }));
      await user.click(await screen.findByRole('checkbox', { name: /Primary relay/ }));
      await user.click(screen.getByRole('button', { name: 'Add 1 model' }));
      await user.click(await screen.findByRole('button', { name: 'Save' }));

      await screen.findByRole('checkbox', { name: /Backup relay/ });
      await user.click(screen.getByRole('button', { name: 'Add custom model…' }));

      // The editor is open on a blank row, and the seeded one left with its
      // promise: what remains is the list the server already holds, so there is
      // nothing to save — the surest statement that the refused projection
      // cannot go out again. Asked back at the catalog, since the door closed
      // the picker on its way out; the editor's own control is named for it,
      // because the catalog behind it carries that word too.
      expect((await screen.findByLabelText('Model') as HTMLInputElement).value).toBe('');
      const [leave] = within(screen.getByRole('dialog', { name: 'Add model' }))
        .getAllByRole('button', { name: 'Cancel' });
      await user.click(leave);
      await waitFor(() => expect(screen.queryByText('GLM 5.2')).toBeNull());
      expect(screen.getByRole('button', { name: 'Save' }).hasAttribute('disabled')).toBe(true);
      expect(write).toHaveBeenCalledTimes(1);
    });

    it('asks about every removal the guard held back, not just the first', async () => {
      const user = userEvent.setup();
      const catalog = [model('alpha'), model('beta'), model('gamma')];
      const routed = agent(catalog, {
        routes: {
          beta: { hops: [{ source_id: 'src_a', model_id: 'beta-air' }] },
          gamma: { hops: [{ source_id: 'src_a', model_id: 'gamma-air' }] },
        },
      });
      vi.spyOn(modelsApi, 'getAgentSources')
        .mockResolvedValueOnce(agent(catalog))
        .mockResolvedValue(routed);
      /** One hop per held-back removal. The fixture for the whole property:
       *  each question shows its own hop, and the answered save echoes all of
       *  them — so neither half may state the plan a second time. */
      const hops = [
        { backend: 'claude' as const, menu_model: 'beta', source_id: 'src_a', model_id: 'beta-air', position: 1 },
        { backend: 'claude' as const, menu_model: 'gamma', source_id: 'src_a', model_id: 'gamma-air', position: 1 },
      ];
      const write = vi.spyOn(modelsApi, 'putAgentModels')
        .mockRejectedValueOnce(new ApiCallError(
          'backend_model_in_route',
          'modelHub.errors.backend_model_in_route',
          true,
          [],
          [],
          hops,
          409,
        ))
        .mockResolvedValue(agent([model('alpha')]));
      renderDialog();

      await user.click(await screen.findByRole('button', { name: 'Remove beta' }));
      await user.click(screen.getByRole('button', { name: 'Remove gamma' }));
      await user.click(screen.getByRole('button', { name: 'Save' }));

      // One question at a time, because the confirmation lives in the row it is
      // about — but every held-back removal gets one, carrying its own hop and
      // no one else's. A row handed back with no question is a removal the user
      // asked for that nobody answered; a question carrying the whole refusal
      // would ask each row to confirm the others.
      for (const hop of hops) {
        const asked = await screen.findByRole('alert');
        expect(within(asked).getByText(`${hop.model_id} · Order #${hop.position}`)).toBeTruthy();
        expect(asked.textContent).toContain('1 hop');
        await user.click(confirmation().getByRole('button', { name: 'Remove' }));
      }
      expect(screen.queryByRole('alert')).toBeNull();

      await user.click(screen.getByRole('button', { name: 'Save' }));

      await waitFor(() => expect(write).toHaveBeenLastCalledWith('claude', expect.objectContaining({
        models: [model('alpha')],
        force: true,
        would_remove_hops: hops,
        would_interrupt: [],
      })));
    });

    it('shows the row of whichever question is pending, whatever the search says', async () => {
      const user = userEvent.setup();
      // A question pending and a filter that matches none of the rows it is
      // about. The confirmation renders inside its row, so a filter that hid
      // that row would leave the draft holding a model the user removed, Save
      // disabled behind an unanswered question, and nothing on screen to
      // explain either — a dead end reachable by typing.
      const catalog = [model('alpha'), model('beta'), model('gamma')];
      const routed = agent(catalog, {
        routes: {
          beta: { hops: [{ source_id: 'src_a', model_id: 'beta-air' }] },
          gamma: { hops: [{ source_id: 'src_a', model_id: 'gamma-air' }] },
        },
      });
      vi.spyOn(modelsApi, 'getAgentSources')
        .mockResolvedValueOnce(agent(catalog))
        .mockResolvedValue(routed);
      const hops = [
        { backend: 'claude' as const, menu_model: 'beta', source_id: 'src_a', model_id: 'beta-air', position: 1 },
        { backend: 'claude' as const, menu_model: 'gamma', source_id: 'src_a', model_id: 'gamma-air', position: 1 },
      ];
      const write = vi.spyOn(modelsApi, 'putAgentModels')
        .mockRejectedValueOnce(new ApiCallError(
          'backend_model_in_route',
          'modelHub.errors.backend_model_in_route',
          true,
          [],
          [],
          hops,
          409,
        ))
        .mockResolvedValue(agent([model('alpha')]));
      renderDialog();

      await user.click(await screen.findByRole('button', { name: 'Remove beta' }));
      await user.click(screen.getByRole('button', { name: 'Remove gamma' }));
      await user.click(screen.getByRole('button', { name: 'Save' }));
      await screen.findByRole('alert');
      await user.type(screen.getByLabelText('Search name or model ID'), 'alpha');

      for (const hop of hops) {
        // Whichever question is pending, its row is on screen with its own hop
        // and its own controls — and it is the only held-back row the search
        // lets through, so what put it there was the queue, not the query.
        const asked = await screen.findByRole('alert');
        expect(within(asked).getByText(`${hop.model_id} · Order #${hop.position}`)).toBeTruthy();
        expect(screen.getByText(hop.menu_model)).toBeTruthy();
        for (const other of hops.filter((entry) => entry !== hop)) {
          expect(screen.queryByText(other.menu_model)).toBeNull();
        }
        await user.click(confirmation().getByRole('button', { name: 'Remove' }));
      }

      // Queue empty, so the search is back in charge of the whole list.
      expect(screen.queryByRole('alert')).toBeNull();
      await user.click(screen.getByRole('button', { name: 'Save' }));

      await waitFor(() => expect(write).toHaveBeenLastCalledWith('claude', expect.objectContaining({
        models: [model('alpha')],
        force: true,
        would_remove_hops: hops,
        would_interrupt: [],
      })));
    });

    it('discards a queued question whose row has already left the draft, and forces nothing on its behalf', async () => {
      const user = userEvent.setup();
      // The queue outlives the question on screen, and the rows behind the head
      // keep their own controls — so the user can remove one before its turn
      // comes. There is then nothing left to remove and nowhere to ask, because
      // the confirmation renders inside the row, so the question goes with the
      // row. Left pending it would be worse than invisible: it is the head, and
      // the row that could advance the queue is the one that is gone, so every
      // question behind it goes unasked and the removals the user did ask for
      // can never be confirmed — the guard would refuse the list forever.
      //
      // Discarding it keeps the queue live but settles nothing: the consequence
      // the server named for that row was never displayed. So the save that
      // follows goes out unforced, and the two properties hold together — the
      // dialog never stalls, and it never vouches for what it did not show.
      const catalog = [model('alpha'), model('beta'), model('gamma'), model('delta')];
      vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(catalog, {
        // The one route this dialog can see. The others were created after the
        // read, which is why only the server could refuse them.
        routes: { delta: { hops: [{ source_id: 'src_a', model_id: 'delta-air' }] } },
      }));
      const hops = [
        { backend: 'claude' as const, menu_model: 'beta', source_id: 'src_a', model_id: 'beta-air', position: 1 },
        { backend: 'claude' as const, menu_model: 'gamma', source_id: 'src_a', model_id: 'gamma-air', position: 1 },
      ];
      const refusal = () => new ApiCallError(
        'backend_model_in_route',
        'modelHub.errors.backend_model_in_route',
        true,
        [],
        [],
        hops,
        409,
      );
      const write = vi.spyOn(modelsApi, 'putAgentModels')
        .mockRejectedValueOnce(refusal())
        // The same refusal, because nothing was accepted and so nothing about
        // the routes changed: an unforced save asks the guard the same question
        // and gets the same answer.
        .mockRejectedValueOnce(refusal())
        .mockResolvedValue(agent([model('alpha'), model('delta')]));
      renderDialog();

      await user.click(await screen.findByRole('button', { name: 'Remove beta' }));
      await user.click(screen.getByRole('button', { name: 'Remove gamma' }));
      await user.click(screen.getByRole('button', { name: 'Save' }));

      // Both removals come back with a question each, the first of them on
      // screen with the server's own hop.
      const asked = await screen.findByRole('alert');
      expect(within(asked).getByText('beta-air · Order #1')).toBeTruthy();

      // The user answers the second one their own way instead: gamma has no
      // route this dialog knows of, so it simply leaves — and takes both the
      // pending question and its own queued one with it.
      await user.click(screen.getByRole('button', { name: 'Remove gamma' }));
      expect(screen.queryByRole('alert')).toBeNull();

      // A question about a row that is still here, so the queue gets another
      // chance to reach the one about the row that is not.
      await user.click(screen.getByRole('button', { name: 'Remove delta' }));
      expect(within(screen.getByRole('alert')).getByText('delta-air · Order #1')).toBeTruthy();
      await user.click(confirmation().getByRole('button', { name: 'Cancel' }));

      // Nothing is asked in gamma's name. The dialog is answering the user
      // again — not holding a question with no row to hold it, behind which
      // beta's removal could never be confirmed.
      expect(screen.queryByRole('alert')).toBeNull();
      expect(screen.getByText('delta')).toBeTruthy();

      await user.click(screen.getByRole('button', { name: 'Remove beta' }));
      await user.click(screen.getByRole('button', { name: 'Save' }));

      // The list is byte-for-byte the one the guard refused — and that is not
      // enough. Both questions left without ever being answered, so the two
      // hops the server named were never put to the user, and a `force` here
      // would be the client vouching for a consequence nobody was shown.
      await waitFor(() => expect(write).toHaveBeenCalledTimes(2));
      expect(write.mock.calls[1]?.[1]).toEqual({ baseline: catalog, models: [model('alpha'), model('delta')] });

      // So the guard refuses it again, with the same two hops, and the
      // questions come back — the round-trip the swallow cost, in exchange for
      // the user seeing what they are agreeing to.
      const reasked = await screen.findByRole('alert');
      expect(within(reasked).getByText('beta-air · Order #1')).toBeTruthy();
      await user.click(confirmation().getByRole('button', { name: 'Remove' }));
      expect(within(screen.getByRole('alert')).getByText('gamma-air · Order #1')).toBeTruthy();
      await user.click(confirmation().getByRole('button', { name: 'Remove' }));

      // Answered now, both of them, against the server's own account. This is
      // the only way the echo is reached, so it carries the refusal whole and
      // in the server's order rather than a transcript of the asking.
      await user.click(screen.getByRole('button', { name: 'Save' }));
      await waitFor(() => expect(write).toHaveBeenLastCalledWith('claude', expect.objectContaining({
        models: [model('alpha'), model('delta')],
        force: true,
        would_remove_hops: hops,
        would_interrupt: [],
      })));
      expect(write).toHaveBeenCalledTimes(3);
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
