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
import {
  beginRegionRead,
  readyRegion,
  unreadRegion,
  type RegionRead,
} from '@/components/settings/models/regionRead';
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

// The same host with an engine already up on it. Health and install admission are
// separate facts on purpose: the manifest has no asset for this platform, and the
// runtime running here arrived some other way. Reads and writes to it are fine; only
// something that would install one is not.
const UNSUPPORTED_RUNNING = runtimeOf('ok', {
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

// The API key in the same store as the sign-in above. The server migrates a
// backend whole, so this key cannot be taken without the subscription beside it —
// and setup's copy offers keys only. Found, and not this screen's to take.
const CLAUDE_KEY: MigrationItem = {
  id: 'mig_claude_key',
  backend: 'claude',
  kind: 'api_key',
  masked_detail: 'sk-…dd3c',
  masked_credential: 'sk-…dd3c',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
  vendor: 'anthropic',
  display_name: 'Anthropic',
};

// No presentation metadata at all: an older server, or a provider nothing names.
const OPENCODE_LEGACY: MigrationItem = {
  id: 'mig_opencode_legacy',
  backend: 'opencode',
  kind: 'opencode_provider',
  masked_detail: 'Self-hosted relay · sk-…abcd',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
};

/** The one sentence the migration feature has for a row it cannot take. */
const BLOCKED_SENTENCE = 'This credential cannot be imported.';

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
      {/* The shell's half of the anchor, not decoration: what is ancillary to the
          pair is portaled into this slot, so a host without it sees none of it. */}
      <div className="onboarding-step">
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
      <div className="onboarding-action-aside" data-setup-action-aside="" />
      </div>
    </I18nextProvider>
  );
};

