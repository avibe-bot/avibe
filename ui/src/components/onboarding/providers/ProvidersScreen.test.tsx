// @vitest-environment jsdom
//
// The screen as a whole, against a fake server rather than a queue of canned
// replies: the stage, the sentence, the capsule and the footer action are four
// readings of one machine, and a fixture that answers them in a fixed order would
// decide which of them saw what. Everything underneath is the real thing — the
// real collection authority, the real lifecycle helper, the real takeover dialog —
// because the failures worth catching here are the ones where this screen wires
// two correct pieces together wrongly.
//
// What each case pins is a way the four readings could quietly stop agreeing: a
// connected card that changes under a selection, a footer that offers a batch the
// dialog would refuse, an engine that installs twice, a dismissed capsule that
// moves the button.
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as React from 'react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { readyRegion, type RegionRead } from '@/components/settings/models/regionRead';
import { providerBrandLabel } from '@/components/settings/providers/providerIdentity';
import type {
  AgentSupply,
  MigrationItem,
  RuntimeDependency,
  RuntimeHealth,
  Source,
} from '@/components/settings/models/types';

const showToast = vi.hoisted(() => vi.fn());

vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast }) }));
// The capsule's own capability probe. Hosted here, it decides nothing about the
// offer — the screen's scan does — but the hook would still fire a request.
vi.mock('@/components/settings/models/useModelHubCapability', () => ({
  useModelHubCapability: () => true,
}));

import { ApiCallError, modelsApi } from '@/components/settings/models/modelsApi';
import {
  INITIAL_SETUP_FLOW_STATE,
  type SetupAction,
  type SetupCapability,
  type SetupFlowState,
  type SetupScreenHandle,
  type SetupScreenId,
} from '../setupFlow';
import { ProvidersScreen } from './ProvidersScreen';

// ── Fixtures ──────────────────────────────────────────────────────────────

const source = (over: Partial<Source> & { id: string; vendor: string }): Source => ({
  last_discovered_at: null,
  kind: 'api_key',
  display_name: over.vendor,
  protocol: 'openai_chat',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active' },
  models: [],
  ...over,
});

const supply = (over: Partial<AgentSupply> & { backend: AgentSupply['backend'] }): AgentSupply => ({
  cli_present: true,
  mode: 'direct',
  menu_kind: 'native' as AgentSupply['menu_kind'],
  ...over,
});

const runtimeOf = (health: RuntimeHealth, over: Partial<RuntimeDependency> = {}): RuntimeDependency => ({
  contract_version: 10,
  manifest: { name: 'cliproxyapi', resolution: 'resolved', version: '1.0.0', source_sha: 'sha', assets: [] },
  status: { verified: true, health },
  ...over,
});

const UNSUPPORTED = runtimeOf('not_installed', {
  manifest: { name: 'cliproxyapi', resolution: 'unsupported', version: '1.0.0', source_sha: 'sha', assets: [] },
});

const CODEX_KEY: MigrationItem = {
  id: 'mig_codex_key',
  backend: 'codex',
  kind: 'api_key',
  masked_detail: 'sk-…9f21 · Codex configuration',
  masked_credential: 'sk-…9f21',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
  vendor: 'openai',
  display_name: 'OpenAI',
};

const OPENCODE_KEY: MigrationItem = {
  id: 'mig_opencode_key',
  backend: 'opencode',
  kind: 'opencode_provider',
  masked_detail: 'google · sk-…3456',
  masked_credential: 'sk-…3456',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
  // The scan's spelling, not the catalog's: resolving the two to one id is what
  // stops the same provider occupying two slots.
  vendor: 'google',
  display_name: 'google',
};

// A subscription sign-in. Its presence blocks its whole backend group, which is
// what keeps a Claude card off the stage.
const CLAUDE_SUBSCRIPTION: MigrationItem = {
  id: 'mig_claude_oauth',
  backend: 'claude',
  kind: 'oauth_native',
  masked_detail: 'Claude account (OAuth)',
  proposed_action: 'keep_native',
  selected: true,
  notes_key: null,
  vendor: 'anthropic',
  display_name: 'Anthropic',
};

