// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nextProvider } from "react-i18next";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "@/i18n";
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { ApiCallError, modelsApi } from "./modelsApi";
import {
  RouteChainDialog,
  type RouteCollectionObservation,
} from "./RouteChainDialog";
import { routeChainMatchesAttempt } from "./routeChainDraft";
import { readyRegion } from "./regionRead";
import type {
  AgentChain,
  AgentChainMutation,
  AgentSupply,
  Source,
} from "./types";

const agent: AgentSupply = {
  backend: "claude",
  cli_present: true,
  mode: "hub",
  menu_kind: "fixed",
};
const sources: Source[] = [
  {
    id: "src_a",
    last_discovered_at: null,
    kind: "api_key",
    vendor: "anthropic",
    display_name: "API key",
    protocol: "anthropic",
    supply_channel: "hub",
    billing: "metered",
    state: { status: "active", retry_at: null, detail_key: null },
    models: [],
  },
  {
    id: "src_b",
    last_discovered_at: null,
    kind: "subscription",
    vendor: "anthropic",
    display_name: "Claude subscription",
    protocol: "anthropic",
    supply_channel: "native_cli",
    billing: "monthly",
    state: { status: "active", retry_at: null, detail_key: null },
    models: [],
  },
];
const chain: AgentChain = { manual_override: {hops:[{source_id:"src_a",model_id:"claude-opus-5"},{source_id:"src_b",model_id:"opus-5"}]}, route_origin: "manual" as const,
  contract_version: 10,
  backend: "claude",
  model_id: "opus-5",
  current: { source_id: "src_b", model_id: "opus-5" },
  chain: [
    {
      source_id: "src_a",
      model_id: "claude-opus-5",
      channel: "hub",
      health: "cooldown",
      runnable: false,
      reason: null,
      retry_at: "2099-01-01T00:00:00Z",
    },
    {
      source_id: "src_b",
      model_id: "opus-5",
      channel: "native_cli",
      health: "healthy",
      runnable: true,
      reason: null,
      retry_at: null,
    },
  ],
  supply_state: "ok",
};
const mutation = (
  next: AgentChain = chain,
  report: Partial<Pick<AgentChainMutation, "removed_hops" | "interrupted">> =
    {},
): AgentChainMutation => ({
  chain: next,
  removed_hops: report.removed_hops ?? [],
  interrupted: report.interrupted ?? [],
});
/** An Agent whose eligible sources actually carry spare models — the only shape
 *  that makes the add-hop selector reachable, since `routeCandidates` excludes
 *  the pairs the draft already holds. */
const stocked = () => ({
  agent: {
    ...agent,
    sources: {
      order: ["src_a", "src_b"],
      eligibility: [
        { source_id: "src_a", eligible: true },
        { source_id: "src_b", eligible: true },
      ],
    },
  } satisfies AgentSupply,
  sources: [
    {
      ...sources[0],
      models: [
        { id: "claude-opus-5", origin: "discovered", reasoning_efforts: [], reasoning_efforts_source: null },
        { id: "claude-sonnet-5", origin: "discovered", reasoning_efforts: [], reasoning_efforts_source: null },
        { id: "claude-haiku-5", origin: "discovered", reasoning_efforts: [], reasoning_efforts_source: null },
      ],
    },
    {
      ...sources[1],
      models: [
        { id: "opus-5", origin: "discovered", reasoning_efforts: [], reasoning_efforts_source: null },
        { id: "sonnet-5", origin: "discovered", reasoning_efforts: [], reasoning_efforts_source: null },
      ],
    },
  ] satisfies Source[],
});
const observation = <T,>(value: T) => ({ value, install: vi.fn() });
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, reject, resolve };
};
const renderStockedDialog = () => {
  const fixture = stocked();
  vi.spyOn(modelsApi, "getAgentChain").mockResolvedValue(chain);
  render(
    <I18nextProvider i18n={i18n}>
      <RouteChainDialog
        selection={{
          agent: fixture.agent,
          modelId: "opus-5",
          read: readyRegion(chain),
        }}
        sources={fixture.sources}
        onClose={vi.fn()}
        onCommitted={vi.fn()}
        readAgents={vi.fn().mockResolvedValue(observation([fixture.agent]))}
        readSources={vi.fn().mockResolvedValue(observation(fixture.sources))}
      />
    </I18nextProvider>,
  );
};
const renderDialog = (onCommitted = vi.fn(), onClose = vi.fn()) => {
  vi.spyOn(modelsApi, "getAgentChain").mockResolvedValue(chain);
  return render(
    <I18nextProvider i18n={i18n}>
      <RouteChainDialog
        selection={{ agent, modelId: "opus-5", read: readyRegion(chain) }}
        sources={sources}
        onClose={onClose}
        onCommitted={onCommitted}
        readAgents={vi.fn().mockResolvedValue(observation([agent]))}
        readSources={vi.fn().mockResolvedValue(observation(sources))}
      />
    </I18nextProvider>,
  );
};

