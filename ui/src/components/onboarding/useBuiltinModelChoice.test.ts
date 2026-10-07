// @vitest-environment jsdom
import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { chosenCandidate } from '../settings/models/backendCatalog';
import { ApiCallError } from '../settings/models/modelsApi';
import type { AgentSupply, BackendModel, BackendModelCandidates, ModelCandidate } from '../settings/models/types';
import { useBuiltinModelChoice, type BuiltinModelChoiceDeps } from './useBuiltinModelChoice';

const candidate = (id: string, reasoning_efforts: string[] = []): ModelCandidate => ({
  id, display_name: null, reasoning_efforts, origin: 'provider',
  suppliers: [{ source_id: 'src_lab', source_name: 'Lab', model_id: id }],
});
const read = (...ids: string[]): BackendModelCandidates => ({ builtin: [], providers: ids.map((id) => candidate(id)), in_list: [] });
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

/**
 * A server with the one behaviour this hook exists for: committing a model to the list
 * of a backend whose Agent has none fills that Agent with the list's first row, as the
 * controller's catalog reconciliation does.
 */
function server(initial: { catalog?: string[]; model?: string | null; effort?: string | null } = {}) {
  const state = {
    catalog: (initial.catalog ?? []).map((id) => ({ id, reasoning_efforts: [] }) as unknown as BackendModel),
    model: initial.model ?? null,
    effort: initial.effort ?? null,
  };
  const agent = () => ({ ok: true, agent: { name: 'vibey', model: state.model, reasoning_effort: state.effort }, default_agent_name: null });
  const api = {
    getVibeAgent: vi.fn(async () => agent()),
    updateVibeAgent: vi.fn(async (_name: string, payload: { model?: string; reasoning_effort?: string | null }) => {
      state.model = payload.model ?? state.model;
      if ('reasoning_effort' in payload) state.effort = payload.reasoning_effort ?? null;
      return agent();
    }),
  };
  const models = {
    getAgentModelCandidates: vi.fn(async () => read('lab-large', 'lab-mini')),
    putAgentModels: vi.fn(async (_backend: string, body: { models: BackendModel[] }) => {
      state.catalog = body.models;
      if (!state.model && state.catalog.length) state.model = state.catalog[0].id;
      return {} as AgentSupply;
    }),
  };
  const supply = (): AgentSupply[] => [{ backend: 'vibey', mode: 'hub', menu_kind: 'fixed', catalog_models: state.catalog } as AgentSupply];
  const agentReads = {
    read: async () => ({ kind: 'current' as const, value: supply() }),
    refresh: vi.fn(async () => ({ kind: 'current' as const, value: supply() })),
    readValue: async () => supply(),
    invalidate: () => undefined,
  };
  return { state, api, models, agentReads };
}

function mount(fixture: ReturnType<typeof server>, overrides: Partial<BuiltinModelChoiceDeps> = {}) {
  let epoch = 1;
  const onSettled = vi.fn();
  const props = (): BuiltinModelChoiceDeps => ({
    api: fixture.api as unknown as BuiltinModelChoiceDeps['api'],
    models: fixture.models as unknown as BuiltinModelChoiceDeps['models'],
    agentReads: fixture.agentReads,
    epoch: () => epoch,
    target: () => 'vibey',
    onSettled,
    ...overrides,
  });
  const view = renderHook(() => useBuiltinModelChoice(props()));
  return {
    view, onSettled,
    leaveAndReturn: () => { epoch += 1; view.rerender(); },
    leave: () => { epoch += 1; },
    returnTo: () => view.rerender(),
  };
}