// ── The fake server ───────────────────────────────────────────────────────

type Server = {
  sources: Source[];
  agents: AgentSupply[];
  scan: MigrationItem[];
  runtime: RuntimeDependency;
};

let server: Server;
const applied: string[][] = [];

const serve = (over: Partial<Server> = {}) => {
  server = {
    sources: [],
    agents: [supply({ backend: 'codex' }), supply({ backend: 'opencode' })],
    scan: [],
    runtime: runtimeOf('ok'),
    ...over,
  };
  vi.spyOn(modelsApi, 'listSources').mockImplementation(async () => server.sources.map((row) => ({ ...row })));
  vi.spyOn(modelsApi, 'listAgents').mockImplementation(async () => server.agents.map((row) => ({ ...row })));
  vi.spyOn(modelsApi, 'refreshAgentPresence')
    .mockImplementation(async () => server.agents.map((row) => ({ ...row })));
  vi.spyOn(modelsApi, 'scanMigration').mockImplementation(async () => ({
    items: server.scan.map((row) => ({ ...row })),
  }));
  vi.spyOn(modelsApi, 'applyMigration').mockImplementation(async (ids) => {
    applied.push([...ids]);
    const taken = server.scan.filter((row) => ids.includes(row.id));
    server.scan = server.scan.filter((row) => !ids.includes(row.id));
    // A landed take-over is a source that now exists. Saying so is what makes the
    // post-import readings an answer from the machine instead of arithmetic.
    server.sources = [
      ...server.sources,
      ...taken.map((row) => source({ id: `src_${row.id}`, vendor: row.vendor ?? 'custom' })),
    ];
    return { applied: taken.length, sources: [] };
  });
  vi.spyOn(modelsApi, 'getRuntimeStatus').mockImplementation(async () => ({ ...server.runtime }));
  vi.spyOn(modelsApi, 'installRuntime').mockImplementation(async () => {
    server.runtime = { ...server.runtime, status: { ...server.runtime.status, health: 'not_started' } };
    return { ...server.runtime };
  });
  vi.spyOn(modelsApi, 'startRuntime').mockImplementation(async () => {
    server.runtime = { ...server.runtime, status: { ...server.runtime.status, health: 'ok' } };
    return { ...server.runtime };
  });
};

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};

// ── The shell's half of the contract ──────────────────────────────────────

const actions: SetupAction[] = [];
const navigated: SetupScreenId[] = [];
const retrySetup = vi.fn();

type ScreenOptions = {
  active?: boolean;
  capability?: SetupCapability;
  gatewayEnabled?: boolean | null;
  runtimeRead?: RegionRead<RuntimeDependency>;
  flow?: Partial<SetupFlowState>;
};

const Harness: React.FC<ScreenOptions & { handle: React.RefObject<SetupScreenHandle | null> }> = ({
  handle,
  active = true,
  capability = 'enabled',
  gatewayEnabled = true,
  runtimeRead = readyRegion(runtimeOf('ok')),
  flow,
}) => {
  // The shell owns the flow state, so the test does too: a screen that kept its
  // own copy would pass every assertion here and lose the selection on re-entry.
  const [flowState, setFlowState] = React.useState<SetupFlowState>({ ...INITIAL_SETUP_FLOW_STATE, ...flow });
  return (
    <I18nextProvider i18n={i18n}>
      <ProvidersScreen
        ref={handle}
        active={active}
        handoff={false}
        capability={capability}
        gatewayEnabled={gatewayEnabled}
        runtimeRead={runtimeRead}
        onRetrySetup={retrySetup}
        flowState={flowState}
        setFlowState={setFlowState}
        onActionChange={(action) => { actions.push(action); }}
        onNavigate={(screen) => { navigated.push(screen); }}
      />
    </I18nextProvider>
  );
};

const renderScreen = (options: ScreenOptions = {}) => {
  const handle = React.createRef<SetupScreenHandle | null>();
  const view = render(<Harness handle={handle} {...options} />);
  return { ...view, handle };
};

