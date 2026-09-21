// @vitest-environment jsdom
// Contract consumers live only in this test; PR0 introduces no feature shell.
import { createElement, useState } from 'react';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { beginRegionRead, failRegionRead, loadingRegion, readyRegion, unreadRegion, type RegionRead } from '../settings/models/regionRead';
import { CONTRACT_VERSION, type RuntimeDependency } from '../settings/models/types';

import {
  INITIAL_SETUP_FLOW_STATE,
  SETUP_SCREENS,
  setupBackTarget,
  setupCandidatePath,
  setupCanAttemptInstall,
  setupCurrentScreen,
  setupPolicy,
  setupCapability,
  setupNavigationReady,
  setupScreenSequence,
  type SetupCapability,
  type SetupFlowState,
  type SetupScreenId,
  type SetupScreenProps,
} from './setupFlow';

afterEach(cleanup);

const runtime = (
  resolution: RuntimeDependency['manifest']['resolution'] = 'resolved',
  health: RuntimeDependency['status']['health'] = 'not_installed',
): RuntimeDependency => ({
  contract_version: CONTRACT_VERSION,
  manifest: resolution === 'unresolved'
    ? { name: 'cliproxyapi', resolution, assets: [] }
    : { name: 'cliproxyapi', resolution, version: 'fixture', source_sha: 'fixture', assets: [] },
  status: { verified: false, health },
});
const policyFor = (capability: SetupCapability) => setupPolicy(capability, readyRegion(runtime()));
const CAPABILITIES: readonly SetupCapability[] = ['pending', 'enabled', 'disabled'];

describe('setup screen sequence', () => {
  it('keeps the flow a prefix-stable ordering of the declared screens', () => {
    // A property rather than three copied lists: whatever the capability says, the flow
    // opens on the introduction, closes on the assistants, and never reorders what is
    // between them — so a screen added later is covered without editing this test.
    for (const capability of CAPABILITIES) {
      const sequence = setupScreenSequence(policyFor(capability));
      expect(sequence[0]).toBe('intro');
      expect(sequence[sequence.length - 1]).toBe('assistants');
      const declared = SETUP_SCREENS.filter((screen) => sequence.includes(screen));
      expect([...sequence]).toEqual([...declared]);
    }
  });

  it('drops only the providers screen when the capability is explicitly off', () => {
    expect(setupScreenSequence(policyFor('disabled'))).toEqual(['intro', 'assistants']);
    expect(setupScreenSequence(policyFor('enabled'))).toEqual([...SETUP_SCREENS]);
  });

  it('maps the capability read without collapsing "unread" into "off"', () => {
    expect(setupCapability(null)).toBe('pending');
    expect(setupCapability(true)).toBe('enabled');
    expect(setupCapability(false)).toBe('disabled');
  });
});

describe('setup navigation readiness', () => {
  it('holds the shell on the introduction until the capability read settles', () => {
    // A returned sequence says which screens exist, not that the shell may enter them:
    // entering the providers screen on a guess and removing it when the read resolves to
    // `disabled` recreates the jump the shorter sequence exists to prevent.
    expect(setupNavigationReady(policyFor('pending'), 'intro')).toBe(false);
    for (const capability of CAPABILITIES.filter((value) => value !== 'pending')) {
      expect(setupNavigationReady(policyFor(capability), 'intro')).toBe(true);
    }
  });
});

describe('setup back target', () => {
  it('leaves to the previous screen of the sequence actually running', () => {
    expect(setupBackTarget(setupScreenSequence(policyFor('enabled')), 'assistants')).toBe('providers');
    expect(setupBackTarget(setupScreenSequence(policyFor('enabled')), 'providers')).toBe('intro');
    // The degraded flow has no providers screen, so Back from the assistants screen is the
    // introduction rather than a screen this instance never renders.
    expect(setupBackTarget(setupScreenSequence(policyFor('disabled')), 'assistants')).toBe('intro');
  });

  it('has nowhere to go from the first screen', () => {
    expect(setupBackTarget(setupScreenSequence(policyFor('enabled')), 'intro')).toBeNull();
    expect(setupBackTarget(setupScreenSequence(policyFor('disabled')), 'intro')).toBeNull();
  });
});