describe('the built-in model choice', () => {
  it('writes a pick the list lacks, names it, and stands once a read shows it', async () => {
    const fixture = server();
    const { view, onSettled } = mount(fixture);
    await act(async () => view.result.current.choose('vibey', chosenCandidate(candidate('lab-mini'))));
    expect(fixture.models.putAgentModels).toHaveBeenCalledWith('vibey', expect.objectContaining({
      baseline: [], expected_suppliers: { 'lab-mini': [{ source_id: 'src_lab', model_id: 'lab-mini' }] },
    }));
    expect(fixture.state.model).toBe('lab-mini');
    expect(view.result.current.holds('vibey')).toBe(false);
    expect(view.result.current.failure('vibey')).toBeNull();
    expect(onSettled).toHaveBeenCalledWith('vibey');
  });

  it('only names a model the list already holds', async () => {
    const fixture = server({ catalog: ['lab-mini'] });
    const { view } = mount(fixture);
    await act(async () => view.result.current.choose('vibey', chosenCandidate(candidate('lab-mini'))));
    expect(fixture.models.putAgentModels).not.toHaveBeenCalled();
    expect(fixture.state.model).toBe('lab-mini');
  });

  it('holds a choice the server filled with another model until Retry applies it', async () => {
    // The list already holds another row, so committing the pick lets the server fill
    // the Agent with that row — and then naming the pick fails.
    const fixture = server({ catalog: ['lab-large'] });
    fixture.api.updateVibeAgent.mockRejectedValueOnce(new Error('offline'));
    const { view } = mount(fixture);
    await act(async () => view.result.current.choose('vibey', chosenCandidate(candidate('lab-mini'))));
    expect(fixture.state.model).toBe('lab-large');
    expect(view.result.current.failure('vibey')).toEqual({ model: 'lab-mini', kind: 'failed' });
    expect(view.result.current.holds('vibey')).toBe(true);

    await act(async () => view.result.current.retry('vibey'));
    expect(fixture.models.putAgentModels).toHaveBeenCalledOnce();
    expect(fixture.state.model).toBe('lab-mini');
    expect(view.result.current.holds('vibey')).toBe(false);
  });

  it('settles a lost reply by the read: a write that landed stands', async () => {
    const fixture = server();
    fixture.api.updateVibeAgent.mockImplementationOnce(async (_name, payload) => {
      fixture.state.model = payload.model ?? null;
      throw new Error('reply lost');
    });
    const { view } = mount(fixture);
    await act(async () => view.result.current.choose('vibey', chosenCandidate(candidate('lab-mini'))));
    expect(view.result.current.holds('vibey')).toBe(false);
    expect(view.result.current.failure('vibey')).toBeNull();
  });

  it('drops the rows a refusal was about and reads today\'s, without naming anything', async () => {
    const fixture = server();
    const { view } = mount(fixture);
    await act(async () => view.result.current.read('vibey'));
    expect(view.result.current.offer('vibey')?.kind).toBe('ready');
    fixture.models.putAgentModels.mockRejectedValueOnce(new ApiCallError('candidate_suppliers_changed', undefined, true, [], [], [], 409));
    const today = deferred<BackendModelCandidates>();
    fixture.models.getAgentModelCandidates.mockReturnValueOnce(today.promise);
    await act(async () => view.result.current.choose('vibey', chosenCandidate(candidate('lab-mini'))));
    expect(fixture.api.updateVibeAgent).not.toHaveBeenCalled();
    expect(view.result.current.failure('vibey')).toEqual({ model: 'lab-mini', kind: 'suppliersChanged' });
    // The refusal ends the choice; nothing is held, and no refused row is offered.
    expect(view.result.current.holds('vibey')).toBe(false);
    expect(view.result.current.offer('vibey')?.kind).toBe('loading');
    await act(async () => today.resolve(read('lab-pro')));
    const offer = view.result.current.offer('vibey');
    expect(offer?.kind === 'ready' && offer.read.providers.map((row) => row.id)).toEqual(['lab-pro']);
  });

  it('moves the Agent\'s effort with its model by the editor\'s rule', async () => {
    for (const [efforts, before, after] of [
      [['low', 'medium', 'high'], 'xhigh', 'medium'],
      [[], 'high', null],
      [['low', 'high'], 'high', 'high'],
    ] as const) {
      const fixture = server({ effort: before });
      const { view } = mount(fixture);
      await act(async () => view.result.current.choose('vibey', chosenCandidate(candidate('lab-mini', [...efforts]))));
      expect(fixture.state.effort).toBe(after);
      const payload = fixture.api.updateVibeAgent.mock.calls[0][1];
      expect('reasoning_effort' in payload).toBe(after !== before);
      view.unmount();
    }
  });

  it('writes one choice at a time', async () => {
    const fixture = server();
    const put = deferred<AgentSupply>();
    fixture.models.putAgentModels.mockReturnValueOnce(put.promise);
    const { view } = mount(fixture);
    let first!: Promise<void>;
    act(() => { first = view.result.current.choose('vibey', chosenCandidate(candidate('lab-mini'))); });
    await waitFor(() => expect(view.result.current.picking('vibey')).toBe('lab-mini'));
    await act(async () => view.result.current.choose('vibey', chosenCandidate(candidate('lab-large'))));
    expect(fixture.models.putAgentModels).toHaveBeenCalledOnce();
    await act(async () => { put.resolve({} as AgentSupply); await first; });
    expect(fixture.api.updateVibeAgent).toHaveBeenCalledOnce();
  });

  it('offers only what this showing of the screen read', async () => {
    const fixture = server();
    const first = deferred<BackendModelCandidates>();
    const second = deferred<BackendModelCandidates>();
    fixture.models.getAgentModelCandidates.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { view, leave, returnTo } = mount(fixture);
    act(() => { void view.result.current.read('vibey'); });
    leave();
    // The first read lands after the person left: it answers a showing that is over.
    await act(async () => first.resolve(read('lab-old')));
    returnTo();
    expect(view.result.current.offer('vibey')).toBeUndefined();
    act(() => { void view.result.current.read('vibey'); });
    expect(view.result.current.offer('vibey')?.kind).toBe('loading');
    await act(async () => second.resolve(read('lab-new')));
    const offer = view.result.current.offer('vibey');
    expect(offer?.kind === 'ready' && offer.read.providers.map((row) => row.id)).toEqual(['lab-new']);
  });

  it('keeps the rows it shows across a re-read within one showing', async () => {
    const fixture = server();
    const { view } = mount(fixture);
    await act(async () => view.result.current.read('vibey'));
    const again = deferred<BackendModelCandidates>();
    fixture.models.getAgentModelCandidates.mockReturnValueOnce(again.promise);
    act(() => { void view.result.current.read('vibey'); });
    expect(view.result.current.offer('vibey')?.kind).toBe('ready');
    await act(async () => again.resolve(read('lab-large')));
  });
});