const cards = () => [...document.querySelectorAll<HTMLElement>('.setup-provider-card')];
const cardFor = (vendor: string) => {
  const card = document.querySelector<HTMLElement>(`.setup-provider-card[data-provider="${vendor}"]`);
  if (!card) throw new Error(`no card for ${vendor}`);
  return card;
};
const gatewayCard = () => {
  const card = document.querySelector<HTMLElement>('.setup-gateway');
  if (!card) throw new Error('no gateway card');
  return card;
};
const summary = () => document.querySelector<HTMLElement>('.setup-provider-summary');
const offerSlot = () => document.querySelector<HTMLElement>('.setup-provider-offer');
const lastAction = () => actions[actions.length - 1];
const activate = async (handle: React.RefObject<SetupScreenHandle | null>) => {
  await act(async () => { handle.current?.activate(); });
};

/** The stage has settled once the one scan every reading derives from has landed. */
const settled = async () => {
  await waitFor(() => expect(modelsApi.scanMigration).toHaveBeenCalled());
  await waitFor(() => expect(lastAction()).toBeTruthy());
};

beforeEach(async () => {
  applied.length = 0;
  actions.length = 0;
  navigated.length = 0;
  retrySetup.mockReset();
  window.localStorage.clear();
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  showToast.mockReset();
});

describe('ProvidersScreen — the stage', () => {
  it('opens on the shortlist and always offers a third way in', async () => {
    serve();
    renderScreen();
    await settled();

    // Two brand slots and the add-more card, whatever the two brand slots hold.
    expect(cards()).toHaveLength(3);
    expect(cards().map((card) => card.dataset.provider)).toEqual(['openai', 'anthropic', undefined]);
    expect(cards().slice(0, 2).every((card) => card.dataset.state === 'empty')).toBe(true);
    expect(cards()[2].dataset.state).toBe('add');
  });

  it('draws what exists ahead of what was only found, and never captions a card', async () => {
    serve({
      sources: [source({ id: 'src_zhipu', vendor: 'zhipuai' })],
      scan: [CODEX_KEY],
    });
    renderScreen();
    await settled();

    await waitFor(() => expect(cards()[0].dataset.state).toBe('connected'));
    expect(cards().map((card) => card.dataset.provider)).toEqual(['zhipuai', 'openai', undefined]);
    expect(cards()[1].dataset.state).toBe('detected');
    // The check states the decision; the reference draws no word beside it, and
    // the add dialog's 「Added」 tag belongs to that list, not to the stage.
    expect(cardFor('zhipuai').querySelector('.setup-provider-check')).toBeTruthy();
    expect(within(cardFor('zhipuai')).queryByText('Added')).toBeNull();
  });

  it('resolves one provider to one card however the two readings spell it', async () => {
    serve({
      // `google` from the scan, `gemini` from the catalog: one card, not two.
      sources: [source({ id: 'src_gemini', vendor: 'gemini' })],
      scan: [OPENCODE_KEY],
    });
    renderScreen();
    await settled();

    await waitFor(() => expect(cards()[0].dataset.state).toBe('connected'));
    expect(cards().filter((card) => card.dataset.provider === 'gemini')).toHaveLength(1);
  });

  it('a selection toggle never changes a connected card', async () => {
    serve({
      sources: [source({ id: 'src_zhipu', vendor: 'zhipuai' })],
      scan: [CODEX_KEY],
    });
    renderScreen();
    await settled();
    const user = userEvent.setup();
    await waitFor(() => expect(cards()[0].dataset.state).toBe('connected'));

    const connected = cardFor('zhipuai');
    const detected = cardFor('openai');
    // A fact is not a toggle: no pressed state to announce, and nothing to press.
    expect(connected.getAttribute('aria-pressed')).toBeNull();
    expect((connected as HTMLButtonElement).disabled).toBe(true);
    expect(detected.getAttribute('aria-pressed')).toBe('false');

    await user.click(detected);
    await waitFor(() => expect(cardFor('openai').getAttribute('aria-pressed')).toBe('true'));
    expect(cardFor('openai').querySelector('.setup-provider-check')).toBeTruthy();
    expect(cardFor('zhipuai').dataset.state).toBe('connected');
    expect(cardFor('zhipuai').querySelector('.setup-provider-check')).toBeTruthy();

    await user.click(cardFor('openai'));
    await waitFor(() => expect(cardFor('openai').getAttribute('aria-pressed')).toBe('false'));
    expect(cardFor('openai').querySelector('.setup-provider-check')).toBeNull();
    expect(cardFor('zhipuai').querySelector('.setup-provider-check')).toBeTruthy();
  });

  it('counts only the sources added through Add more that are still there', async () => {
    serve({ sources: [source({ id: 'src_kept', vendor: 'zhipu' })] });
    // `src_gone` was added here and has since been removed elsewhere. The badge
    // describes what exists now.
    renderScreen({ flow: { addedThroughMore: ['src_kept', 'src_gone'] } });
    await settled();

    await waitFor(() => expect(within(cards()[2]).queryByText('1 added')).toBeTruthy());
  });

  it('names what is there rather than counting it', async () => {
    serve({ sources: [source({ id: 'src_zhipu', vendor: 'zhipuai', display_name: 'zhipuai' })] });
    renderScreen();
    await settled();

    await waitFor(() => expect(summary()?.textContent)
      .toContain(`1 provider added: ${providerBrandLabel('zhipuai')}`));
    expect(summary()?.dataset.tone).toBeUndefined();
  });

  it('offers a way back when the machine cannot be read', async () => {
    serve();
    vi.mocked(modelsApi.scanMigration).mockRejectedValue(new Error('offline'));
    renderScreen();

    await waitFor(() => expect(summary()?.dataset.tone).toBe('error'));
    const failed = summary();
    if (!failed) throw new Error('no summary');
    await userEvent.setup().click(within(failed).getByRole('button', { name: 'Retry' }));

    // Both halves are re-armed: either the shell's read or this screen's attempt
    // could be what is stale.
    expect(retrySetup).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(modelsApi.scanMigration).toHaveBeenCalledTimes(2));
  });
});