describe('shell-owned flow state', () => {
  it('starts with nothing selected, imported, added or ordered, and no dirty draft', () => {
    expect(INITIAL_SETUP_FLOW_STATE).toEqual({
      providerSelection: { scan: null, selectedBackends: [] },
      importedCount: 0,
      addedThroughMore: [],
      routeOrder: [],
      routeOrderDirty: false,
    });
  });

  it('carries screen updates through navigation and composes a late functional update with newer state', async () => {
    let finishImport: (() => void) | undefined;
    const pendingImport = new Promise<void>((resolve) => { finishImport = resolve; });
    const draft = [
      { source_id: '来源-一', model_id: '模型-首选' },
      { source_id: '来源-一', model_id: '模型-备用' },
    ];
    const selection: SetupFlowState['providerSelection'] = {
      scan: { items: [
        { id: 'key-a', backend: 'opencode', kind: 'opencode_provider', masked_detail: 'fixture…0001', proposed_action: 'import', selected: true },
        { id: 'key-b', backend: 'opencode', kind: 'opencode_provider', masked_detail: 'fixture…0002', proposed_action: 'import', selected: true },
      ] },
      selectedBackends: ['opencode'],
    };
    const ProviderScreen = ({ active, flowState, setFlowState, onNavigate }: SetupScreenProps) =>
      createElement('section', { hidden: !active, inert: !active, 'data-testid': 'providers' },
        createElement('output', null, JSON.stringify(flowState)),
        createElement('button', { onClick: () => {
          setFlowState((previous) => ({ ...previous, providerSelection: selection }));
          // Deliberately settle after another screen has edited the draft.
          void pendingImport.then(() => setFlowState((previous) => ({
            ...previous, importedCount: previous.importedCount + 2,
            providerSelection: { scan: null, selectedBackends: [] },
          })));
          onNavigate('assistants');
        } }, 'Start fixture import'),
        createElement('button', { onClick: () => {
          setFlowState((previous) => ({ ...previous, addedThroughMore: ['新增来源'] }));
          onNavigate('assistants');
        } }, 'Add fixture source'));
    const AssistantScreen = ({ active, flowState, setFlowState, onNavigate }: SetupScreenProps) =>
      createElement('section', { hidden: !active, inert: !active, 'data-testid': 'assistants' },
        createElement('output', null, JSON.stringify(flowState)),
        createElement('button', { onClick: () => {
          setFlowState((previous) => ({ ...previous, routeOrder: draft, routeOrderDirty: true }));
          onNavigate('providers');
        } }, 'Edit fixture route'));
    const Shell = () => {
      const [current, setCurrent] = useState<SetupScreenId>('providers');
      const [flowState, setFlowState] = useState(INITIAL_SETUP_FLOW_STATE);
      const common: Omit<SetupScreenProps, 'active'> = {
        flowState, setFlowState, policy: policyFor('enabled'), onRetryRuntime: () => {},
        handoff: false, onNavigate: setCurrent, onActionChange: () => {},
      };
      return createElement('main', null,
        createElement(ProviderScreen, { ...common, active: current === 'providers' }),
        createElement(AssistantScreen, { ...common, active: current === 'assistants' }));
    };
    render(createElement(Shell));
    const read = (id: string): SetupFlowState => JSON.parse(within(screen.getByTestId(id)).getByRole('status').textContent!);

    fireEvent.click(screen.getByRole('button', { name: 'Start fixture import' }));
    expect(read('assistants').providerSelection).toEqual(selection);
    fireEvent.click(screen.getByRole('button', { name: 'Edit fixture route' }));
    expect(read('providers')).toMatchObject({ providerSelection: selection, routeOrder: draft, routeOrderDirty: true });
    fireEvent.click(screen.getByRole('button', { name: 'Add fixture source' }));
    await act(async () => { finishImport!(); await pendingImport; });
    expect(read('assistants')).toEqual({
      providerSelection: { scan: null, selectedBackends: [] }, importedCount: 2,
      addedThroughMore: ['新增来源'], routeOrder: draft, routeOrderDirty: true,
    });
    expect((screen.getByTestId('providers') as HTMLElement).hidden).toBe(true);
    expect(screen.getByTestId('providers').hasAttribute('inert')).toBe(true);
  });
});


