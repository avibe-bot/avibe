// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { BackendModelCatalogDialog } from './BackendModelCatalogDialog';
import { blankBackendModel, candidateBackendModel } from './backendCatalog';
import { ApiCallError, modelsApi } from './modelsApi';
import type { AgentSupply, BackendModel, ModelCandidate, RouteHop, Source } from './types';

const model = (id: string, overrides: Partial<BackendModel> = {}): BackendModel => ({
  ...blankBackendModel(),
  id,
  ...overrides,
});

const locked = model('claude-default', { locked: true, routeable: false });

const source = (id: string, displayName: string): Source => ({
  id,
  last_discovered_at: null,
  kind: 'api_key',
  vendor: 'anthropic',
  display_name: displayName,
  protocol: 'anthropic',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active', retry_at: null, detail_key: null },
  models: [],
});

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
        sources={[source('src_a', 'Primary relay')]}
        canReadSources
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

  it('names what a removal takes with it, and removes both together once agreed', async () => {
    const user = userEvent.setup();
    const catalog = [model('alpha'), model('beta')];
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent(catalog, {
      routes: {
        alpha: {
          hops: [
            { source_id: 'src_a', model_id: 'glm-5.2-air' },
            { source_id: 'src_gone', model_id: 'backup' },
          ],
        },
      },
    }));
    const putAgentModels = vi.spyOn(modelsApi, 'putAgentModels').mockResolvedValue(agent([]));
    renderDialog();

    // A route is the only thing a removal takes that the user did not name, so
    // it is the only removal that asks first.
    await user.click(await screen.findByRole('button', { name: 'Remove beta' }));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText('1 model')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'Remove alpha' }));
    // Named by the Sources and upstream models the user can recognise, in route
    // order — and by the id for a Source this client never read, because the
    // consequence is worth stating either way.
    expect(screen.getByRole('alert').textContent)
      .toBe('Also removes its route: Primary relay → glm-5.2-air, src_gone → backup');
    expect(screen.getByText('1 model')).toBeTruthy();

    // Asking is not doing.
    await user.click(confirmation().getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText('1 model')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'Remove alpha' }));
    await user.click(confirmation().getByRole('button', { name: 'Remove' }));
    expect(screen.getByText('0 models')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Save' }));

    // The save echoes the plan the user was shown, one-based like the server's
    // own positions, so the guard is answered for exactly that plan and not for
    // whatever the routes hold by the time it lands.
    await waitFor(() => expect(putAgentModels).toHaveBeenCalledWith('claude', {
      baseline: catalog,
      models: [],
      force: true,
      would_remove_hops: [
        { backend: 'claude', menu_model: 'alpha', source_id: 'src_a', model_id: 'glm-5.2-air', position: 1 },
        { backend: 'claude', menu_model: 'alpha', source_id: 'src_gone', model_id: 'backup', position: 2 },
      ],
      // This dialog never showed an interruption plan, so it cannot claim one
      // was confirmed.
      would_interrupt: [],
    }));
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

    // Nothing was committed, so there is nothing to report and nothing to
    // re-read: the answer to this refusal is the same question again, asked
    // against the suppliers the server matched this time.
    expect(await screen.findByRole('checkbox', { name: /Backup relay/ })).toBeTruthy();
    expect(read).toHaveBeenCalledTimes(2);
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
    const ROUTED = agent(CATALOG, {
      routes: { beta: { hops: [{ source_id: 'src_a', model_id: 'beta-air' }] } },
    });

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
      /** What the server serves, in order. A refusal that re-reads needs a
       *  second answer; one that does not must not be handed one it could pass
       *  on. */
      reads: AgentSupply[];
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

    const MEMBERS: readonly Member[] = [
      {
        what: 'the suppliers the picker displayed went stale',
        reads: [agent(CATALOG)],
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
        // The route appears only in the second read: it was created after the
        // baseline, which is why the client removed the row without asking and
        // only the server could refuse it.
        reads: [agent(CATALOG), ROUTED],
        arrange: async (user) => {
          // A removal cannot carry an edit, so the work at stake is the rest of
          // the draft: this refusal re-reads the list, and rebasing onto it is
          // where an edit goes missing.
          await widen(user, 'alpha');
          await user.click(await screen.findByRole('button', { name: 'Remove beta' }));
          expect(screen.queryByRole('alert')).toBeNull();
        },
        refusal: new ApiCallError(
          'backend_model_in_route',
          'modelHub.errors.backend_model_in_route',
          true,
          [],
          [],
          [{ backend: 'claude', menu_model: 'beta', source_id: 'src_a', model_id: 'beta-air', position: 1 }],
          409,
        ),
        // Asked through the same confirmation that records what a save may
        // force, and naming the plan the server has rather than the empty one
        // the client had.
        reasked: () => screen.findByText('Also removes its route: Primary relay → beta-air'),
        answer: async (user) => {
          await user.click(confirmation().getByRole('button', { name: 'Remove' }));
        },
        models: [EDITED],
        agreement: {
          force: true,
          would_remove_hops: [
            { backend: 'claude', menu_model: 'beta', source_id: 'src_a', model_id: 'beta-air', position: 1 },
          ],
          would_interrupt: [],
        },
      },
    ];

    for (const member of MEMBERS) {
      it(`asks again and keeps the draft when ${member.what}`, async () => {
        const user = userEvent.setup();
        const read = vi.spyOn(modelsApi, 'getAgentSources');
        for (const served of member.reads) read.mockResolvedValueOnce(served);
        read.mockResolvedValue(member.reads[member.reads.length - 1] as AgentSupply);
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
      const write = vi.spyOn(modelsApi, 'putAgentModels')
        .mockRejectedValueOnce(new ApiCallError(
          'backend_model_in_route',
          'modelHub.errors.backend_model_in_route',
          true,
          [],
          [],
          [
            { backend: 'claude', menu_model: 'beta', source_id: 'src_a', model_id: 'beta-air', position: 1 },
            { backend: 'claude', menu_model: 'gamma', source_id: 'src_a', model_id: 'gamma-air', position: 1 },
          ],
          409,
        ))
        .mockResolvedValue(agent([model('alpha')]));
      renderDialog();

      await user.click(await screen.findByRole('button', { name: 'Remove beta' }));
      await user.click(screen.getByRole('button', { name: 'Remove gamma' }));
      await user.click(screen.getByRole('button', { name: 'Save' }));

      // One question at a time, because the confirmation lives in the row it is
      // about — but every held-back removal gets one. A row handed back with no
      // question is a removal the user asked for that nobody answered.
      expect(await screen.findByText('Also removes its route: Primary relay → beta-air')).toBeTruthy();
      await user.click(confirmation().getByRole('button', { name: 'Remove' }));
      expect(await screen.findByText('Also removes its route: Primary relay → gamma-air')).toBeTruthy();
      await user.click(confirmation().getByRole('button', { name: 'Remove' }));
      expect(screen.queryByRole('alert')).toBeNull();

      await user.click(screen.getByRole('button', { name: 'Save' }));

      await waitFor(() => expect(write).toHaveBeenLastCalledWith('claude', expect.objectContaining({
        models: [model('alpha')],
        force: true,
        would_remove_hops: [
          { backend: 'claude', menu_model: 'beta', source_id: 'src_a', model_id: 'beta-air', position: 1 },
          { backend: 'claude', menu_model: 'gamma', source_id: 'src_a', model_id: 'gamma-air', position: 1 },
        ],
        would_interrupt: [],
      })));
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