describe('ProvidersScreen — the import capsule', () => {
  it('counts the keys the import list would offer, not the rows the scan returned', async () => {
    serve({ scan: [CODEX_KEY, OPENCODE_KEY, CLAUDE_SUBSCRIPTION] });
    renderScreen();

    expect(await screen.findByText('Found 2 API keys to import into Model Hub')).toBeTruthy();
  });

  it('keeps its slot when the offer is dismissed, so the action below cannot move', async () => {
    serve({ scan: [CODEX_KEY] });
    renderScreen();
    await settled();
    const user = userEvent.setup();

    const slot = offerSlot();
    expect(slot).toBeTruthy();
    expect(slot?.closest('.setup-provider-stage')).toBeTruthy();
    expect(await screen.findByText('Found 1 API key to import into Model Hub')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'Dismiss import notice' }));

    await waitFor(() => expect(screen.queryByText(/API key to import/)).toBeNull());
    // The capsule went; the box it sat in did not.
    expect(offerSlot()).toBe(slot);
  });

  it('hands its review to the screen rather than opening a second dialog', async () => {
    serve({ scan: [CODEX_KEY] });
    renderScreen();
    await settled();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: 'Review migration' }));

    const dialog = await screen.findByRole('dialog');
    // The screen's own scan, in the screen's own dialog: a capsule that scanned
    // again could offer rows the stage and the footer never counted.
    expect(within(dialog).getByText('Migrate to Model Hub')).toBeTruthy();
    expect(modelsApi.scanMigration).toHaveBeenCalledTimes(1);
  });
});