describe('authoritative setup policy', () => {
  it('separates install admission from runtime health and never rewrites persisted mode', () => {
    const healths: RuntimeDependency['status']['health'][] = ['ok', 'degraded', 'down', 'not_started', 'not_installed', 'installing'];
    for (const health of healths) {
      const policy = setupPolicy('enabled', readyRegion(runtime('unsupported', health)));
      expect(policy.installSupport).toBe('unsupported');
      expect(setupCanAttemptInstall(policy)).toBe(false);
      expect(setupCandidatePath(policy, 'direct')).toBe('direct');
      const running = health === 'ok' || health === 'degraded';
      expect(setupCandidatePath(policy, 'hub')).toBe(running ? 'hub' : 'hub-recovery');
      expect(setupScreenSequence(policy).includes('providers')).toBe(running);
    }
    for (const resolution of ['resolved', 'unresolved'] as const) {
      const policy = setupPolicy('enabled', readyRegion(runtime(resolution)));
      expect(policy.installSupport).toBe('admitted');
      expect(setupCanAttemptInstall(policy)).toBe(true);
      expect(setupCandidatePath(policy, 'direct')).toBe('configure-hub');
      expect(setupScreenSequence(policy)).toEqual(SETUP_SCREENS);
    }
  });

  it('holds downstream progression on pending, failed and stale reads without inventing fallback', () => {
    const stale = readyRegion(runtime('unsupported'));
    for (const read of [loadingRegion<RuntimeDependency>(), unreadRegion<RuntimeDependency>(), beginRegionRead(stale), failRegionRead(stale)]) {
      const policy = setupPolicy('enabled', read);
      expect(setupNavigationReady(policy, 'intro')).toBe(true); // Bootstrap remains reachable.
      expect(setupNavigationReady(policy, 'providers')).toBe(false);
      expect(setupCanAttemptInstall(policy)).toBe(false);
      expect(setupNavigationReady(policy, 'assistants')).toBe(false);
      expect(setupCandidatePath(policy, 'direct')).toBe('pending');
      expect(setupCandidatePath(policy, 'hub')).toBe('hub-recovery');
    }
    for (const read of [loadingRegion<RuntimeDependency>(), unreadRegion<RuntimeDependency>()]) {
      const policy = setupPolicy('enabled', read);
      expect(policy.installSupport).toBe('unknown');
      expect(setupCurrentScreen(policy, 'providers')).toBe('providers');
    }
    expect(setupCandidatePath(policyFor('disabled'), 'hub')).toBe('hub-recovery');
    expect(setupCandidatePath(policyFor('disabled'), undefined)).toBe('pending');
  });
});

// A contract consumer, not the feature shell or a backend-readiness implementation.
// Connection readiness below is synthetic; assertions test policy choice/CTA/navigation.
type Observation = { capability: SetupCapability; runtimeRead: RegionRead<RuntimeDependency> };
type PolicyHarnessControls = {
  settle: (observation: Observation) => void;
  go: (screen: SetupScreenId) => void;
};
function PolicyHarness({ capture, modes, retry, complete }: {
  capture: (controls: PolicyHarnessControls) => void;
  modes: ReadonlyArray<'direct' | 'hub'>;
  retry: () => void;
  complete: () => void;
}) {
  const [environment, setEnvironment] = useState<Observation & { current: SetupScreenId }>({
    capability: 'pending', runtimeRead: loadingRegion(), current: 'intro',
  });
  const [flowState, setFlowState] = useState<SetupFlowState>({
    ...INITIAL_SETUP_FLOW_STATE,
    routeOrder: [{ source_id: 'src_fixture1', model_id: '模型' }], routeOrderDirty: true,
  });
  const policy = setupPolicy(environment.capability, environment.runtimeRead);
  const sequence = setupScreenSequence(policy);
  const go = (requested: SetupScreenId) => setEnvironment((previous) => ({
    ...previous, current: setupCurrentScreen(setupPolicy(previous.capability, previous.runtimeRead), requested),
  }));
  capture({
    go,
    settle: (observation) => setEnvironment((previous) => ({
      ...observation, current: setupCurrentScreen(setupPolicy(observation.capability, observation.runtimeRead), previous.current),
    })),
  });
  const current = environment.current;
  const paths = modes.map((mode) => setupCandidatePath(policy, mode));
  const policyAllowsSyntheticReadyCandidate = paths.some((path) => path === 'direct' || path === 'hub');
  const nextDisabled = !setupNavigationReady(policy, current)
    || (current === 'assistants' && !policyAllowsSyntheticReadyCandidate);
  const common: Omit<SetupScreenProps, 'active'> = {
    policy, flowState, setFlowState, onRetryRuntime: retry,
    handoff: false, onNavigate: go, onActionChange: () => {},
  };
  const Consumer = ({ active, policy: consumedPolicy, flowState: consumedState, onRetryRuntime }: SetupScreenProps) =>
    createElement('section', { hidden: !active, inert: !active },
      createElement('output', { 'aria-label': 'policy' }, JSON.stringify(consumedPolicy)),
      createElement('output', { 'aria-label': 'draft' }, JSON.stringify(consumedState)),
      createElement('output', { 'aria-label': 'candidate paths' }, modes.map((mode) => setupCandidatePath(consumedPolicy, mode)).join(',')),
      createElement('button', { onClick: onRetryRuntime }, 'Retry runtime read'));
  const back = setupBackTarget(sequence, current);
  return createElement('main', null,
    createElement('output', { 'aria-label': 'current screen' }, current),
    ...SETUP_SCREENS.map((id) => createElement(Consumer, { ...common, key: id, active: id === current })),
    createElement('button', { disabled: nextDisabled, onClick: () => {
      if (current === 'assistants') complete();
      else go(sequence[sequence.indexOf(current) + 1]);
    } }, 'Primary'),
    createElement('button', { disabled: !back, onClick: () => { if (back) go(back); } }, 'Back'));
}
const output = (name: string) => screen.getByRole('status', { name }).textContent;
const primary = () => screen.getByRole('button', { name: 'Primary' }) as HTMLButtonElement;