beforeEach(() => {
  vi.spyOn(modelsApi, 'getAgentProvenance').mockResolvedValue(null);
  // cmdk observes its list box and scrolls the active row into view; jsdom
  // implements neither.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("RouteChainDialog", () => {
  it.each(['manual', 'inherited', 'preview', 'missing', 'stale'] as const)(
    'exposes complete same-prefix source/model identities as wrapping detail text in %s state', async (state) => {
      const user = userEvent.setup();
      const prefix = 'exact-identity-with-a-very-long-shared-prefix-';
      const identities = ['alpha', 'omega'].map((suffix) => ({
        source_id: `${prefix}source-${suffix}`, model_id: `${prefix}model-${suffix}`,
      }));
      const detailedSources = identities.map((hop) => ({ ...sources[0], id: hop.source_id, display_name: hop.source_id }));
      const detailedAgent: AgentSupply = { ...agent, sources: {
        order: [], eligibility: identities.map((hop) => ({ source_id: hop.source_id, eligible: true })),
      } };
      const projection: AgentChain = {
        ...chain,
        route_origin: state === 'inherited' ? 'automatic' : 'manual',
        manual_override: state === 'inherited' ? null : { hops: identities },
        current: identities[0],
        chain: identities.map((hop) => ({
          ...chain.chain[1], ...hop,
          reason: state === 'missing' ? 'source_missing' : null,
          runnable: state !== 'missing',
        })),
      };
      const inherited: AgentChain = { ...projection, manual_override: null, route_origin: 'automatic' };
      vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(projection);
      vi.spyOn(modelsApi, 'previewAgentChain').mockResolvedValue(inherited);
      render(<I18nextProvider i18n={i18n}><RouteChainDialog
        selection={{ agent: detailedAgent, modelId: 'opus-5', read: readyRegion(projection) }}
        sources={state === 'missing' || state === 'stale' ? [] : detailedSources}
        onClose={vi.fn()} readAgents={vi.fn()} readSources={vi.fn()}
      /></I18nextProvider>);
      await waitFor(() => expect(document.querySelectorAll('.model-hub-route-hop-name')).toHaveLength(2));
      if (state === 'preview') {
        await user.click(screen.getByRole('button', { name: 'Restore automatic' }));
        await screen.findByRole('button', { name: 'Undo restore' });
      }
      const names = [...document.querySelectorAll('.model-hub-route-hop-name')];
      const models = [...document.querySelectorAll('.model-hub-route-hop-model')];
      identities.forEach((hop, index) => {
        expect(names[index].textContent).toContain(hop.source_id);
        expect(models[index].textContent).toBe(hop.model_id);
        for (const field of [names[index], models[index]]) {
          expect(field.classList.contains('truncate')).toBe(false);
          expect(field.closest('.model-hub-route-hop-copy')?.classList.contains('min-w-0')).toBe(true);
        }
      });
      const footer = within(document.querySelector<HTMLElement>('.model-hub-route-foot')!);
      const labels = state === 'inherited' ? ['Close', 'Edit route']
        : [state === 'preview' ? 'Undo restore' : 'Restore automatic', 'Cancel', 'Save'];
      expect(footer.getAllByRole('button').map((button) => button.textContent)).toEqual(labels);
      for (const label of labels) expect(footer.getByRole('button', { name: label }).hasAttribute('hidden')).toBe(false);
      expect(screen.getByRole('dialog').classList.contains('overflow-hidden')).toBe(true);
    },
  );

  it('closes inherited inspection with visible Close, not the icon-only Cancel command', async () => {
    const user = userEvent.setup();
    const inherited: AgentChain = { ...chain, manual_override: null, route_origin: 'automatic' };
    vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(inherited);
    const put = vi.spyOn(modelsApi, 'putAgentChain');
    const restore = vi.spyOn(modelsApi, 'restoreAgentChain');
    const close = vi.fn();
    render(<I18nextProvider i18n={i18n}><RouteChainDialog
      selection={{ agent, modelId: 'opus-5', read: readyRegion(inherited) }}
      sources={sources} onClose={close} readAgents={vi.fn()} readSources={vi.fn()}
    /></I18nextProvider>);

    await screen.findByRole('button', { name: 'Edit route' });
    const footer = within(document.querySelector<HTMLElement>('.model-hub-route-foot')!);
    const dismiss = footer.getByRole('button', { name: 'Close' });
    expect(dismiss.textContent).toBe('Close');
    expect(footer.queryByRole('button', { name: 'Cancel' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Cancel' }).textContent).toBe('');
    expect(screen.queryByRole('button', { name: 'Add a hop' })).toBeNull();
    await user.click(dismiss);
    expect(close).toHaveBeenCalledTimes(1);
    expect(put).not.toHaveBeenCalled();
    expect(restore).not.toHaveBeenCalled();
  });

  it('uses visible Cancel after Edit and throughout restore preview and undo without writing', async () => {
    const user = userEvent.setup();
    const inherited: AgentChain = { ...chain, manual_override: null, route_origin: 'automatic' };
    vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(inherited);
    const preview = vi.spyOn(modelsApi, 'previewAgentChain').mockResolvedValue(inherited);
    const put = vi.spyOn(modelsApi, 'putAgentChain');
    const restore = vi.spyOn(modelsApi, 'restoreAgentChain');
    const close = vi.fn();
    render(<I18nextProvider i18n={i18n}><RouteChainDialog
      selection={{ agent, modelId: 'opus-5', read: readyRegion(inherited) }}
      sources={sources} onClose={close} readAgents={vi.fn()} readSources={vi.fn()}
    /></I18nextProvider>);

    await user.click(await screen.findByRole('button', { name: 'Edit route' }));
    const footer = within(document.querySelector<HTMLElement>('.model-hub-route-foot')!);
    expect(footer.getByRole('button', { name: 'Cancel' }).textContent).toBe('Cancel');
    expect(footer.queryByRole('button', { name: 'Close' })).toBeNull();
    expect(screen.getAllByRole('button', { name: 'Remove hop' })).toHaveLength(2);

    await user.click(screen.getByRole('button', { name: 'Restore automatic' }));
    await screen.findByRole('button', { name: 'Undo restore' });
    expect(preview).toHaveBeenCalledWith('claude', 'opus-5', { manual_override: null });
    expect(footer.getByRole('button', { name: 'Cancel' }).textContent).toBe('Cancel');
    expect(footer.queryByRole('button', { name: 'Close' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Remove hop' })).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Undo restore' }));
    expect(footer.getByRole('button', { name: 'Cancel' }).textContent).toBe('Cancel');
    expect(footer.queryByRole('button', { name: 'Close' })).toBeNull();
    expect(screen.getAllByRole('button', { name: 'Remove hop' })).toHaveLength(2);
    await user.click(footer.getByRole('button', { name: 'Cancel' }));
    expect(close).toHaveBeenCalledTimes(1);
    expect(put).not.toHaveBeenCalled();
    expect(restore).not.toHaveBeenCalled();
  });

  describe('manual edit capability from an empty inherited route', () => {
    const inherited: AgentChain = {
      ...chain,
      manual_override: null,
      route_origin: null,
      current: null,
      chain: [],
      supply_state: 'interrupted',
    };
    const alternateSubscription: Source = {
      ...sources[1],
      models: [{ id: 'sonnet-5', origin: 'discovered', reasoning_efforts: [], reasoning_efforts_source: null }],
    };
    const renderEmptyRoute = (nextAgent: AgentSupply, nextSources: Source[], initial = inherited) => {
      vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(initial);
      render(<I18nextProvider i18n={i18n}><RouteChainDialog
        selection={{ agent: nextAgent, modelId: inherited.model_id, read: readyRegion(initial) }}
        sources={nextSources}
        onClose={vi.fn()}
        onOpenDefaults={vi.fn()}
        readAgents={vi.fn()}
        readSources={vi.fn()}
      /></I18nextProvider>);
    };

    it('selects another known subscription model and saves the exact manual mapping', async () => {
      const user = userEvent.setup();
      const hop = { source_id: alternateSubscription.id, model_id: 'sonnet-5' };
      const put = vi.spyOn(modelsApi, 'putAgentChain').mockResolvedValue(mutation({
        ...chain, manual_override: { hops: [hop] }, current: hop, chain: [{ ...chain.chain[1], ...hop }],
      }));
      renderEmptyRoute({ ...agent, sources: {
        order: [alternateSubscription.id], eligibility: [{ source_id: alternateSubscription.id, eligible: true }],
      } }, [alternateSubscription]);

      await user.click(await screen.findByRole('button', { name: 'Edit route' }));
      await user.click(screen.getByRole('button', { name: 'Add a hop' }));
      await user.click(screen.getByRole('option', { name: /sonnet-5/ }));
      await user.click(screen.getByRole('button', { name: 'Add' }));
      await user.click(screen.getByRole('button', { name: 'Save' }));

      await waitFor(() => expect(put).toHaveBeenCalledWith('claude', 'opus-5', { hops: [hop] }));
    });

    it('allows an eligible API key outside defaults to save an exact unlisted target', async () => {
      const user = userEvent.setup();
      const hop = { source_id: sources[0].id, model_id: 'unlisted-model' };
      const put = vi.spyOn(modelsApi, 'putAgentChain').mockResolvedValue(mutation({
        ...chain, manual_override: { hops: [hop] }, current: hop,
        chain: [{ ...chain.chain[0], ...hop, health: 'healthy', runnable: true, retry_at: null }],
      }));
      renderEmptyRoute({ ...agent, sources: {
        order: [], eligibility: [{ source_id: sources[0].id, eligible: true }],
      } }, [sources[0]]);

      await user.click(await screen.findByRole('button', { name: 'Edit route' }));
      await user.click(screen.getByRole('button', { name: 'Add a hop' }));
      await user.type(screen.getByLabelText('Exact model ID'), hop.model_id);
      await user.click(screen.getByRole('button', { name: 'Add' }));
      await user.click(screen.getByRole('button', { name: 'Save' }));

      await waitFor(() => expect(put).toHaveBeenCalledWith('claude', 'opus-5', { hops: [hop] }));
      expect(sources[0].models).toEqual([]);
    });

    it.each([
      { name: 'no sources', available: [], eligible: true },
      { name: 'an eligible subscription with no known models', available: [sources[1]], eligible: true },
      { name: 'an eligible subscription with only retired models', available: [{ ...alternateSubscription, models: alternateSubscription.models.map((model) => ({ ...model, retired: true })) }], eligible: true },
      { name: 'ineligible API-key and subscription sources', available: [sources[0], alternateSubscription], eligible: false },
    ])('keeps the unconfigured view without Edit for $name', async ({ available, eligible }) => {
      renderEmptyRoute({ ...agent, sources: {
        order: [], eligibility: available.map((source) => ({ source_id: source.id, eligible })),
      } }, available);

      await screen.findByRole('button', { name: 'Configure default routing' });
      expect(screen.queryByRole('button', { name: 'Edit route' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Restore automatic' })).toBeNull();
    });

    it('reads legacy empty storage only as server-normalized inheritance with no admissible targets', async () => {
      renderEmptyRoute({ ...agent, sources: { order: [], eligibility: [] } }, []);
      await screen.findByRole('button', { name: 'Configure default routing' });
      expect(screen.getByText('Unconfigured')).toBeTruthy();
      expect(screen.queryByRole('button', { name: 'Restore automatic' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
      expect(screen.queryByText(/saved route is empty/)).toBeNull();
    });

    it('cannot save an empty newly opened editor as Manual', async () => {
      const user = userEvent.setup();
      renderEmptyRoute({ ...agent, sources: {
        order: [], eligibility: [{ source_id: sources[0].id, eligible: true }],
      } }, [sources[0]]);
      await user.click(await screen.findByRole('button', { name: 'Edit route' }));
      expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
      expect(screen.queryByText(/saved route is empty/)).toBeNull();
    });
  });

  describe('last-hop clear inherits routing', () => {
    const inherited: AgentChain = { ...chain, manual_override: null, route_origin: 'automatic' };
    const unconfigured: AgentChain = { ...inherited, route_origin: null, current: null, chain: [], supply_state: 'interrupted' };
    const clear = async (user: ReturnType<typeof userEvent.setup>) => {
      await screen.findAllByRole('button', { name: 'Remove hop' });
      await user.click(screen.getAllByRole('button', { name: 'Remove hop' })[0]);
      await user.click(screen.getByRole('button', { name: 'Remove hop' }));
    };

    it.each(['manual', 'automatic'] as const)('previews clear from %s, undoes only the previous unsaved draft and cancels without writes', async (origin) => {
      const user = userEvent.setup();
      const preview = vi.spyOn(modelsApi, 'previewAgentChain').mockResolvedValue(inherited);
      const put = vi.spyOn(modelsApi, 'putAgentChain');
      const restore = vi.spyOn(modelsApi, 'restoreAgentChain');
      const close = vi.fn();
      renderDialog(vi.fn(), close);
      if (origin === 'automatic') {
        vi.mocked(modelsApi.getAgentChain).mockResolvedValue(inherited);
        cleanup();
        render(<I18nextProvider i18n={i18n}><RouteChainDialog selection={{ agent, modelId: 'opus-5', read: readyRegion(inherited) }} sources={sources} onClose={close} readAgents={vi.fn()} readSources={vi.fn()} /></I18nextProvider>);
        await user.click(await screen.findByRole('button', { name: 'Edit route' }));
      }
      await clear(user);
      await screen.findByText('After restore: automatic matching');
      expect(preview).toHaveBeenCalledWith('claude', 'opus-5', { manual_override: null });
      expect(screen.queryByRole('button', { name: 'Remove hop' })).toBeNull();
      expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false);
      await user.click(screen.getByRole('button', { name: 'Undo restore' }));
      expect(screen.getAllByRole('button', { name: 'Remove hop' })).toHaveLength(1);
      expect(document.querySelector('.model-hub-route-hop-model')?.textContent).toBe('opus-5');
      await user.click(screen.getByRole('button', { name: 'Remove hop' }));
      await screen.findByText('After restore: automatic matching');
      await user.click(within(document.querySelector<HTMLElement>('.model-hub-route-foot')!).getByRole('button', { name: 'Cancel' }));
      expect(close).toHaveBeenCalledOnce();
      expect(put).not.toHaveBeenCalled();
      expect(restore).not.toHaveBeenCalled();
      expect(chain.manual_override?.hops).toHaveLength(2);
    });

    it('clears to no-key Unconfigured, confirms the exact DELETE guard and shows actual impact then Done', async () => {
      const user = userEvent.setup();
      const gap = { backend: 'claude' as const, model_id: 'opus-5', agents: ['writer'] };
      const hop = { backend: 'claude' as const, menu_model: 'opus-5', ...chain.manual_override!.hops[0], position: 1 };
      vi.spyOn(modelsApi, 'previewAgentChain').mockResolvedValue(unconfigured);
      const restore = vi.spyOn(modelsApi, 'restoreAgentChain')
        .mockRejectedValueOnce(new ApiCallError('source_last_supplier', undefined, true, [gap], [], [hop]))
        .mockResolvedValueOnce(mutation(unconfigured, { removed_hops: [hop], interrupted: [gap] }));
      const put = vi.spyOn(modelsApi, 'putAgentChain');
      const committed = vi.fn();
      vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(chain);
      render(<I18nextProvider i18n={i18n}><RouteChainDialog
        selection={{ agent: { ...agent, sources: { order: [], eligibility: [] } }, modelId: 'opus-5', read: readyRegion(chain) }}
        sources={[]} onClose={vi.fn()} onCommitted={committed} readAgents={vi.fn()} readSources={vi.fn()}
      /></I18nextProvider>);
      await clear(user);
      await screen.findByText('After restore: unconfigured');
      expect(restore).not.toHaveBeenCalled();
      await user.click(screen.getByRole('button', { name: 'Save' }));
      await user.click(await screen.findByRole('button', { name: 'Save anyway' }));
      await within(document.querySelector<HTMLElement>('.model-hub-route-foot')!).findByRole('button', { name: 'Done' });
      expect(restore.mock.calls).toEqual([
        ['claude', 'opus-5', undefined],
        ['claude', 'opus-5', { force: true, would_remove_hops: [hop], would_interrupt: [gap] }],
      ]);
      expect(committed).toHaveBeenCalledWith(mutation(unconfigured, { removed_hops: [hop], interrupted: [gap] }));
      expect(screen.getByText('Agents pinned to it: writer')).toBeTruthy();
      expect(put).not.toHaveBeenCalled();
    });

    it('keeps clear intent and Undo through a failed preview, retries without enabling stale Save', async () => {
      const user = userEvent.setup();
      const pending = deferred<AgentChain>();
      vi.spyOn(modelsApi, 'previewAgentChain').mockRejectedValueOnce(new Error('offline')).mockReturnValueOnce(pending.promise);
      const restore = vi.spyOn(modelsApi, 'restoreAgentChain');
      renderDialog();
      await clear(user);
      await screen.findByRole('alert');
      expect(screen.getByRole('button', { name: 'Undo restore' })).toBeTruthy();
      expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
      expect(screen.queryByText('Manual', { exact: true })).toBeNull();
      await user.click(screen.getByRole('button', { name: 'Retry' }));
      expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
      await act(async () => pending.resolve({ ...inherited, route_origin: 'passthrough' }));
      await screen.findByText('After restore: original-name passthrough');
      expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false);
      expect(restore).not.toHaveBeenCalled();
      await user.click(screen.getByRole('button', { name: 'Undo restore' }));
      expect(document.querySelector('.model-hub-route-hop-model')?.textContent).toBe('opus-5');
    });

    it.each(['undo', 'close'] as const)('invalidates a late clear preview after %s', async (action) => {
      const user = userEvent.setup();
      const pending = deferred<AgentChain>();
      vi.spyOn(modelsApi, 'previewAgentChain').mockReturnValue(pending.promise);
      const close = vi.fn();
      renderDialog(vi.fn(), close);
      await clear(user);
      const footer = within(document.querySelector<HTMLElement>('.model-hub-route-foot')!);
      expect((footer.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
      await user.click(footer.getByRole('button', { name: action === 'undo' ? 'Undo restore' : 'Cancel' }));
      await act(async () => pending.resolve(inherited));
      expect(screen.queryByText('After restore: automatic matching')).toBeNull();
      if (action === 'undo') {
        expect(screen.getAllByRole('button', { name: 'Remove hop' })).toHaveLength(1);
        expect(document.querySelector('.model-hub-route-hop-model')?.textContent).toBe('opus-5');
        expect((footer.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false);
      } else expect(close).toHaveBeenCalledOnce();
    });

    it('invalidates old clear preview on Default routing navigation and keeps Undo through refresh failure', async () => {
      const user = userEvent.setup();
      const old = deferred<AgentChain>();
      const preview = vi.spyOn(modelsApi, 'previewAgentChain')
        .mockReturnValueOnce(old.promise).mockRejectedValueOnce(new Error('offline')).mockResolvedValue(unconfigured);
      vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(chain);
      const openDefaults = vi.fn();
      const props = { selection: { agent, modelId: 'opus-5', read: readyRegion(chain) }, sources, onClose: vi.fn(), onOpenDefaults: openDefaults, readAgents: vi.fn(), readSources: vi.fn() };
      const view = (covered: boolean) => <I18nextProvider i18n={i18n}><RouteChainDialog {...props} covered={covered} /></I18nextProvider>;
      const { rerender } = render(view(false));
      await clear(user);
      await user.click(screen.getByRole('button', { name: /Default routing/ }));
      expect(openDefaults).toHaveBeenCalledOnce();
      rerender(view(true));
      rerender(view(false));
      await screen.findByRole('alert');
      await act(async () => old.resolve(inherited));
      expect(screen.queryByText('After restore: automatic matching')).toBeNull();
      expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
      await user.click(screen.getByRole('button', { name: 'Retry' }));
      await screen.findByText('After restore: unconfigured');
      expect(preview).toHaveBeenCalledTimes(3);
      await user.click(screen.getByRole('button', { name: 'Undo restore' }));
      expect(document.querySelector('.model-hub-route-hop-model')?.textContent).toBe('opus-5');
    });

    it.each(['en', 'zh'] as const)('renders inherited preview, failure, Undo and Save copy in %s', async (lng) => {
      const user = userEvent.setup();
      const locale = i18n.cloneInstance({ lng });
      const copy = (lng === 'en' ? en : zh).settings.models;
      const pending = deferred<AgentChain>();
      vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(chain);
      vi.spyOn(modelsApi, 'previewAgentChain').mockReturnValueOnce(pending.promise).mockResolvedValue(inherited);
      render(<I18nextProvider i18n={locale}><RouteChainDialog selection={{ agent, modelId: 'opus-5', read: readyRegion(chain) }} sources={sources} onClose={vi.fn()} readAgents={vi.fn()} readSources={vi.fn()} /></I18nextProvider>);
      await screen.findAllByRole('button', { name: copy.routeDialog.removeHop });
      await user.click(screen.getAllByRole('button', { name: copy.routeDialog.removeHop })[0]);
      await user.click(screen.getByRole('button', { name: copy.routeDialog.removeHop }));
      expect(screen.getByText(copy.routing.previewLoading)).toBeTruthy();
      const footer = within(document.querySelector<HTMLElement>('.model-hub-route-foot')!);
      expect(footer.getAllByRole('button').map((button) => button.textContent)).toEqual([copy.routing.undoRestore, copy.routeDialog.cancel, copy.routeDialog.save]);
      expect((footer.getByRole('button', { name: copy.routeDialog.save }) as HTMLButtonElement).disabled).toBe(true);
      await act(async () => pending.reject(new Error('offline')));
      expect(screen.getByRole('alert').textContent).toContain(copy.routing.previewFailed);
      await user.click(screen.getByRole('button', { name: copy.routeDialog.retry }));
      await screen.findByText(copy.routing.preview.automatic);
      await user.click(footer.getByRole('button', { name: copy.routing.undoRestore }));
      expect(screen.getByText(copy.routing.undoDone)).toBeTruthy();
    });

    it('reconciles an ambiguous DELETE by inherited intent, retries only failed reads and never repeats the write', async () => {
      const user = userEvent.setup();
      const committed = vi.fn();
      vi.spyOn(modelsApi, 'previewAgentChain').mockResolvedValue(inherited);
      const restore = vi.spyOn(modelsApi, 'restoreAgentChain').mockRejectedValue(new Error('lost response'));
      const put = vi.spyOn(modelsApi, 'putAgentChain');
      renderDialog(committed);
      vi.mocked(modelsApi.getAgentChain).mockRejectedValueOnce(new Error('offline')).mockResolvedValue(unconfigured);
      await clear(user);
      await screen.findByText('After restore: automatic matching');
      await user.click(screen.getByRole('button', { name: 'Save' }));
      await user.click(await screen.findByRole('button', { name: 'Retry' }));
      await waitFor(() => expect(modelsApi.getAgentChain).toHaveBeenCalledTimes(2));
      await waitFor(() => expect((screen.getByRole('button', { name: 'Retry' }) as HTMLButtonElement).disabled).toBe(false));
      expect(committed).not.toHaveBeenCalled();
      await user.click(screen.getByRole('button', { name: 'Retry' }));
      await within(document.querySelector<HTMLElement>('.model-hub-route-foot')!).findByRole('button', { name: 'Done' });
      expect(committed).toHaveBeenCalledWith({ chain: unconfigured, removed_hops: null, interrupted: null });
      expect(restore).toHaveBeenCalledOnce();
      expect(put).not.toHaveBeenCalled();
    });
  });

  it('refreshes inherited routing after returning from Default routing', async () => {
    const inherited: AgentChain = { ...chain, manual_override: null, route_origin: 'automatic' };
    const next: AgentChain = { ...inherited, chain: [chain.chain[1]] };
    const read = vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValueOnce(inherited).mockResolvedValue(next);
    const props = { selection: { agent, modelId: 'opus-5', read: readyRegion(inherited) }, sources, onClose: vi.fn(), readAgents: vi.fn(), readSources: vi.fn() };
    const view = (covered: boolean) => <I18nextProvider i18n={i18n}><RouteChainDialog {...props} covered={covered} /></I18nextProvider>;
    const { rerender } = render(view(false));
    await screen.findByRole('button', { name: 'Edit route' });
    rerender(view(true));
    rerender(view(false));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(document.querySelectorAll('.model-hub-route-hop-model')).toHaveLength(1));
    expect(document.querySelector('.model-hub-route-hop-model')?.textContent).toBe('opus-5');
  });

  it('refreshes restore preview without losing the earlier manual draft', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(chain);
    const inherited: AgentChain = { ...chain, manual_override: null, route_origin: 'automatic' };
    const preview = vi.spyOn(modelsApi, 'previewAgentChain').mockResolvedValueOnce(inherited).mockResolvedValue({ ...inherited, chain: [chain.chain[0]] });
    const props = { selection: { agent, modelId: 'opus-5', read: readyRegion(chain) }, sources, onClose: vi.fn(), readAgents: vi.fn(), readSources: vi.fn() };
    const view = (covered: boolean) => <I18nextProvider i18n={i18n}><RouteChainDialog {...props} covered={covered} /></I18nextProvider>;
    const { rerender } = render(view(false));
    await screen.findAllByRole('button', { name: 'Remove hop' });
    await user.click(screen.getAllByRole('button', { name: 'Remove hop' })[0]);
    await user.click(screen.getByRole('button', { name: 'Restore automatic' }));
    await screen.findByRole('button', { name: 'Undo restore' });
    rerender(view(true));
    rerender(view(false));
    await waitFor(() => expect(preview).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(document.querySelectorAll('.model-hub-route-hop-model')).toHaveLength(1));
    expect(document.querySelector('.model-hub-route-hop-model')?.textContent).toBe('claude-opus-5');
    await user.click(screen.getByRole('button', { name: 'Undo restore' }));
    expect(document.querySelector('.model-hub-route-hop-model')?.textContent).toBe('opus-5');
  });

  it('previews restore, undoes to the unsaved manual draft, and cancels without writing', async () => {
    const user = userEvent.setup();
    const inherited: AgentChain = { ...chain, manual_override: null, route_origin: 'passthrough' };
    const preview = vi.spyOn(modelsApi, 'previewAgentChain').mockResolvedValue(inherited);
    const put = vi.spyOn(modelsApi, 'putAgentChain');
    const restore = vi.spyOn(modelsApi, 'restoreAgentChain');
    const close = vi.fn();
    renderDialog(vi.fn(), close);
    await screen.findAllByRole('button', { name: 'Remove hop' });
    await user.click(screen.getAllByRole('button', { name: 'Remove hop' })[0]);
    await user.click(screen.getByRole('button', { name: 'Restore automatic' }));
    await screen.findByText('After restore: original-name passthrough');
    expect(preview).toHaveBeenCalledWith('claude', 'opus-5', { manual_override: null });
    expect(screen.queryByRole('button', { name: 'Remove hop' })).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Undo restore' }));
    expect(screen.getAllByRole('button', { name: 'Remove hop' })).toHaveLength(1);
    expect(document.querySelector('.model-hub-route-hop-model')?.textContent).toBe('opus-5');
    await user.click(screen.getAllByRole('button', { name: 'Cancel' }).at(-1)!);
    expect(close).toHaveBeenCalled();
    expect(put).not.toHaveBeenCalled();
    expect(restore).not.toHaveBeenCalled();
  });

  it('saves restored intent with DELETE even when automatic hops equal the manual hops', async () => {
    const user = userEvent.setup();
    const inherited: AgentChain = { ...chain, manual_override: null, route_origin: 'automatic' };
    vi.spyOn(modelsApi, 'previewAgentChain').mockResolvedValue(inherited);
    const restore = vi.spyOn(modelsApi, 'restoreAgentChain').mockResolvedValue(mutation(inherited));
    const put = vi.spyOn(modelsApi, 'putAgentChain');
    renderDialog();
    await user.click(await screen.findByRole('button', { name: 'Restore automatic' }));
    await screen.findByRole('button', { name: 'Undo restore' });
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(restore).toHaveBeenCalledWith('claude', 'opus-5', undefined));
    expect(put).not.toHaveBeenCalled();
  });

  it('saving an explicit edit of identical inherited hops creates manual intent', async () => {
    const user = userEvent.setup();
    const inherited: AgentChain = { ...chain, manual_override: null, route_origin: 'automatic' };
    renderDialog();
    vi.mocked(modelsApi.getAgentChain).mockResolvedValue(inherited);
    // Reopen on a fresh mount so the initial read observes inherited intent.
    cleanup();
    const put = vi.spyOn(modelsApi, 'putAgentChain').mockResolvedValue(mutation(chain));
    render(<I18nextProvider i18n={i18n}><RouteChainDialog selection={{ agent, modelId: 'opus-5', read: readyRegion(inherited) }} sources={[]} onClose={vi.fn()} readAgents={vi.fn()} readSources={vi.fn()} /></I18nextProvider>);
    await user.click(await screen.findByRole('button', { name: 'Edit route' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(put).toHaveBeenCalledWith('claude', 'opus-5', { hops: chain.manual_override!.hops }));
  });

  it('accepts an exact unlisted API-key target without creating inventory', async () => {
    const user = userEvent.setup();
    renderStockedDialog();
    await user.click(await screen.findByRole('button', { name: 'Add a hop' }));
    expect(screen.getByLabelText('Source').querySelectorAll('option')).toHaveLength(1);
    await user.type(screen.getByLabelText('Exact model ID'), 'unlisted-model');
    await user.click(screen.getByRole('button', { name: 'Add' }));
    expect([...document.querySelectorAll('.model-hub-route-hop-model')].at(-1)?.textContent).toBe('unlisted-model');
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false);
  });
  it("matches a suspended Direct attempt by persisted intent and ordered pairs", () => {
    const attempt = {
      backend: "claude" as const,
      modelId: "opus-5",
      stage: "initial" as const,
      manual_override: { hops: [{ source_id: "src_b", model_id: "opus-5" }, { source_id: "src_a", model_id: "claude-opus-5" }] },
      submitted: [
        { source_id: "src_b", model_id: "opus-5" },
        { source_id: "src_a", model_id: "claude-opus-5" },
      ],
    };
    expect(
      routeChainMatchesAttempt(
        {
          ...chain,
          manual_override: attempt.manual_override,
          chain: [chain.chain[1], chain.chain[0]],
        },
        attempt,
      ),
    ).toBe(true);
    expect(routeChainMatchesAttempt(chain, attempt)).toBe(false);
  });
  it("renders the exact read projection and keeps the current hop identifiable", async () => {
    renderDialog();
    await screen.findAllByRole("button", { name: "Remove hop" });

    expect(screen.getByText("opus-5 · Route chain")).toBeTruthy();
    const current = document.querySelector('[data-current="true"]');
    expect(current?.textContent).toContain("Claude subscription");
    expect(current?.textContent).toContain("opus-5");
    const ordinals = [...document.querySelectorAll(".model-hub-route-ordinal")];
    expect(ordinals[0]?.className).toContain("model-hub-accent-pill--mint");
    expect(ordinals[1]?.className).toContain("model-hub-fill-0a");
    expect(screen.getAllByRole("button", { name: "Remove hop" })).toHaveLength(
      2,
    );
    expect(
      (screen.getByRole("button", { name: "Save" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("saves the changed ordered hop array and consumes the response envelope", async () => {
    const user = userEvent.setup();
    const onCommitted = vi.fn();
    const put = vi
      .spyOn(modelsApi, "putAgentChain")
      .mockResolvedValue(mutation());
    renderDialog(onCommitted);
    await screen.findAllByRole("button", { name: "Remove hop" });

    await user.click(screen.getAllByRole("button", { name: "Remove hop" })[0]);
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("claude", "opus-5", {
        hops: [{ source_id: "src_b", model_id: "opus-5" }],
      }),
    );
    expect(onCommitted).toHaveBeenCalledWith({
      chain,
      removed_hops: [],
      interrupted: [],
    });
    expect(screen.getByText("Done").closest("button")).toBeTruthy();
  });

  it("MH-ROUTE-EDIT-001 replaces one hop in place and saves the exact order", async () => {
    const user = userEvent.setup();
    const put = vi
      .spyOn(modelsApi, "putAgentChain")
      .mockResolvedValue(mutation());
    renderStockedDialog();
    const editButtons = await screen.findAllByRole("button", {
      name: "Edit hop",
    });

    await user.click(editButtons[0]);
    expect(
      [...document.querySelectorAll(".model-hub-route-candidate-model")].map(
        (element) => element.textContent,
      ),
    ).toEqual([
      "claude-haiku-5",
      "claude-opus-5",
      "claude-sonnet-5",
      "sonnet-5",
    ]);
    await user.type(
      screen.getByPlaceholderText("Search sources or models"),
      "claude-sonnet-5",
    );
    await user.click(screen.getByRole("button", { name: "Replace" }));

    await waitFor(() =>
      expect(
        [...document.querySelectorAll(".model-hub-route-hop-model")].map(
          (element) => element.textContent,
        ),
      ).toEqual(["claude-sonnet-5", "opus-5"]),
    );
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("claude", "opus-5", {
        hops: [
          { source_id: "src_a", model_id: "claude-sonnet-5" },
          { source_id: "src_b", model_id: "opus-5" },
        ],
      }),
    );
  });

  it("renders the complete removed-hop impact after a successful save", async () => {
    const user = userEvent.setup();
    const removed = {
      backend: "claude" as const,
      menu_model: "opus-5",
      source_id: "src_a",
      model_id: "claude-opus-5",
      position: 1,
    };
    vi.spyOn(modelsApi, "putAgentChain").mockResolvedValue(
      mutation(chain, { removed_hops: [removed] }),
    );
    renderDialog();
    await screen.findAllByRole("button", { name: "Remove hop" });

    await user.click(screen.getAllByRole("button", { name: "Remove hop" })[0]);
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Claude Code · opus-5")).toBeTruthy();
    expect(screen.getByText("claude-opus-5 · Order #1")).toBeTruthy();
  });

  it("keeps Done available while page-owned M6 is pending and failed", async () => {
    const user = userEvent.setup();
    const removed = {
      backend: "claude" as const,
      menu_model: "opus-5",
      source_id: "src_a",
      model_id: "claude-opus-5",
      position: 1,
    };
    const put = vi
      .spyOn(modelsApi, "putAgentChain")
      .mockResolvedValue(mutation(chain, { removed_hops: [removed] }));
    const onCommitted = vi.fn();
    const retry = vi.fn();
    vi.spyOn(modelsApi, "getAgentChain").mockResolvedValue(chain);
    const page = render(
      <I18nextProvider i18n={i18n}>
        <RouteChainDialog
          selection={{ agent, modelId: "opus-5", read: readyRegion(chain) }}
          sources={sources}
          onClose={vi.fn()}
          onCommitted={onCommitted}
          commitReconciliation={{ pending: true, failed: false, retry }}
          readAgents={vi.fn().mockResolvedValue(observation([agent]))}
          readSources={vi.fn().mockResolvedValue(observation(sources))}
        />
      </I18nextProvider>,
    );
    await screen.findAllByRole("button", { name: "Remove hop" });
    await user.click(screen.getAllByRole("button", { name: "Remove hop" })[0]);
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText(
        "Route chain saved. Refreshing the model surface…",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "These are the items this save actually removed or interrupted.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("Done").closest("button")).toBeTruthy();
    page.rerender(
      <I18nextProvider i18n={i18n}>
        <RouteChainDialog
          selection={{ agent, modelId: "opus-5", read: readyRegion(chain) }}
          sources={sources}
          onClose={vi.fn()}
          onCommitted={onCommitted}
          commitReconciliation={{ pending: false, failed: true, retry }}
          readAgents={vi.fn().mockResolvedValue(observation([agent]))}
          readSources={vi.fn().mockResolvedValue(observation(sources))}
        />
      </I18nextProvider>,
    );
    expect(
      await screen.findByText(
        "The route chain was saved, but the model surface could not be refreshed.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "These are the items this save actually removed or interrupted.",
      ),
    ).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("echoes the exact refusal plan on a forced confirmation", async () => {
    const user = userEvent.setup();
    const gap = {
      backend: "claude" as const,
      model_id: "opus-5",
      agents: ["writer"],
    };
    const hop = {
      backend: "claude" as const,
      menu_model: "opus-5",
      source_id: "src_a",
      model_id: "claude-opus-5",
      position: 1,
    };
    const put = vi
      .spyOn(modelsApi, "putAgentChain")
      .mockRejectedValueOnce(
        new ApiCallError(
          "source_last_supplier",
          undefined,
          true,
          [gap],
          [],
          [hop],
        ),
      )
      .mockResolvedValueOnce(mutation());
    renderDialog();
    await screen.findAllByRole("button", { name: "Remove hop" });

    await user.click(screen.getAllByRole("button", { name: "Remove hop" })[1]);
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByRole("button", { name: "Save anyway" }),
    ).toBeTruthy();
    expect(screen.getByText("Save the route chain for opus-5")).toBeTruthy();
    expect(screen.getByText("Hops that will be removed")).toBeTruthy();
    expect(screen.getByText("1 hop")).toBeTruthy();
    expect(
      screen.getByText((_, element) =>
        element?.classList.contains("model-hub-guard-hop") === true &&
        element.textContent?.includes("Order #1") === true,
      ),
    ).toBeTruthy();
    expect(
      screen.getByText("Some models will be left with no usable source."),
    ).toBeTruthy();
    expect(
      screen.queryByText("Models that will be left with no source"),
    ).toBeNull();
    await user.click(screen.getByRole("button", { name: "Save anyway" }));

    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith("claude", "opus-5", {
        hops: [{ source_id: "src_a", model_id: "claude-opus-5" }],
        force: true,
        would_remove_hops: [hop],
        would_interrupt: [gap],
      }),
    );
  });

  it("re-reads the exact chain after an unconfirmed write and never retries the PUT", async () => {
    const user = userEvent.setup();
    const committedChain: AgentChain = {
      ...chain,
      manual_override: { hops: [{ source_id: "src_b", model_id: "opus-5" }] },
      current: { source_id: "src_b", model_id: "opus-5" },
      chain: [chain.chain[1]],
    };
    const read = vi
      .spyOn(modelsApi, "getAgentChain")
      .mockResolvedValueOnce(chain)
      .mockResolvedValueOnce(committedChain);
    const put = vi
      .spyOn(modelsApi, "putAgentChain")
      .mockRejectedValueOnce(new TypeError("response lost"));
    renderDialog();
    await screen.findAllByRole("button", { name: "Remove hop" });

    await user.click(screen.getAllByRole("button", { name: "Remove hop" })[0]);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await user.click(await screen.findByRole("button", { name: "Retry" }));

    await waitFor(() => expect(read).toHaveBeenCalledWith("claude", "opus-5"));
    expect(put).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Done").closest("button")).toBeTruthy();
  });

  it("installs a nonmatching D-36 observation without treating it as a retryable write", async () => {
    const user = userEvent.setup();
    const observed: AgentChain = {
      ...chain,
      manual_override: { hops: [{ source_id: "src_a", model_id: "claude-opus-5" }] },
      chain: [chain.chain[0]],
      current: null,
      supply_state: "interrupted",
    };
    const read = vi
      .spyOn(modelsApi, "getAgentChain")
      .mockResolvedValueOnce(chain)
      .mockResolvedValueOnce(observed);
    const put = vi
      .spyOn(modelsApi, "putAgentChain")
      .mockRejectedValueOnce(new TypeError("response lost"));
    const onObserved = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <RouteChainDialog
          selection={{ agent, modelId: "opus-5", read: readyRegion(chain) }}
          sources={sources}
          onClose={vi.fn()}
          onObserved={onObserved}
          readAgents={vi.fn().mockResolvedValue(observation([agent]))}
          readSources={vi.fn().mockResolvedValue(observation(sources))}
        />
      </I18nextProvider>,
    );
    await screen.findAllByRole("button", { name: "Remove hop" });
    await user.click(screen.getAllByRole("button", { name: "Remove hop" })[0]);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await user.click(await screen.findByRole("button", { name: "Retry" }));

    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(put).toHaveBeenCalledTimes(1);
    expect(onObserved).toHaveBeenCalledWith(observed);
    expect(screen.getByText("The save outcome is not confirmed")).toBeTruthy();
  });

  it("runs D-36 Agents-first and retries only a failed Source member", async () => {
    const user = userEvent.setup();
    const nonmatching: AgentChain = {
      ...chain,
      manual_override: { hops: [{ source_id: "src_a", model_id: "claude-opus-5" }] },
      chain: [chain.chain[0]],
      current: null,
      supply_state: "interrupted",
    };
    const agentRead = deferred<RouteCollectionObservation<AgentSupply[]>>();
    const readAgents = vi.fn().mockReturnValue(agentRead.promise);
    const readSources = vi
      .fn()
      .mockRejectedValueOnce(new Error("sources unread"))
      .mockResolvedValueOnce(observation(sources));
    const readChain = vi
      .spyOn(modelsApi, "getAgentChain")
      .mockResolvedValueOnce(chain)
      .mockResolvedValueOnce(nonmatching)
      .mockResolvedValueOnce(nonmatching);
    const put = vi
      .spyOn(modelsApi, "putAgentChain")
      .mockRejectedValueOnce(new TypeError("response lost"));
    render(
      <I18nextProvider i18n={i18n}>
        <RouteChainDialog
          selection={{ agent, modelId: "opus-5", read: readyRegion(chain) }}
          sources={sources}
          onClose={vi.fn()}
          readAgents={readAgents}
          readSources={readSources}
        />
      </I18nextProvider>,
    );
    await screen.findAllByRole("button", { name: "Remove hop" });
    await user.click(screen.getAllByRole("button", { name: "Remove hop" })[0]);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(readChain).toHaveBeenCalledTimes(1);
    expect(readSources).not.toHaveBeenCalled();

    agentRead.resolve(observation([agent]));
    expect(
      await screen.findByText("The current model surface could not be read"),
    ).toBeTruthy();
    expect(readChain).toHaveBeenCalledTimes(2);
    expect(readSources).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(readSources).toHaveBeenCalledTimes(2));
    expect(readChain).toHaveBeenCalledTimes(2);
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(readChain).toHaveBeenCalledTimes(3));
    expect(readSources).toHaveBeenCalledTimes(2);
    expect(put).toHaveBeenCalledTimes(1);
  });

  it("returns an invalidated pending draft to editing after a named rejection", async () => {
    const user = userEvent.setup();
    const pending = deferred<AgentChainMutation>();
    const { agent: editableAgent, sources: editableSources } = stocked();
    vi.spyOn(modelsApi, "getAgentChain").mockResolvedValue(chain);
    vi.spyOn(modelsApi, "putAgentChain").mockReturnValue(pending.promise);
    const props = {
      selection: {
        agent: editableAgent,
        modelId: "opus-5",
        read: readyRegion(chain),
      },
      onClose: vi.fn(),
      readAgents: vi.fn().mockResolvedValue(observation([editableAgent])),
      readSources: vi.fn().mockResolvedValue(observation(editableSources)),
    };
    const page = render(
      <I18nextProvider i18n={i18n}>
        <RouteChainDialog {...props} sources={editableSources} />
      </I18nextProvider>,
    );
    await screen.findAllByRole("button", { name: "Remove hop" });
    await user.click(screen.getByRole("button", { name: "Add a hop" }));
    await user.click(screen.getByRole("button", { name: "Add" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findAllByText("Saving route chain…")).toHaveLength(2);

    const refreshedSources = editableSources.map((source) =>
      source.id === "src_a"
        ? { ...source, models: source.models.map((model, index) => index === 0 ? model : { ...model, retired: true }) }
        : source,
    );
    page.rerender(
      <I18nextProvider i18n={i18n}>
        <RouteChainDialog {...props} sources={refreshedSources} />
      </I18nextProvider>,
    );
    await act(async () => {
      pending.reject(new ApiCallError("invalid_route"));
      await pending.promise.catch(() => undefined);
    });

    expect(
      await screen.findAllByText(
        "This edited hop is unavailable after the refresh. Replace or remove it before saving.",
      ),
    ).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Remove hop" })).toHaveLength(3);
    expect(
      (screen.getByRole("button", { name: "Save" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("names both candidate columns and prints each source once per group", async () => {
    const user = userEvent.setup();
    renderStockedDialog();
    await screen.findAllByRole("button", { name: "Remove hop" });

    await user.click(screen.getByRole("button", { name: "Add a hop" }));

    expect(screen.getAllByText("Source").length).toBeGreaterThan(0);
    expect(screen.getByText("Model")).toBeTruthy();
    // Both halves of a candidate are legible: the model id AND which source
    // supplies it. The source column is what a sizing rework once dropped, so
    // the assertion is over the rendered row grid, not over the mere presence
    // of the name somewhere in the panel.
    const rows = [...document.querySelectorAll(".model-hub-route-candidate")];
    expect(
      rows.map((row) => [
        row.querySelector(".model-hub-route-candidate-source")?.textContent,
        row.querySelector(".model-hub-route-candidate-model")?.textContent,
      ]),
    ).toEqual([
      ["API key", "claude-haiku-5"],
      ["", "claude-sonnet-5"],
      ["Claude subscription", "sonnet-5"],
    ]);
  });

  it("narrows the candidates to what was typed without claiming none exist", async () => {
    const user = userEvent.setup();
    renderStockedDialog();
    await screen.findAllByRole("button", { name: "Remove hop" });
    await user.click(screen.getByRole("button", { name: "Add a hop" }));

    await user.type(
      screen.getByPlaceholderText("Search sources or models"),
      "subscription",
    );

    expect(
      [...document.querySelectorAll(".model-hub-route-candidate-model")].map(
        (cell) => cell.textContent,
      ),
    ).toEqual(["sonnet-5"]);

    await user.clear(screen.getByPlaceholderText("Search sources or models"));
    await user.type(
      screen.getByPlaceholderText("Search sources or models"),
      "gpt",
    );

    expect(screen.getByText("No source or model matches that")).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Add" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("keeps the candidate list scrollable while the dialog locks the page", async () => {
    const user = userEvent.setup();
    renderStockedDialog();
    await screen.findAllByRole("button", { name: "Remove hop" });
    await user.click(screen.getByRole("button", { name: "Add a hop" }));

    // The dialog locks page scrolling through `react-remove-scroll`, which
    // cancels wheel events outside its own lock and shards. The selector is
    // portalled to `document.body`, so it is outside both unless it owns a lock
    // of its own — and when it does not, the panel keeps its `overflow-y: auto`
    // and its overflowing content while the wheel silently does nothing. Only
    // the event outcome can see that, so it is measured here rather than
    // inferred from the CSS. jsdom has no layout, so the scrollability the
    // browser gets from that CSS is stated explicitly.
    const list = document.querySelector<HTMLElement>(
      ".model-hub-route-selector-list",
    );
    expect(list).not.toBeNull();
    (list as HTMLElement).style.overflowY = "auto";
    Object.defineProperty(list, "scrollHeight", {
      value: 1000,
      configurable: true,
    });
    Object.defineProperty(list, "clientHeight", {
      value: 200,
      configurable: true,
    });
    const wheelPrevented = (node: Element) => {
      const event = new WheelEvent("wheel", {
        bubbles: true,
        cancelable: true,
        deltaY: 240,
      });
      node.dispatchEvent(event);
      return event.defaultPrevented;
    };

    expect(
      wheelPrevented(document.querySelector(".model-hub-route-candidate")!),
    ).toBe(false);
    expect(
      wheelPrevented(document.body.appendChild(document.createElement("div"))),
    ).toBe(true);
  });

  it("announces the one-based position of the hop focused after removal", async () => {
    const user = userEvent.setup();
    const threeHopChain: AgentChain = {
      ...chain,
      chain: [
        chain.chain[0],
        chain.chain[1],
        {
          ...chain.chain[0],
          source_id: "src_c",
          model_id: "claude-haiku-5",
        },
      ],
    };
    vi.spyOn(modelsApi, "getAgentChain").mockResolvedValue(threeHopChain);
    render(
      <I18nextProvider i18n={i18n}>
        <RouteChainDialog
          selection={{ agent, modelId: "opus-5", read: readyRegion(threeHopChain) }}
          sources={sources}
          onClose={vi.fn()}
          readAgents={vi.fn().mockResolvedValue(observation([agent]))}
          readSources={vi.fn().mockResolvedValue(observation(sources))}
        />
      </I18nextProvider>,
    );
    const removeButtons = await screen.findAllByRole("button", {
      name: "Remove hop",
    });

    await user.click(removeButtons[1]);

    expect(screen.getByText("Moved to hop 2.")).toBeTruthy();
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getAllByRole("button", { name: "Reorder this hop" })[1],
      ),
    );
  });

  it("cancels only the active grab and preserves the earlier unsaved draft", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderDialog(vi.fn(), onClose);
    const grips = await screen.findAllByRole("button", {
      name: "Reorder this hop",
    });

    await user.click(grips[0]);
    await user.keyboard("{Space}");
    await user.keyboard("{ArrowDown}");
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getAllByRole("button", { name: "Reorder this hop" })[1],
      ),
    );
    await user.keyboard("{Space}");
    expect(
      screen.getAllByRole("button", { name: "Reorder this hop" })[1]
        .getAttribute("aria-grabbed"),
    ).toBe("false");
    const unsavedOrder = ["Claude subscription", "API key"];
    expect(
      [...document.querySelectorAll(".model-hub-route-hop-name")].map(
        (node) => node.textContent,
      ),
    ).toEqual(unsavedOrder);
    expect(
      (screen.getByRole("button", { name: "Save" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);

    await user.keyboard("{Space}");
    expect(
      screen.getAllByRole("button", { name: "Reorder this hop" })[1]
        .getAttribute("aria-grabbed"),
    ).toBe("true");
    await user.keyboard("{ArrowUp}");
    expect(screen.getByText("Moved to hop 1.")).toBeTruthy();
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getAllByRole("button", { name: "Reorder this hop" })[0],
      ),
    );
    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
    expect(
      screen.getByText("Reorder cancelled. Restored to hop 2."),
    ).toBeTruthy();
    expect(
      [...document.querySelectorAll(".model-hub-route-hop-name")].map(
        (node) => node.textContent,
      ),
    ).toEqual(unsavedOrder);
    const restoredGrips = screen.getAllByRole("button", {
      name: "Reorder this hop",
    });
    expect(restoredGrips[1].getAttribute("aria-grabbed")).toBe("false");
    await waitFor(() => expect(document.activeElement).toBe(restoredGrips[1]));
    expect(
      (screen.getByRole("button", { name: "Save" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });

  it("keeps the native Escape dismissal when no hop is grabbed", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderDialog(vi.fn(), onClose);
    await screen.findAllByRole("button", { name: "Reorder this hop" });

    await user.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