describe('ProvidersScreen — the action the shell renders', () => {
  it('offers to add when there is nothing yet, and opens the add dialog when pressed', async () => {
    serve();
    const { handle } = renderScreen();
    await settled();

    expect(lastAction()).toMatchObject({
      labelKey: 'onboarding.providers.actionAdd',
      disabled: false,
      busy: false,
      icon: 'none',
    });
    expect(lastAction().labelArgs).toBeUndefined();

    await activate(handle);

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Add more model providers')).toBeTruthy();
  });

  it('continues once something is really there to continue with', async () => {
    serve({ sources: [source({ id: 'src_zhipu', vendor: 'zhipu' })] });
    const { handle } = renderScreen();
    await settled();

    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionContinue'));
    await activate(handle);

    expect(navigated).toEqual(['assistants']);
  });

  it('offers the pending batch ahead of continuing, counted the way the dialog counts it', async () => {
    serve({ sources: [source({ id: 'src_zhipu', vendor: 'zhipu' })], scan: [CODEX_KEY, OPENCODE_KEY] });
    const { handle } = renderScreen();
    await settled();
    const user = userEvent.setup();

    // A source exists, so the standing offer is to continue.
    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionContinue'));

    await user.click(cardFor('openai'));

    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionImport'));
    expect(lastAction().labelArgs).toEqual({ count: 1 });
    await activate(handle);

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Migrate to Model Hub')).toBeTruthy();
    // Opening the takeover is not continuing: the report of what landed would be
    // lost behind a navigation.
    expect(navigated).toEqual([]);
  });
});

describe('ProvidersScreen — what an import leaves behind', () => {
  it('submits one batch, reports what landed and re-reads the machine', async () => {
    serve({ scan: [CODEX_KEY, OPENCODE_KEY, CLAUDE_SUBSCRIPTION] });
    const { handle } = renderScreen();
    await settled();
    const user = userEvent.setup();

    await user.click(cardFor('openai'));
    await user.click(cardFor('gemini'));
    await waitFor(() => expect(lastAction().labelArgs).toEqual({ count: 2 }));

    await activate(handle);
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /Start migration/ }));

    // One atomic batch, holding exactly the rows the count promised.
    await waitFor(() => expect(applied).toHaveLength(1));
    expect(applied[0]).toEqual([CODEX_KEY.id, OPENCODE_KEY.id]);
    expect(await screen.findByText('Migrated 2 API keys into Model Hub')).toBeTruthy();
    // The rescan is what retires the consent, so the footer stops offering a
    // batch the dialog would now refuse to build.
    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionContinue'));
    expect(modelsApi.listSources).toHaveBeenCalledTimes(2);
  });

  it('keeps the selection and asks to retry when the batch is refused', async () => {
    serve({ scan: [CODEX_KEY] });
    const { handle } = renderScreen();
    await settled();
    const user = userEvent.setup();

    await user.click(cardFor('openai'));
    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionImport'));
    vi.mocked(modelsApi.applyMigration)
      .mockRejectedValueOnce(new ApiCallError('migration_credentials_invalid', 'refused'));

    await activate(handle);
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: /Start migration/ }));

    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionRetryImport'));
    // Nothing landed, so nothing is retired: the same batch is still the offer.
    expect(lastAction().labelArgs).toEqual({ count: 1 });
    expect(cardFor('openai').getAttribute('aria-pressed')).toBe('true');

    await activate(handle);
    expect(await screen.findByRole('dialog')).toBeTruthy();
  });
});