it('consumes late unsupported resolution atomically, preserves drafts and Direct access, and cannot bounce back into providers', () => {
  let controls!: PolicyHarnessControls;
  const modes = Object.freeze(['direct', 'hub'] as const);
  const retry = vi.fn(); const complete = vi.fn();
  render(createElement(PolicyHarness, { capture: (value) => { controls = value; }, modes, retry, complete }));
  expect(primary().disabled).toBe(true);
  act(() => controls.settle({ capability: 'enabled', runtimeRead: loadingRegion() }));
  fireEvent.click(primary());
  expect(output('current screen')).toBe('providers'); // No support read before bootstrap required.
  const draft = output('draft');
  expect(primary().disabled).toBe(true);
  act(() => controls.settle({ capability: 'enabled', runtimeRead: readyRegion(runtime('unsupported')) }));
  expect(output('current screen')).toBe('assistants');
  expect(output('candidate paths')).toBe('direct,hub-recovery');
  expect(output('draft')).toBe(draft);
  expect(primary().disabled).toBe(false);
  fireEvent.click(primary());
  expect(complete).toHaveBeenCalledOnce();
  act(() => controls.go('providers')); // Includes Add source / an obsolete requested destination.
  expect(output('current screen')).toBe('assistants');
  fireEvent.click(screen.getByRole('button', { name: 'Back' }));
  expect(output('current screen')).toBe('intro');
  fireEvent.click(primary());
  expect(output('current screen')).toBe('assistants');
  fireEvent.click(screen.getByRole('button', { name: 'Retry runtime read' }));
  expect(retry).toHaveBeenCalledOnce();
  expect(output('current screen')).toBe('assistants');
  expect(modes).toEqual(['direct', 'hub']);
});

it('consumes supported and failed reads with a usable retry and preserves a running Hub on install-unsupported admission', () => {
  let controls!: PolicyHarnessControls;
  const complete = vi.fn(); const retry = vi.fn();
  render(createElement(PolicyHarness, { capture: (value) => { controls = value; }, modes: ['hub'], retry, complete }));
  act(() => controls.settle({ capability: 'enabled', runtimeRead: loadingRegion() }));
  fireEvent.click(primary());
  act(() => controls.settle({ capability: 'enabled', runtimeRead: unreadRegion() }));
  expect(output('current screen')).toBe('providers');
  expect(primary().disabled).toBe(true);
  expect(output('candidate paths')).toBe('hub-recovery');
  fireEvent.click(screen.getByRole('button', { name: 'Retry runtime read' }));
  expect(retry).toHaveBeenCalledOnce();
  act(() => controls.settle({ capability: 'enabled', runtimeRead: readyRegion(runtime('resolved', 'ok')) }));
  fireEvent.click(primary());
  expect(output('current screen')).toBe('assistants');
  fireEvent.click(primary());
  expect(complete).toHaveBeenCalledOnce();
  act(() => controls.settle({ capability: 'enabled', runtimeRead: readyRegion(runtime('unsupported', 'ok')) }));
  expect(output('candidate paths')).toBe('hub');
  expect(primary().disabled).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: 'Back' }));
  expect(output('current screen')).toBe('providers');
  act(() => controls.settle({ capability: 'enabled', runtimeRead: readyRegion(runtime('unsupported', 'down')) }));
  expect(output('current screen')).toBe('assistants');
  expect(primary().disabled).toBe(true); // No Direct candidate means no readiness shortcut.
  expect(output('candidate paths')).toBe('hub-recovery');
  const stale = readyRegion(runtime('unsupported', 'down'));
  act(() => controls.settle({ capability: 'enabled', runtimeRead: failRegionRead(stale) }));
  expect(output('current screen')).toBe('assistants'); // Stale evidence holds layout only.
  expect(primary().disabled).toBe(true);
});


it('consumes explicit opt-out without a runtime read and redirects a late capability change coherently', () => {
  let controls!: PolicyHarnessControls;
  const complete = vi.fn();
  render(createElement(PolicyHarness, { capture: (value) => { controls = value; }, modes: ['direct'], retry: vi.fn(), complete }));
  act(() => controls.settle({ capability: 'enabled', runtimeRead: loadingRegion() }));
  fireEvent.click(primary());
  expect(output('current screen')).toBe('providers');
  act(() => controls.settle({ capability: 'disabled', runtimeRead: unreadRegion() }));
  expect(output('current screen')).toBe('assistants');
  expect(output('candidate paths')).toBe('direct');
  expect(primary().disabled).toBe(false);
  fireEvent.click(primary());
  expect(complete).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: 'Back' }));
  expect(output('current screen')).toBe('intro');
});