const renderScreen = (options: ScreenOptions = {}) => {
  const handle = React.createRef<SetupScreenHandle | null>();
  const view = render(<Harness handle={handle} {...options} />);
  /** Change the shell's half of the contract without remounting: the wizard hiding
   *  this screen, or handing down a runtime read that moved. */
  const show = async (next: ScreenOptions) => {
    await act(async () => { view.rerender(<Harness handle={handle} {...options} {...next} />); });
  };
  return { ...view, handle, show };
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
    // The server proposed this row, so the card opens on that proposal.
    expect(detected.getAttribute('aria-pressed')).toBe('true');

    await user.click(detected);
    await waitFor(() => expect(cardFor('openai').getAttribute('aria-pressed')).toBe('false'));
    expect(cardFor('openai').querySelector('.setup-provider-check')).toBeNull();
    expect(cardFor('zhipuai').dataset.state).toBe('connected');
    expect(cardFor('zhipuai').querySelector('.setup-provider-check')).toBeTruthy();

    await user.click(cardFor('openai'));
    await waitFor(() => expect(cardFor('openai').getAttribute('aria-pressed')).toBe('true'));
    expect(cardFor('openai').querySelector('.setup-provider-check')).toBeTruthy();
    expect(cardFor('zhipuai').querySelector('.setup-provider-check')).toBeTruthy();
  });

  it('opens on the rows the server proposed, and leaves the ones it did not', async () => {
    // The shipped takeover opens ticked on exactly these rows. A screen that ignored
    // them would show the same scan with a different answer, and someone who trusted
    // the cards would import less than the dialog behind them offered.
    serve({ scan: [CODEX_KEY, { ...OPENCODE_KEY, selected: false }] });
    renderScreen();
    await settled();

    await waitFor(() => expect(cardFor('openai').getAttribute('aria-pressed')).toBe('true'));
    expect(cardFor('gemini').getAttribute('aria-pressed')).toBe('false');
    expect(lastAction()).toMatchObject({ labelKey: 'onboarding.providers.actionImport', labelArgs: { count: 1 } });
  });

  it('says so when a key is stored but nothing has checked it yet', async () => {
    // `save_unverified: true` is what the key form sends, so a card can be connected
    // with nothing having confirmed the key answers. Settings discloses that; a card
    // that did not would report it as a provider already supplying models.
    serve({
      sources: [source({
        id: 'src_pending',
        vendor: 'openai',
        state: { status: 'standby' },
        verification_pending: 'vp_fixture',
      })],
    });
    renderScreen();
    await settled();

    await waitFor(() => expect(cardFor('openai').dataset.pending).toBe('true'));
    expect(within(cardFor('openai')).getByText(/Saved/)).toBeTruthy();
    expect(cardFor('openai').getAttribute('aria-label')).toContain('Saved');
  });

  it('names a subscription by its sign-in, not by a mask it cannot have', async () => {
    // A subscription has no key to mask by construction. A card describing itself by
    // its mask alone would fall through to the offer an empty card makes and invite
    // a key for a provider that is already connected — and the card is disabled, so
    // that invitation is to press something that does nothing. What it says instead
    // is what Settings says about the same source.
    serve({
      sources: [source({
        id: 'src_claude',
        vendor: 'anthropic',
        kind: 'subscription',
        account_label: 'max@example.com',
      })],
    });
    renderScreen();
    await settled();

    await waitFor(() => expect(cardFor('anthropic').dataset.state).toBe('connected'));
    expect(within(cardFor('anthropic')).getByText('Subscription · max@example.com')).toBeTruthy();
    expect(within(cardFor('anthropic')).queryByText(/Add a .* API Key/)).toBeNull();
  });

  it('names a connected API key by its kind when the server sent no mask', async () => {
    serve({ sources: [source({ id: 'src_key', vendor: 'openai' })] });
    renderScreen();
    await settled();

    await waitFor(() => expect(cardFor('openai').dataset.state).toBe('connected'));
    // No account either: the line is the kind alone rather than a stray separator.
    expect(within(cardFor('openai')).getByText('API key')).toBeTruthy();
  });

  it('says a source was written even when the read that would show it fails', async () => {
    serve();
    renderScreen();
    await settled();
    const user = userEvent.setup();

    vi.spyOn(modelsApi, 'createApiKeySource').mockResolvedValue({
      source: source({ id: 'src_new', vendor: 'custom' }),
      added_to: [],
      adopted_by: [],
    });
    vi.mocked(modelsApi.listSources).mockRejectedValue(new Error('offline'));

    await user.click(cards()[2]);
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'API Key' }));
    await user.type(within(dialog).getByLabelText('Base URL'), 'https://api.example/v1');
    await user.type(within(dialog).getByLabelText('API key'), 'sk-live-1');
    await user.click(within(dialog).getByRole('button', { name: 'Add' }));

    // The write landed; the read that would show it did not. Keeping the old list
    // silently would report a provider that exists as one that does not.
    await waitFor(() => expect(summary()?.dataset.tone).toBe('error'));
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

  it('offers a way back when the machine cannot be read, and asks again only for the read that failed', async () => {
    serve();
    vi.mocked(modelsApi.scanMigration).mockRejectedValue(new Error('offline'));
    renderScreen();

    await waitFor(() => expect(summary()?.dataset.tone).toBe('error'));
    const failed = summary();
    if (!failed) throw new Error('no summary');
    await userEvent.setup().click(within(failed).getByRole('button', { name: 'Retry' }));

    // What broke the sentence is a read, so a read is what this asks for again. The
    // engine is not what failed: asking the shell to resume its bootstrap, or arming an
    // install or a start from here, would be a mutation nobody asked for.
    await waitFor(() => expect(modelsApi.scanMigration).toHaveBeenCalledTimes(2));
    expect(retrySetup).not.toHaveBeenCalled();
    expect(modelsApi.installRuntime).not.toHaveBeenCalled();
    expect(modelsApi.startRuntime).not.toHaveBeenCalled();
  });

  it('re-reads supply for a new observation, and not for the same one re-reported', async () => {
    // The shell owns the runtime region and hands one down on every render it makes.
    // What this screen takes its inventory against is the machine that answers it, not
    // the object that described the machine — a refresh that confirmed nothing changed,
    // or a parent that rebuilt its props, is the same observation said twice. Reading
    // again for one of those does not merely waste a request: every answer publishes
    // selection state, which is another render, which is another carrier.
    serve();
    const { show } = renderScreen({ runtimeRead: readyRegion(runtimeOf('ok')) });
    await settled();
    expect(modelsApi.scanMigration).toHaveBeenCalledTimes(1);

    await show({ runtimeRead: readyRegion(runtimeOf('ok')) });
    expect(modelsApi.scanMigration).toHaveBeenCalledTimes(1);

    // A genuinely different machine is the other half, and the half that matters: the
    // engine that could not answer the first read is why it failed, and the sequence
    // that fixes one publishes into the shell's region rather than into this screen.
    await show({
      runtimeRead: readyRegion(runtimeOf('ok', {
        status: { verified: true, health: 'ok', installed_version: '2.0.0' },
      })),
    });
    await waitFor(() => expect(modelsApi.scanMigration).toHaveBeenCalledTimes(2));
    // A read is all it is. Nothing here authorizes a mutation of the engine.
    expect(modelsApi.installRuntime).not.toHaveBeenCalled();
    expect(modelsApi.startRuntime).not.toHaveBeenCalled();
  });

  it('keeps the sources that did arrive when the scan beside them fails', async () => {
    // Two reads, two questions. A scan that fails says nothing about the sources,
    // and throwing away a list that did arrive would report providers that exist as
    // providers that do not — leaving the action offering 「添加」 on a machine that
    // already has credentials.
    serve({ sources: [source({ id: 'src_zhipu', vendor: 'zhipuai' })] });
    vi.mocked(modelsApi.scanMigration).mockRejectedValue(new Error('offline'));
    renderScreen();
    await settled();

    await waitFor(() => expect(cardFor('zhipuai').dataset.state).toBe('connected'));
    // Said, not hidden: half the machine could not be read.
    expect(summary()?.dataset.tone).toBe('error');
    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionContinue'));
  });

  it('keeps the scan that did arrive when the source read beside it fails', async () => {
    serve({ scan: [CODEX_KEY] });
    vi.mocked(modelsApi.listSources).mockRejectedValue(new Error('offline'));
    renderScreen();
    await settled();

    await waitFor(() => expect(cardFor('openai').dataset.state).toBe('detected'));
    expect(summary()?.dataset.tone).toBe('error');
  });

  it('says it is still looking before the first inventory read lands', async () => {
    // Not 「添加」. An empty list and a list nobody has read yet are the same
    // `Source[]`, and the difference is the whole question the button is answering.
    serve();
    const read = deferred<Source[]>();
    vi.mocked(modelsApi.listSources).mockReturnValue(read.promise);
    renderScreen();

    await waitFor(() => expect(lastAction()).toBeTruthy());
    expect(lastAction()).toMatchObject({
      labelKey: 'onboarding.providers.actionChecking',
      disabled: true,
      busy: true,
    });

    await act(async () => { read.resolve([]); });
    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionAdd'));
  });

  it('offers to ask again, not to add, when the inventory could not be read', async () => {
    // 「添加」 against an unread inventory is the same button on a machine that
    // already holds the credential as on one that holds nothing — which is how a
    // second copy of an existing key gets written.
    serve();
    vi.mocked(modelsApi.listSources).mockRejectedValue(new Error('offline'));
    const { handle } = renderScreen();
    await settled();

    await waitFor(() => expect(lastAction().labelKey).toBe('common.retry'));
    expect(lastAction()).toMatchObject({ disabled: false, busy: false, icon: 'none' });

    vi.mocked(modelsApi.listSources)
      .mockImplementation(async () => [source({ id: 'src_zhipu', vendor: 'zhipuai' })]);
    await activate(handle);

    // Asking again is the whole action, and the answer it gets is what the screen
    // then offers. No dialog: nothing was added here.
    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionContinue'));
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(cardFor('zhipuai').dataset.state).toBe('connected'));
  });

  it('keeps a detected key reachable behind the connected card of the same brand', async () => {
    // The stage draws one OpenAI card, and it draws the fact: a source exists. The
    // native store beside it still holds a DIFFERENT key — the scan never reads the
    // Hub's inventory, so it cannot be a duplicate of the connected one — and the
    // capsule counts it. A row the capsule counts and no pane can act on is the
    // contradiction this pins: Add more is where it stays reachable.
    serve({
      sources: [source({ id: 'src_openai', vendor: 'openai', masked_credential: 'sk-…1111' })],
      scan: [CODEX_KEY],
    });
    renderScreen();
    await settled();
    const user = userEvent.setup();

    await waitFor(() => expect(cardFor('openai').dataset.state).toBe('connected'));
    expect(cards().filter((card) => card.dataset.state === 'detected')).toHaveLength(0);
    expect(await screen.findByText('Found 1 API key to import into Model Hub')).toBeTruthy();

    const addMore = cards().find((card) => card.dataset.state === 'add');
    await user.click(addMore as HTMLElement);
    const dialog = await screen.findByRole('dialog');

    // Listed, under its own masked credential, and pressable — not marked 「已添加」
    // because a brand it shares with a source is not the key that source holds.
    const row = within(dialog).getByRole('button', { name: /Select existing OpenAI/ });
    expect(row.dataset.state).toBe('detected');
    expect((row as HTMLButtonElement).disabled).toBe(false);
    expect(within(row).getByText(/sk-…9f21/)).toBeTruthy();
  });

  it('still offers a take-over the scan found while the inventory beside it is unread', async () => {
    // Two reads, two questions. Taking over a key the scan found does not depend on
    // knowing what else is already there.
    serve({ scan: [CODEX_KEY] });
    vi.mocked(modelsApi.listSources).mockRejectedValue(new Error('offline'));
    renderScreen();
    await settled();

    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionImport'));
    expect(lastAction().labelArgs).toEqual({ count: 1 });
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

// A scan answers two different questions: what is on this machine, and what this
// entry may take over. They are two projections of one grouping, not two rules —
// and every case here is a way a screen that conflated them would lie. Hiding a
// found key behind an empty 「add Anthropic」 invitation is the loud version; a
// capsule counting a key its own review would refuse is the quiet one.
describe('ProvidersScreen — what it found but may not take', () => {
  it('keeps a blocked credential on the stage, with its reason and no way to consent', async () => {
    serve({ scan: [CLAUDE_KEY, CLAUDE_SUBSCRIPTION] });
    renderScreen();
    await settled();
    const user = userEvent.setup();

    const card = cardFor('anthropic');
    expect(card.dataset.state).toBe('detected');
    expect(card.dataset.blocked).toBe('true');
    expect(within(card).getByText(new RegExp(BLOCKED_SENTENCE))).toBeTruthy();
    // Not a toggle that happens to be off, and not an invitation to add a key for a
    // provider whose key is right there on the card.
    expect((card as HTMLButtonElement).disabled).toBe(true);
    expect(card.getAttribute('aria-pressed')).toBeNull();
    // Still the found key, said in the card and in what a screen reader reads: the
    // mask is the evidence that this is a detection and not an offer.
    expect(card.getAttribute('aria-label')).toContain('sk-…dd3c');

    await user.click(card);
    expect(screen.queryByRole('dialog')).toBeNull();
    // Nothing to import, so the footer asks for a provider instead of offering a
    // batch, and the capsule — whose whole sentence is a count — says nothing.
    expect(lastAction().labelKey).toBe('onboarding.providers.actionAdd');
    expect(screen.queryByText(/to import into Model Hub/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Review migration' })).toBeNull();
  });

  it('counts the takeable group beside it, and only that one', async () => {
    serve({ scan: [CLAUDE_KEY, CLAUDE_SUBSCRIPTION, CODEX_KEY] });
    renderScreen();
    await settled();
    const user = userEvent.setup();

    // What can be done comes first, and both stay on the stage. The second slot has
    // to be the Claude key that was found — an empty 「add Anthropic」 card would fill
    // the same position from the shortlist and read as if nothing had been found.
    await waitFor(() => expect(cards().map((card) => [card.dataset.provider, card.dataset.state]))
      .toEqual([['openai', 'detected'], ['anthropic', 'detected'], [undefined, 'add']]));
    expect(cardFor('anthropic').dataset.blocked).toBe('true');
    expect(cardFor('openai').getAttribute('aria-pressed')).toBe('true');

    // One number, three readings: the card that is ticked, the sentence, the footer.
    expect(await screen.findByText('Found 1 API key to import into Model Hub')).toBeTruthy();
    expect(lastAction()).toMatchObject({
      labelKey: 'onboarding.providers.actionImport',
      labelArgs: { count: 1 },
    });

    // Pressing the blocked card cannot borrow the consent of the one beside it, and
    // does not open the add dialog an empty card in that slot would have opened.
    await user.click(cardFor('anthropic'));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(cardFor('anthropic').getAttribute('aria-pressed')).toBeNull();
    expect(lastAction().labelArgs).toEqual({ count: 1 });
  });

  it('submits the batch it counted, and keeps the blocked card after the rescan', async () => {
    serve({ scan: [CLAUDE_KEY, CLAUDE_SUBSCRIPTION, CODEX_KEY] });
    const { handle } = renderScreen();
    await settled();
    const user = userEvent.setup();

    await waitFor(() => expect(lastAction().labelArgs).toEqual({ count: 1 }));
    await activate(handle);
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /Start migration/ }));

    // No half group and no sign-in: one row, from the one group that consented.
    await waitFor(() => expect(applied).toHaveLength(1));
    expect(applied[0]).toEqual([CODEX_KEY.id]);
    expect(await screen.findByText('Migrated 1 API key into Model Hub')).toBeTruthy();

    // The rescan still finds the Claude store, so the card is still there, still
    // saying why. Settings is where it is resolved; forgetting it is not.
    await waitFor(() => expect(modelsApi.scanMigration).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(cardFor('anthropic').dataset.blocked).toBe('true'));
    expect(within(cardFor('anthropic')).getByText(new RegExp(BLOCKED_SENTENCE))).toBeTruthy();
    // And nothing left to offer: the receipt is the whole sentence now.
    expect(screen.queryByRole('button', { name: 'Review migration' })).toBeNull();
  });

  it('names a credential the server did not name, rather than guessing a brand', async () => {
    serve({ scan: [OPENCODE_LEGACY] });
    renderScreen();
    await settled();

    // Its own slot, keyed by the row: a card named 「OpenCode」 would name the file
    // it was found in, and a brand mark would name a provider nobody identified.
    const card = cardFor(OPENCODE_LEGACY.id);
    expect(card.dataset.state).toBe('detected');
    expect(within(card).getByText('Self-hosted relay · sk-…abcd')).toBeTruthy();
    expect(card.querySelector('.setup-provider-logo svg')).toBeTruthy();
    // Unnamed is not blocked: it is still a key this entry may take over.
    expect(card.getAttribute('aria-pressed')).toBe('true');
    expect(card.dataset.blocked).toBeUndefined();
    expect(await screen.findByText('Found 1 API key to import into Model Hub')).toBeTruthy();
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

    // A source already holds the first slot, so the stage has room to draw one of
    // the two proposed groups. The count is the batch the dialog would submit, not
    // the number of cards that fit.
    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionImport'));
    expect(lastAction().labelArgs).toEqual({ count: 2 });
    expect(cards().filter((card) => card.dataset.state === 'detected')).toHaveLength(1);

    // Dropping the group that is drawn leaves the one that is not: still a batch,
    // one row smaller.
    await user.click(cardFor('openai'));
    await waitFor(() => expect(lastAction().labelArgs).toEqual({ count: 1 }));
    expect(lastAction().labelKey).toBe('onboarding.providers.actionImport');
    await activate(handle);

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Migrate to Model Hub')).toBeTruthy();
    // Opening the takeover is not continuing: the report of what landed would be
    // lost behind a navigation.
    expect(navigated).toEqual([]);
  });
});

describe('ProvidersScreen — the way on when nothing is connected', () => {
  it('states a way on beside the take-over it found, and only navigates', async () => {
    serve({ scan: [CLAUDE_KEY] });
    renderScreen();
    await settled();

    // The take-over keeps the footer: whoever came here to connect something is
    // still offered the thing they came for. The way on is beside it, not instead.
    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionImport'));
    const onward = screen.getByRole('button', { name: 'Continue to assistants' });
    expect((onward as HTMLButtonElement).disabled).toBe(false);

    await userEvent.setup().click(onward);

    // Navigating is the whole action. Nothing was added, taken over or installed
    // on the way out, and the review that was on offer is still only on offer.
    expect(navigated).toEqual(['assistants']);
    expect(server.sources).toEqual([]);
    expect(modelsApi.applyMigration).not.toHaveBeenCalled();
    expect(modelsApi.installRuntime).not.toHaveBeenCalled();
    expect(modelsApi.startRuntime).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('leaves the single action alone once a source is really there', async () => {
    serve({ sources: [source({ id: 'src_zhipu', vendor: 'zhipu' })] });
    renderScreen();
    await settled();

    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionContinue'));
    expect(screen.queryByRole('button', { name: 'Continue to assistants' })).toBeNull();
  });

  it('takes the way on back when the shell hides the screen, and returns it on re-entry', async () => {
    serve();
    const { show } = renderScreen();
    await settled();
    expect(screen.getByRole('button', { name: 'Continue to assistants' })).toBeTruthy();

    // It leaves the screen root to reach the shell's slot, so nothing else would
    // take it out of reach of a reader on the step after this one.
    await show({ active: false });
    expect(screen.queryByRole('button', { name: 'Continue to assistants' })).toBeNull();

    await show({ active: true });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Continue to assistants' })).toBeTruthy());
    expect(navigated).toEqual([]);
  });

  it('says nothing about a way on while the inventory is unread, which is not an empty one', async () => {
    serve();
    vi.mocked(modelsApi.listSources).mockRejectedValue(new Error('offline'));
    renderScreen();
    await settled();

    await waitFor(() => expect(lastAction().labelKey).toBe('common.retry'));
    expect(screen.queryByRole('button', { name: 'Continue to assistants' })).toBeNull();
  });
});

describe('ProvidersScreen — what an import leaves behind', () => {
  it('submits one batch, reports what landed and re-reads the machine', async () => {
    serve({ scan: [CODEX_KEY, OPENCODE_KEY, CLAUDE_SUBSCRIPTION] });
    const { handle } = renderScreen();
    await settled();
    const user = userEvent.setup();

    // Both proposed groups arrive consented to; the third is blocked and cannot be.
    await waitFor(() => expect(lastAction().labelArgs).toEqual({ count: 2 }));

    await activate(handle);
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /Start migration/ }));

    // One atomic batch, holding exactly the rows the count promised.
    await waitFor(() => expect(applied).toHaveLength(1));
    expect(applied[0]).toEqual([CODEX_KEY.id, OPENCODE_KEY.id]);
    expect(await screen.findByText('Migrated 2 API keys into Model Hub')).toBeTruthy();
    // The batch is spent in the tick it landed, not a round trip later: until the
    // rescan answers, the old scan still names rows that are now imported.
    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionContinue'));
    expect(modelsApi.listSources).toHaveBeenCalledTimes(2);
  });

  it('never re-offers a spent batch while the rescan is still in flight', async () => {
    serve({ scan: [CODEX_KEY] });
    const { handle } = renderScreen();
    await settled();
    const user = userEvent.setup();

    // Hold the rescan open, so the window between the apply and its answer is the
    // whole of what this case observes.
    const rescan = deferred<{ items: MigrationItem[] }>();
    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionImport'));
    vi.mocked(modelsApi.scanMigration).mockReturnValueOnce(rescan.promise);

    await activate(handle);
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: /Start migration/ }));

    await waitFor(() => expect(applied).toHaveLength(1));
    // The capsule, the cards and the action all read the same emptied scan.
    await waitFor(() => expect(lastAction().labelKey).not.toBe('onboarding.providers.actionImport'));
    expect(screen.queryByText(/API key to import/)).toBeNull();

    await act(async () => { rescan.resolve({ items: [] }); });
    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionContinue'));
  });

  it('spends a terminally rejected batch before the rescan, so a failed reread cannot resubmit it', async () => {
    serve({ scan: [CODEX_KEY] });
    const { handle } = renderScreen();
    await settled();
    const user = userEvent.setup();

    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionImport'));
    expect(lastAction().labelArgs).toEqual({ count: 1 });

    vi.mocked(modelsApi.applyMigration)
      .mockRejectedValueOnce(new ApiCallError('migration_credentials_invalid', 'refused'));
    vi.mocked(modelsApi.scanMigration).mockRejectedValue(new Error('offline'));

    await activate(handle);
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: /Start migration/ }));

    // Spent in the same tick as the rejection: the refused ids are gone before the
    // rescan answers, the count does not move because nothing landed, and a failed
    // reread has nothing left to revive.
    await waitFor(() => expect(modelsApi.scanMigration).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => {
      expect(lastAction().labelKey).not.toBe('onboarding.providers.actionImport');
      expect(lastAction().labelKey).not.toBe('onboarding.providers.actionRetryImport');
    });
    expect(lastAction().labelArgs).toBeUndefined();
    expect(cardFor('openai').dataset.state).toBe('empty');
    expect(screen.queryByText(/Migrated/)).toBeNull();
    expect(screen.queryByText(/API key to import/)).toBeNull();
    expect(vi.mocked(modelsApi.applyMigration).mock.calls[0][0]).toEqual([CODEX_KEY.id]);

    await activate(handle);
    const dialog = screen.queryByRole('dialog');
    if (dialog) {
      expect(within(dialog).queryByRole('button', { name: /Start migration/ })).toBeNull();
    }
    expect(modelsApi.applyMigration).toHaveBeenCalledTimes(1);
  });

  it('refuses a batch the engine behind it can no longer take, without taking the review away', async () => {
    // A take-over migrates keys INTO the Hub. An engine that stopped being readable
    // while the review was open has nothing to migrate them into — and this is a write
    // like any other, so it is refused at the submit and not only at the door.
    serve({ scan: [CODEX_KEY] });
    const { handle, show } = renderScreen();
    await settled();
    const user = userEvent.setup();

    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionImport'));
    await activate(handle);
    await screen.findByRole('dialog');

    await show({ runtimeRead: unreadRegion<RuntimeDependency>() });
    const start = () => within(screen.getByRole('dialog')).getByRole<HTMLButtonElement>(
      'button', { name: /Start migration/ },
    );
    expect(start().disabled).toBe(true);
    await user.click(start());
    expect(modelsApi.applyMigration).not.toHaveBeenCalled();

    // What the person was reading is still there, still says what it found, and can
    // still be left. Closing it for them would be a different answer than refusing
    // the write, and would lose the one place the offer is spelled out.
    expect(within(screen.getByRole('dialog')).getByText(CODEX_KEY.masked_detail!)).toBeTruthy();
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Not now' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    // Nothing was consumed: the offer is exactly the one it was.
    expect(lastAction()).toMatchObject({ labelKey: 'onboarding.providers.actionImport', labelArgs: { count: 1 } });
  });

  it('refuses a take-over the host cannot install an engine for, and still writes a key', async () => {
    // A running engine on a host with no published runtime asset for its platform.
    // The take-over is the one write on this screen with an install still waiting
    // behind it: `apply_native_migration` ensures the runtime dependency before it
    // touches a credential, and that ensure refuses an unsupported platform ahead of
    // the branch that would have reused the engine already up — so the batch comes
    // back 422 however healthy the engine is.
    serve({ runtime: UNSUPPORTED_RUNNING, scan: [CODEX_KEY] });
    const { handle } = renderScreen({ runtimeRead: readyRegion(UNSUPPORTED_RUNNING) });
    await settled();
    const user = userEvent.setup();

    // The offer is still made and the review still opens. What the scan found is
    // worth reading, and Settings is where it can be acted on.
    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionImport'));
    await activate(handle);
    const review = await screen.findByRole('dialog');
    expect(within(review).getByText(CODEX_KEY.masked_detail!)).toBeTruthy();
    const start = within(review).getByRole<HTMLButtonElement>('button', { name: /Start migration/ });
    expect(start.disabled).toBe(true);
    await user.click(start);
    expect(modelsApi.applyMigration).not.toHaveBeenCalled();
    await user.click(within(review).getByRole('button', { name: 'Not now' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    // And nothing else is narrowed. A key is a write to an engine that is up, which
    // this one is; refusing it because a fresh install would fail would strand a
    // working machine on a boundary that is not about writing.
    vi.spyOn(modelsApi, 'createApiKeySource').mockResolvedValue({
      source: source({ id: 'src_new', vendor: 'custom' }),
      added_to: [],
      adopted_by: [],
    });
    await user.click(cards()[2]);
    const add = await screen.findByRole('dialog');
    await user.click(within(add).getByRole('button', { name: 'API Key' }));
    await user.type(within(add).getByLabelText('Base URL'), 'https://api.example/v1');
    await user.type(within(add).getByLabelText('API key'), 'sk-live-1');
    await user.click(within(add).getByRole('button', { name: 'Add' }));

    await waitFor(() => expect(modelsApi.createApiKeySource).toHaveBeenCalledTimes(1));
  });

  it('settles a batch it had already sent, whatever the screen admits by the time it answers', async () => {
    serve({ scan: [CODEX_KEY] });
    const gate = deferred<void>();
    // The server's own behaviour, held open at the wire: the batch is on its way and
    // the answer is what this case gets to choose the moment for.
    vi.mocked(modelsApi.applyMigration).mockImplementationOnce(async (ids) => {
      applied.push([...ids]);
      await gate.promise;
      server.scan = server.scan.filter((row) => !ids.includes(row.id));
      server.sources = [...server.sources, source({ id: 'src_mig_codex', vendor: 'openai' })];
      return { applied: ids.length, sources: [] };
    });
    const { handle, show } = renderScreen();
    await settled();
    const user = userEvent.setup();

    await waitFor(() => expect(lastAction().labelKey).toBe('onboarding.providers.actionImport'));
    await activate(handle);
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: /Start migration/ }));
    await waitFor(() => expect(applied).toHaveLength(1));

    // The engine goes unreadable while that batch is in flight. It is already on the
    // server's side of the wire: permission governs the next write, never the delivery
    // of one that happened, and dropping the report would leave keys migrated with
    // nothing on screen saying so.
    await show({ runtimeRead: unreadRegion<RuntimeDependency>() });
    await act(async () => { gate.resolve(); });

    expect(await screen.findByText('Migrated 1 API key into Model Hub')).toBeTruthy();
    expect(modelsApi.applyMigration).toHaveBeenCalledTimes(1);
    // Reported once, counted once — and a screen that no longer admits a write does
    // not offer the spent batch again either.
    await waitFor(() => expect(lastAction().labelKey).not.toBe('onboarding.providers.actionImport'));
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
    // On its own a recheck is a button that will keep saying no. The guide is the
    // other half of the answer: what would have to change for it to say yes — so it
    // is the installation page the contract names, not the docs root, which leaves
    // someone to find that page themselves from the one screen they cannot get past.
    const guide = within(gatewayCard()).getByRole('link', { name: 'View installation guide' });
    expect(guide.getAttribute('href'))
      .toBe('https://github.com/avibe-bot/avibe/blob/master/docs/INSTALL_FOR_AI.md');
    expect(guide.getAttribute('rel')).toContain('noopener');
  });

  it('sends a Chinese reader to the guide that is written in Chinese', async () => {
    // The guide ships a translated counterpart. Handing someone reading Chinese the
    // English file is a worse answer than the one above.
    serve({ runtime: UNSUPPORTED });
    await act(async () => { await i18n.changeLanguage('zh'); });
    try {
      renderScreen({ runtimeRead: readyRegion(UNSUPPORTED) });
      await settled();

      await waitFor(() => expect(gatewayCard().dataset.state).toBe('unsupported'));
      const guide = within(gatewayCard()).getByRole('link', { name: '查看安装指南' });
      expect(guide.getAttribute('href'))
        .toBe('https://github.com/avibe-bot/avibe/blob/master/docs/INSTALL_FOR_AI_ZH.md');
    } finally {
      await act(async () => { await i18n.changeLanguage('en'); });
    }
  });

  it('offers no guide for a failure a guide would not explain', async () => {
    serve({ runtime: runtimeOf('not_started') });
    vi.mocked(modelsApi.startRuntime).mockRejectedValue(new ApiCallError('engine_down', 'down'));
    renderScreen({ runtimeRead: readyRegion(runtimeOf('not_started')) });

    await waitFor(() => expect(gatewayCard().dataset.state).toBe('failed'));
    // The engine is installable here; it did not start. Pointing at installation
    // instructions would send someone to fix something that is not broken.
    expect(within(gatewayCard()).queryByRole('link')).toBeNull();
    expect(within(gatewayCard()).getByRole('button', { name: 'Retry' })).toBeTruthy();
  });

  it('reports a failed step on the card it is about, and retries it on the read that answers', async () => {
    serve({ runtime: runtimeOf('not_started') });
    vi.mocked(modelsApi.startRuntime).mockRejectedValueOnce(new ApiCallError('engine_down', 'down'));
    const { show } = renderScreen({ runtimeRead: readyRegion(runtimeOf('not_started')) });

    await waitFor(() => expect(gatewayCard().dataset.state).toBe('failed'));
    expect(gatewayCard().dataset.failedStep).toBe('start');
    // A failed start moved supervisor health too. The shell's read still describes
    // the health the engine had before the attempt, so it is asked again here.
    await waitFor(() => expect(retrySetup).toHaveBeenCalledTimes(1));

    await userEvent.setup().click(within(gatewayCard()).getByRole('button', { name: 'Retry' }));

    // The press asks the shell for the read and stops there. Starting a second attempt
    // now would run it against the only read a press can see — the one the failure was
    // read from — so the failure stays on the card until something actually changes.
    expect(retrySetup).toHaveBeenCalledTimes(2);
    expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1);
    expect(gatewayCard().dataset.state).toBe('failed');

    // The shell's read STARTS. This is what it really hands down first: the previous
    // value degraded to 「refreshing」, and a configuration back to unknown while it is
    // re-read. None of it is an answer, and a request spent here is a request lost —
    // the read lands a moment later saying the engine is stopped, and nothing starts it.
    await show({
      capability: 'pending',
      gatewayEnabled: null,
      runtimeRead: beginRegionRead(readyRegion(runtimeOf('not_started'))),
    });
    expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1);
    expect(gatewayCard().dataset.state).toBe('failed');

    // The shell answers. The answer is what re-arms the attempt.
    await show({
      capability: 'enabled',
      gatewayEnabled: true,
      runtimeRead: readyRegion(runtimeOf('not_started')),
    });

    await waitFor(() => expect(modelsApi.startRuntime).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(gatewayCard().dataset.state).not.toBe('failed'));
    // Three reads, not two: the press asked for one, and the engine moving asked for
    // another. That last one is the only thing that turns the card and the footer
    // around — the attempt's own success is not the machine answering.
    await waitFor(() => expect(retrySetup).toHaveBeenCalledTimes(3));
  });

  it('stays retryable when the answer still calls for a resume after a spent attempt', async () => {
    // The local attempt succeeded — the engine started, there was nothing to adopt —
    // and the read that followed it still says stopped. That read is the authority
    // here, and 「my attempt did not fail」 is not evidence against it. With the
    // attempt already spent for this demand, an idle card leaves nothing on the
    // screen that could start the engine while Continue stays blocked for a reason
    // the card does not give.
    serve({ runtime: runtimeOf('not_started') });
    const { show } = renderScreen({ runtimeRead: readyRegion(runtimeOf('not_started')) });

    await waitFor(() => expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1));
    // The attempt asks the shell for the read that would turn the card around.
    await waitFor(() => expect(retrySetup).toHaveBeenCalledTimes(1));

    // And the shell's answer is 「still stopped」.
    await show({ runtimeRead: readyRegion(runtimeOf('not_started')) });

    await waitFor(() => expect(gatewayCard().dataset.state).toBe('failed'));
    // Retryable, not retried: a demand this screen has already attempted once must
    // not re-arm itself, or a machine the engine keeps dying on becomes a loop of
    // installs nobody asked for.
    expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1);
    // No step is named, because nothing failed. What the card reports is that the
    // engine is not ready and that asking again is possible.
    expect(gatewayCard().dataset.failedStep).toBeUndefined();

    await userEvent.setup().click(within(gatewayCard()).getByRole('button', { name: 'Retry' }));

    // Same rule as a failed attempt: the press asks for a read, and only the answer
    // arms anything.
    expect(retrySetup).toHaveBeenCalledTimes(2);
    expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1);

    // And the card the press was made on is still there while that read is in
    // flight. This is the state the shell really hands down first — the previous
    // value degraded to 「refreshing」 — and a verdict recomputed from it would take
    // the Retry away from under the person who just pressed it and replace it with
    // an idle card claiming the engine is fine.
    await show({ runtimeRead: beginRegionRead(readyRegion(runtimeOf('not_started'))) });
    expect(gatewayCard().dataset.state).toBe('failed');
    expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1);

    await show({ runtimeRead: readyRegion(runtimeOf('not_started')) });
    await waitFor(() => expect(modelsApi.startRuntime).toHaveBeenCalledTimes(2));
  });

  it('admits one engine mutation at a time, whatever else on the screen has failed', async () => {
    serve({ runtime: runtimeOf('not_installed') });
    const install = deferred<RuntimeDependency>();
    vi.mocked(modelsApi.installRuntime).mockReturnValue(install.promise);
    vi.mocked(modelsApi.scanMigration).mockRejectedValue(new Error('offline'));
    renderScreen({ runtimeRead: readyRegion(runtimeOf('not_installed')) });

    await waitFor(() => expect(gatewayCard().dataset.state).toBe('installing'));
    await waitFor(() => expect(summary()?.dataset.tone).toBe('error'));
    const user = userEvent.setup();
    const retry = () => within(summary()!).getByRole('button', { name: 'Retry' });
    await user.click(retry());
    await user.click(retry());

    // The install is unsettled and still owns the engine. The broken sentence has its
    // own recovery, and pressing it — twice — reaches the reads and nothing else: a
    // second install over one the server is still performing is not a retry.
    await waitFor(() => expect(modelsApi.scanMigration).toHaveBeenCalledTimes(3));
    expect(modelsApi.installRuntime).toHaveBeenCalledTimes(1);
    expect(modelsApi.startRuntime).not.toHaveBeenCalled();

    // Nor can a write start around the side of the footer: every add control obeys the
    // same admission the footer publishes.
    await user.click(cards()[cards().length - 1]);
    expect(screen.queryByRole('dialog')).toBeNull();

    await act(async () => { install.resolve(runtimeOf('not_started')); });

    await waitFor(() => expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1));
    expect(modelsApi.installRuntime).toHaveBeenCalledTimes(1);
  });

  it('cannot resume a gateway the answer says is off', async () => {
    serve({ runtime: runtimeOf('not_started') });
    vi.mocked(modelsApi.startRuntime).mockRejectedValue(new ApiCallError('engine_down', 'down'));
    const { show } = renderScreen({ runtimeRead: readyRegion(runtimeOf('not_started')) });

    await waitFor(() => expect(gatewayCard().dataset.state).toBe('failed'));
    await userEvent.setup().click(within(gatewayCard()).getByRole('button', { name: 'Retry' }));

    // The answer comes back with the Hub turned off. A retry asks for the current
    // configuration; it carries no authorization of its own, and a stopped intent is
    // not something this screen may start around.
    await show({ gatewayEnabled: false, runtimeRead: readyRegion(runtimeOf('not_started')) });
    expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1);
    expect(gatewayCard().dataset.state).toBe('failed');

    // Enabled again later is a new answer, not a queued press.
    await show({ gatewayEnabled: true, runtimeRead: readyRegion(runtimeOf('not_started')) });
    expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1);
  });

  it('refuses a write against an engine nobody can read, however much else is known', async () => {
    serve();
    const { handle, show } = renderScreen();
    await settled();
    // Awaited, not read once: the scan and the inventory are two reads, and the action
    // this case starts from is the one published after both have answered.
    await waitFor(() => expect(lastAction())
      .toMatchObject({ labelKey: 'onboarding.providers.actionAdd', disabled: false }));

    // The inventory is still an answer and the stage still draws it. What stopped
    // being known is the engine the add would be written to — and a write needs that,
    // not just an idle screen and a source list.
    await show({ runtimeRead: unreadRegion<RuntimeDependency>() });
    expect(cards()).toHaveLength(3);
    await waitFor(() => expect(lastAction())
      .toMatchObject({ labelKey: 'onboarding.providers.actionAdd', disabled: true }));

    // Not through the card, and not through the footer either: one admission, wherever
    // the control is drawn.
    await userEvent.setup().click(cards()[2]);
    expect(screen.queryByRole('dialog')).toBeNull();
    await activate(handle);
    expect(screen.queryByRole('dialog')).toBeNull();
    // And the recovery is where the failure is, on the card that can do something.
    expect(within(gatewayCard()).getByRole('button', { name: 'Retry' })).toBeTruthy();
  });

  it('still asks again for the read that failed while the engine is unknown too', async () => {
    // Two unknowns, two different answers. Asking again is a read: it writes nothing,
    // so an unreadable engine is no reason to withhold it. What it may not do is turn
    // into a write once the inventory answers.
    serve();
    vi.mocked(modelsApi.listSources).mockRejectedValue(new Error('offline'));
    const { handle } = renderScreen({ runtimeRead: unreadRegion<RuntimeDependency>() });
    await settled();
    await waitFor(() => expect(lastAction()).toMatchObject({ labelKey: 'common.retry', disabled: false }));

    const before = vi.mocked(modelsApi.listSources).mock.calls.length;
    vi.mocked(modelsApi.listSources)
      .mockImplementation(async () => [source({ id: 'src_zhipu', vendor: 'zhipuai' })]);
    await activate(handle);

    await waitFor(() => expect(modelsApi.listSources).toHaveBeenCalledTimes(before + 1));
    await waitFor(() => expect(cardFor('zhipuai').dataset.state).toBe('connected'));
    expect(screen.queryByRole('dialog')).toBeNull();
    // The inventory answered; the engine still has not. So the screen names what is
    // next and goes on refusing it.
    await waitFor(() => expect(lastAction())
      .toMatchObject({ labelKey: 'onboarding.providers.actionContinue', disabled: true }));
  });

  it('refuses the write an open dialog was for when the answer beneath it changes', async () => {
    serve();
    vi.spyOn(modelsApi, 'createApiKeySource').mockResolvedValue({
      source: source({ id: 'src_new', vendor: 'custom' }),
      added_to: [],
      adopted_by: [],
    });
    const { show } = renderScreen();
    await settled();
    const user = userEvent.setup();

    await user.click(cards()[2]);
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'API Key' }));
    await user.type(within(dialog).getByLabelText('Base URL'), 'https://api.example/v1');
    await user.type(within(dialog).getByLabelText('API key'), 'sk-live-1');
    const add = () => within(screen.getByRole('dialog')).getByRole<HTMLButtonElement>(
      'button', { name: 'Add' },
    );
    expect(add().disabled).toBe(false);

    // The engine stopped being readable while the form was being filled. Admission is
    // not only a door: the dialog stays, because what is typed in it is the person's
    // and the read behind it may come back — and the write is what stops.
    await show({ runtimeRead: unreadRegion<RuntimeDependency>() });
    expect(screen.queryByRole('dialog')).not.toBeNull();
    expect(add().disabled).toBe(true);
    await user.click(add());
    expect(modelsApi.createApiKeySource).not.toHaveBeenCalled();

    // The read comes back and says the engine is serving. Nothing was lost.
    await show({ runtimeRead: readyRegion(runtimeOf('ok')) });
    expect(add().disabled).toBe(false);
    await user.click(add());
    await waitFor(() => expect(modelsApi.createApiKeySource).toHaveBeenCalledTimes(1));
  });

  it('refuses the write of a dialog the wizard has hidden, and takes it back on return', async () => {
    serve();
    const create = vi.spyOn(modelsApi, 'createApiKeySource').mockResolvedValue({
      source: source({ id: 'src_new', vendor: 'custom' }),
      added_to: [],
      adopted_by: [],
    });
    const { show } = renderScreen();
    await settled();
    const user = userEvent.setup();

    await user.click(cards()[2]);
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'API Key' }));
    await user.type(within(dialog).getByLabelText('Base URL'), 'https://api.example/v1');
    await user.type(within(dialog).getByLabelText('API key'), 'sk-live-1');
    const add = () => within(screen.getByRole('dialog')).getByRole<HTMLButtonElement>(
      'button', { name: 'Add' },
    );

    // The wizard moved somewhere else with this open. A screen nobody is looking at
    // does not act — and a write is the strongest thing it could do, so it is the
    // first thing that stops. What was typed is still the person's.
    await show({ active: false });
    expect(add().disabled).toBe(true);
    await user.click(add());
    expect(create).not.toHaveBeenCalled();

    await show({ active: true });
    expect(add().disabled).toBe(false);
    await user.click(add());
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
  });

  it('refuses a write the configuration does not admit, however healthy the engine is', async () => {
    serve();
    const { handle, show } = renderScreen();
    await settled();
    const user = userEvent.setup();

    // The runtime is up and the card goes on saying so, truthfully: health is a fact
    // about the machine, not a permission. What is gone is the configuration that
    // admits this flow to the Hub — and every write here is a write to the Hub.
    await show({ gatewayEnabled: false, runtimeRead: readyRegion(runtimeOf('ok')) });
    await waitFor(() => expect(lastAction())
      .toMatchObject({ labelKey: 'onboarding.providers.actionAdd', disabled: true }));
    await user.click(cards()[2]);
    expect(screen.queryByRole('dialog')).toBeNull();
    await activate(handle);
    expect(screen.queryByRole('dialog')).toBeNull();

    // Unknown is not authorization either. A capability still being read and a saved
    // intent nobody has answered are two reasons to wait, not two halves of a yes.
    await show({ capability: 'pending', gatewayEnabled: null, runtimeRead: readyRegion(runtimeOf('ok')) });
    await user.click(cards()[2]);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('ends a retry on the answer that failed, not on an unrelated later one', async () => {
    serve({ runtime: runtimeOf('not_started') });
    vi.mocked(modelsApi.startRuntime).mockRejectedValue(new ApiCallError('engine_down', 'down'));
    const { show } = renderScreen({ runtimeRead: readyRegion(runtimeOf('not_started')) });

    await waitFor(() => expect(gatewayCard().dataset.state).toBe('failed'));
    await userEvent.setup().click(within(gatewayCard()).getByRole('button', { name: 'Retry' }));
    expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1);

    // The read the retry asked for fails, and one load failure takes everything with
    // it: no capability, no saved intent, an unread machine. That is a complete answer
    // — 「不知道」 — and nothing more is coming unless someone asks again. Filing it
    // under 「still unknown」 would leave the press waiting to be spent on whatever
    // read landed next, which is not the one it asked for.
    await show({ capability: 'pending', gatewayEnabled: null, runtimeRead: unreadRegion<RuntimeDependency>() });
    expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1);

    // Something else re-reads the machine much later and finds it installed and idle.
    await show({ capability: 'enabled', gatewayEnabled: true, runtimeRead: readyRegion(runtimeOf('not_started')) });
    await waitFor(() => expect(gatewayCard()).toBeTruthy());
    expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1);
  });

  it('refuses to continue into a screen with no engine behind it', async () => {
    // The next screen picks a model per assistant out of what the Hub supplies, so
    // arriving with a stopped engine is arriving at an empty screen with no way to
    // tell why. The label does not change; what went wrong stays on the card.
    const stopped = { ...UNSUPPORTED, status: { ...UNSUPPORTED.status, health: 'not_started' as const } };
    serve({ runtime: stopped, sources: [source({ id: 'src_zhipu', vendor: 'zhipu' })] });
    vi.mocked(modelsApi.startRuntime).mockRejectedValue(new ApiCallError('engine_down', 'down'));
    const { handle } = renderScreen({ runtimeRead: readyRegion(stopped) });
    await settled();

    await waitFor(() => expect(lastAction()).toMatchObject({
      labelKey: 'onboarding.providers.actionContinue',
      disabled: true,
      busy: false,
    }));

    await activate(handle);
    expect(navigated).toEqual([]);
  });

  it('continues once the engine is really serving', async () => {
    serve({ sources: [source({ id: 'src_zhipu', vendor: 'zhipu' })] });
    const { handle } = renderScreen();
    await settled();

    await waitFor(() => expect(lastAction()).toMatchObject({
      labelKey: 'onboarding.providers.actionContinue',
      disabled: false,
    }));

    await activate(handle);
    expect(navigated).toEqual(['assistants']);
  });

  it('treats a machine with no CLI as nothing to adopt, not as a failure', async () => {
    serve({
      agents: [supply({ backend: 'codex', cli_present: false }), supply({ backend: 'opencode', cli_present: false })],
      runtime: runtimeOf('not_installed'),
    });
    renderScreen({ runtimeRead: readyRegion(runtimeOf('not_installed')) });
    await settled();

    // The engine is ensured whatever there is to adopt. Nothing to adopt is not
    // nothing to do: the next screen picks a model per assistant out of what the Hub
    // supplies, so a machine with no CLI still arrives there needing one behind it.
    await waitFor(() => expect(modelsApi.installRuntime).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1));
    // Presence is read fresh: a CLI installed while setup was open is exactly the
    // case a cached list would get wrong.
    await waitFor(() => expect(modelsApi.refreshAgentPresence).toHaveBeenCalled());
    await waitFor(() => expect(gatewayCard().dataset.state).toBe('idle'));
  });

  it('still ensures the engine on a machine where every CLI is already adopted', async () => {
    // The other exit that used to strand this screen. `resumeGatewayAdoption` returns
    // success without touching the runtime when its backend is already in hub mode,
    // so the engine stayed missing: card idle, no retry beside it, Continue blocked
    // on a read that never moves.
    serve({
      agents: [supply({ backend: 'codex', mode: 'hub' }), supply({ backend: 'claude', mode: 'hub' })],
      runtime: runtimeOf('not_installed'),
    });
    renderScreen({ runtimeRead: readyRegion(runtimeOf('not_installed')) });
    await settled();

    await waitFor(() => expect(modelsApi.installRuntime).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1));
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

  it('lets an install it started finish after the wizard hides the screen', async () => {
    // The mutation is on the server either way. Abandoning the attempt when the
    // screen is merely hidden leaves the card stuck on 「正在安装」, the shell's read
    // describing a machine that has since changed, and nothing to reconcile either.
    serve({ runtime: runtimeOf('not_installed') });
    const install = deferred<RuntimeDependency>();
    vi.mocked(modelsApi.installRuntime).mockReturnValue(install.promise);
    const { show } = renderScreen({ runtimeRead: readyRegion(runtimeOf('not_installed')) });

    await waitFor(() => expect(modelsApi.installRuntime).toHaveBeenCalledTimes(1));
    await show({ active: false });

    await act(async () => { install.resolve(runtimeOf('not_started')); });
    await waitFor(() => expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1));
    // The engine moved, so the read that describes it is asked for again.
    await waitFor(() => expect(retrySetup).toHaveBeenCalled());

    // And coming back finds the finished attempt rather than launching a second
    // install over it.
    await show({ active: true });
    await act(async () => { await Promise.resolve(); });
    expect(modelsApi.installRuntime).toHaveBeenCalledTimes(1);
    expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1);
  });

  it('installs once under a StrictMode replay, and does not abandon what it started', async () => {
    // The app mounts under StrictMode, which runs every effect's cleanup and then the
    // effect again. Both halves of the attempt have to survive that: the guard, or it
    // installs twice; the attempt's identity, or the replay's cleanup discards a
    // mutation the server is performing and the card never leaves 「正在安装」.
    serve({ runtime: runtimeOf('not_installed') });
    const install = deferred<RuntimeDependency>();
    vi.mocked(modelsApi.installRuntime).mockReturnValue(install.promise);
    const handle = React.createRef<SetupScreenHandle | null>();
    render(
      <React.StrictMode>
        <Harness handle={handle} runtimeRead={readyRegion(runtimeOf('not_installed'))} />
      </React.StrictMode>,
    );

    await waitFor(() => expect(modelsApi.installRuntime).toHaveBeenCalledTimes(1));
    await act(async () => { install.resolve(runtimeOf('not_started')); });

    await waitFor(() => expect(modelsApi.startRuntime).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(gatewayCard().dataset.state).toBe('idle'));
    expect(modelsApi.installRuntime).toHaveBeenCalledTimes(1);
  });

  it('says the engine could not be read rather than showing an idle one', async () => {
    // The read failed and asking again is worth doing. Folded into 「waiting」 this
    // renders as the idle card: the engine looks fine while Continue stays disabled
    // for a reason nothing on screen gives.
    serve();
    renderScreen({ runtimeRead: unreadRegion<RuntimeDependency>() });
    await settled();

    expect(gatewayCard().dataset.state).toBe('failed');
    expect(within(gatewayCard()).getByText('Model Hub is not ready. Try again.')).toBeTruthy();
    // The runtime-unread copy, never the unsupported notice: nothing has been read
    // about this host, so nothing can be said about what it supports.
    expect(within(gatewayCard()).queryByRole('link')).toBeNull();
    expect(modelsApi.installRuntime).not.toHaveBeenCalled();

    await userEvent.setup().click(within(gatewayCard()).getByRole('button', { name: 'Retry' }));
    // The shell owns the read; this screen owns the attempt. Both are re-armed,
    // because either one could be what is stale.
    expect(retrySetup).toHaveBeenCalled();
  });

  it('stays quiet about a failed read there is nothing to ask again about', async () => {
    // The shell owns the read and has declared this one beyond retrying. A Retry
    // that cannot help is a worse answer than none.
    serve();
    renderScreen({ runtimeRead: unreadRegion<RuntimeDependency>(false) });
    await settled();

    expect(gatewayCard().dataset.state).toBe('idle');
    expect(within(gatewayCard()).queryByRole('button')).toBeNull();
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