describe('ProvidersScreen — the engine', () => {
  it('resumes the install the first recovery left undone, once', async () => {
    serve({ runtime: runtimeOf('not_installed') });
    const install = deferred<RuntimeDependency>();
    vi.mocked(modelsApi.installRuntime).mockReturnValue(install.promise);
    const { handle } = renderScreen({ runtimeRead: readyRegion(runtimeOf('not_installed')) });

    await waitFor(() => expect(gatewayCard().dataset.state).toBe('installing'));
    // Nothing else is possible while the engine is coming up, and the shell is
    // told so rather than left to infer it.
    expect(lastAction()).toMatchObject({
      labelKey: 'onboarding.providers.actionConnecting',
      disabled: true,
      busy: true,
      icon: 'spinner',
    });
    // A press that arrives anyway does nothing at all.
    await activate(handle);
    expect(navigated).toEqual([]);
    expect(screen.queryByRole('dialog')).toBeNull();

    await act(async () => { install.resolve(runtimeOf('not_started')); });

    await waitFor(() => expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(gatewayCard().dataset.state).not.toBe('installing'));
    expect(modelsApi.installRuntime).toHaveBeenCalledTimes(1);
    // An engine that just came up can answer reads that failed before it did.
    await waitFor(() => expect(modelsApi.listSources).toHaveBeenCalledTimes(2));
  });

  it('starts an installed engine even where installing one is unsupported', async () => {
    const stopped = { ...UNSUPPORTED, status: { ...UNSUPPORTED.status, health: 'not_started' as const } };
    serve({ runtime: stopped });
    renderScreen({ runtimeRead: readyRegion(stopped) });

    await waitFor(() => expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1));
    // Refusing to start a working engine on an installation technicality would
    // strand it.
    expect(modelsApi.installRuntime).not.toHaveBeenCalled();
  });

  it('says installation is not possible here rather than attempting it', async () => {
    serve({ runtime: UNSUPPORTED });
    renderScreen({ runtimeRead: readyRegion(UNSUPPORTED) });
    await settled();

    await waitFor(() => expect(gatewayCard().dataset.state).toBe('unsupported'));
    expect(modelsApi.installRuntime).not.toHaveBeenCalled();
    expect(modelsApi.startRuntime).not.toHaveBeenCalled();
    expect(within(gatewayCard()).getByRole('button', { name: 'Retry' })).toBeTruthy();
  });

  it('reports a failed step on the card it is about, and retries from there', async () => {
    serve({ runtime: runtimeOf('not_started') });
    vi.mocked(modelsApi.startRuntime).mockRejectedValueOnce(new ApiCallError('engine_down', 'down'));
    renderScreen({ runtimeRead: readyRegion(runtimeOf('not_started')) });

    await waitFor(() => expect(gatewayCard().dataset.state).toBe('failed'));
    expect(gatewayCard().dataset.failedStep).toBe('start');

    await userEvent.setup().click(within(gatewayCard()).getByRole('button', { name: 'Retry' }));

    expect(retrySetup).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(modelsApi.startRuntime).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(gatewayCard().dataset.state).not.toBe('failed'));
  });

  it('treats a machine with no CLI as nothing to adopt, not as a failure', async () => {
    serve({
      agents: [supply({ backend: 'codex', cli_present: false }), supply({ backend: 'opencode', cli_present: false })],
      runtime: runtimeOf('not_installed'),
    });
    renderScreen({ runtimeRead: readyRegion(runtimeOf('not_installed')) });
    await settled();

    // Presence is read fresh: a CLI installed while setup was open is exactly the
    // case a cached list would get wrong.
    await waitFor(() => expect(modelsApi.refreshAgentPresence).toHaveBeenCalled());
    expect(modelsApi.installRuntime).not.toHaveBeenCalled();
    await waitFor(() => expect(gatewayCard().dataset.state).toBe('idle'));
  });

  it('does nothing at all while the screen is hidden', async () => {
    serve({ runtime: runtimeOf('not_installed') });
    renderScreen({ active: false, runtimeRead: readyRegion(runtimeOf('not_installed')) });

    // Give every effect the chance a mounted-but-hidden screen would have.
    await act(async () => { await Promise.resolve(); });

    expect(modelsApi.scanMigration).not.toHaveBeenCalled();
    expect(modelsApi.listSources).not.toHaveBeenCalled();
    expect(modelsApi.installRuntime).not.toHaveBeenCalled();
    expect(modelsApi.startRuntime).not.toHaveBeenCalled();
    // No claim on the shell's action either: the hidden screen is not the one
    // the button belongs to.
    expect(actions).toEqual([]);
    expect(screen.queryByText(/API key to import/)).toBeNull();
  });

  it('waits rather than acting on a configuration that turned the gateway off', async () => {
    serve({ runtime: runtimeOf('not_installed', { enabled: false }) });
    renderScreen({ gatewayEnabled: false, runtimeRead: readyRegion(runtimeOf('not_installed', { enabled: false })) });

    await act(async () => { await Promise.resolve(); });

    // Setup never silently re-enables a disabled configuration, and never reads
    // the machine as if it had.
    expect(modelsApi.installRuntime).not.toHaveBeenCalled();
    expect(modelsApi.scanMigration).not.toHaveBeenCalled();
    expect(gatewayCard().dataset.state).toBe('idle');
  });
});
