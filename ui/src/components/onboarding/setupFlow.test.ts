// @vitest-environment jsdom
// Contract consumers live only in this test; PR0 introduces no feature shell.
import { createElement, useState } from 'react';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { beginRegionRead, failRegionRead, loadingRegion, readyRegion, unreadRegion } from '../settings/models/regionRead';
import { CONTRACT_VERSION, type RuntimeDependency } from '../settings/models/types';

import {
  INITIAL_SETUP_FLOW_STATE,
  SETUP_SCREENS,
  setupBackTarget,
  setupCanAttemptInstall,
  setupCandidateAllowed,
  setupHubRunning,
  setupCapability,
  setupNavigationReady,
  type SetupCapability,
  type SetupFlowState,
  type SetupScreenId,
  type SetupScreenProps,
} from './setupFlow';

afterEach(cleanup);

const CAPABILITIES: readonly SetupCapability[] = ['pending', 'enabled', 'disabled'];

describe('one Hub setup journey', () => {
  it('keeps every step and requires enabled capability to progress', () => {
    expect(SETUP_SCREENS).toEqual(['intro', 'providers', 'assistants']);
    for (const capability of CAPABILITIES) {
      expect(setupNavigationReady(capability, true)).toBe(capability === 'enabled');
    }
  });

  it('distinguishes unread capability from an authoritative disabled configuration', () => {
    expect(setupCapability(null)).toBe('pending');
    expect(setupCapability(true)).toBe('enabled');
    expect(setupCapability(false)).toBe('disabled');
  });

  it('keeps Back on the same three-screen path', () => {
    expect(setupBackTarget(SETUP_SCREENS, 'assistants')).toBe('providers');
    expect(setupBackTarget(SETUP_SCREENS, 'providers')).toBe('intro');
    expect(setupBackTarget(SETUP_SCREENS, 'intro')).toBeNull();
  });
});

describe('shell-owned flow state', () => {
  it('starts with nothing selected, imported or added', () => {
    expect(INITIAL_SETUP_FLOW_STATE).toEqual({
      providerSelection: { scan: null, selectedBackends: [] },
      importedCount: 0,
      addedThroughMore: [],
    });
  });

  it('carries screen updates through navigation and composes a late functional update with newer state', async () => {
    let finishImport: (() => void) | undefined;
    const pendingImport = new Promise<void>((resolve) => { finishImport = resolve; });
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
    const AssistantScreen = ({ active, flowState, onNavigate }: SetupScreenProps) =>
      createElement('section', { hidden: !active, inert: !active, 'data-testid': 'assistants' },
        createElement('output', null, JSON.stringify(flowState)),
        createElement('button', { onClick: () => {
          onNavigate('providers');
        } }, 'Back to providers'));
    const Shell = () => {
      const [current, setCurrent] = useState<SetupScreenId>('providers');
      const [flowState, setFlowState] = useState(INITIAL_SETUP_FLOW_STATE);
      const common: Omit<SetupScreenProps, 'active'> = {
        capability: 'enabled', gatewayEnabled: true, runtimeRead: loadingRegion(), onRetrySetup: () => {},
        flowState, setFlowState, handoff: false, onNavigate: setCurrent, onActionChange: () => {},
      };
      return createElement('main', null,
        createElement(ProviderScreen, { ...common, active: current === 'providers' }),
        createElement(AssistantScreen, { ...common, active: current === 'assistants' }));
    };
    render(createElement(Shell));
    const read = (id: string): SetupFlowState => JSON.parse(within(screen.getByTestId(id)).getByRole('status').textContent!);

    fireEvent.click(screen.getByRole('button', { name: 'Start fixture import' }));
    expect(read('assistants').providerSelection).toEqual(selection);
    fireEvent.click(screen.getByRole('button', { name: 'Back to providers' }));
    expect(read('providers')).toMatchObject({ providerSelection: selection });
    fireEvent.click(screen.getByRole('button', { name: 'Add fixture source' }));
    await act(async () => { finishImport!(); await pendingImport; });
    expect(read('assistants')).toEqual({
      providerSelection: { scan: null, selectedBackends: [] }, importedCount: 2,
      addedThroughMore: ['新增来源'],
    });
    expect((screen.getByTestId('providers') as HTMLElement).hidden).toBe(true);
    expect(screen.getByTestId('providers').hasAttribute('inert')).toBe(true);
  });
});

const runtime = (
  resolution: RuntimeDependency['manifest']['resolution'],
  health: RuntimeDependency['status']['health'] = 'not_installed',
): RuntimeDependency => ({
  contract_version: CONTRACT_VERSION, enabled: true,
  manifest: resolution === 'unresolved'
    ? { name: 'cliproxyapi', resolution, assets: [] }
    : { name: 'cliproxyapi', resolution, version: 'fixture', source_sha: 'fixture', assets: [] },
  status: { verified: false, health },
});

it('keeps Hub readiness independent of install admission and requires the deployment-selected mode', () => {
  const healths: RuntimeDependency['status']['health'][] = ['ok', 'degraded', 'down', 'not_started', 'not_installed', 'installing'];
  for (const health of healths) {
    const read = readyRegion(runtime('unsupported', health));
    expect(setupCanAttemptInstall('enabled', true, read)).toBe(false);
    expect(setupCandidateAllowed('enabled', true, read, 'direct')).toBe(false);
    expect(setupCandidateAllowed('enabled', true, read, 'hub')).toBe(health === 'ok' || health === 'degraded');
  }
  for (const resolution of ['resolved', 'unresolved'] as const) {
    expect(setupCanAttemptInstall('enabled', true, readyRegion(runtime(resolution)))).toBe(true);
  }
  for (const capability of ['pending', 'disabled'] as const) {
    expect(setupCanAttemptInstall(capability, true, readyRegion(runtime('resolved')))).toBe(false);
    expect(setupCandidateAllowed(capability, true, readyRegion(runtime('resolved', 'ok')), 'hub')).toBe(false);
  }
  expect(setupCandidateAllowed('disabled', true, unreadRegion(), 'direct')).toBe(false);
  expect(setupCandidateAllowed('disabled', true, unreadRegion(), undefined)).toBe(false);
  const stoppedIntent = readyRegion({ ...runtime('resolved', 'ok'), enabled: false });
  expect(setupCanAttemptInstall('enabled', true, stoppedIntent)).toBe(false);
  expect(setupCandidateAllowed('enabled', true, stoppedIntent, 'hub')).toBe(false);
  expect(setupNavigationReady('enabled', null)).toBe(false);
  expect(setupNavigationReady('enabled', false)).toBe(false);
});

// Test-only consumers of C2 and its actual gates; synthetic route/auth/application
// readiness below does not prove backend readiness or a shipped feature coordinator.
type Observation = Pick<SetupScreenProps, 'capability' | 'gatewayEnabled' | 'runtimeRead'>;
function RuntimeConsumer({ active, capability, runtimeRead, flowState, setFlowState, onRetrySetup }: SetupScreenProps) {
  return createElement('section', { hidden: !active, inert: !active },
    createElement('output', { 'aria-label': 'draft' }, JSON.stringify(flowState)),
    createElement('output', { 'aria-label': 'capability' }, capability),
    createElement('output', { 'aria-label': 'runtime read' }, runtimeRead.kind),
    createElement('button', { onClick: () => setFlowState((previous) => ({
      ...previous, addedThroughMore: ['src_fixture1'],
    })) }, 'Add source'),
    createElement('button', { onClick: onRetrySetup }, 'Recheck runtime'));
}
function RuntimeHarness({ capture, retry, install, complete, mode }: {
  capture: (settle: (observation: Observation) => void) => void;
  // Synthetic result of the shell's config-first retry owner; no real HTTP here.
  retry: () => Promise<Observation>;
  install: () => void;
  complete: () => void;
  mode: 'direct' | 'hub';
}) {
  const [observation, setObservation] = useState<Observation>({ capability: 'pending', gatewayEnabled: null, runtimeRead: loadingRegion() });
  const [current, setCurrent] = useState<SetupScreenId>('intro');
  const [flowState, setFlowState] = useState(INITIAL_SETUP_FLOW_STATE);
  capture(setObservation);
  const { capability, gatewayEnabled, runtimeRead } = observation;
  const sequence = SETUP_SCREENS;
  const back = setupBackTarget(sequence, current);
  const mayContinue = setupNavigationReady(capability, gatewayEnabled) && (current === 'intro'
    || (current === 'providers' ? setupHubRunning(runtimeRead) : setupCandidateAllowed(capability, gatewayEnabled, runtimeRead, mode)));
  const common: Omit<SetupScreenProps, 'active'> = {
    ...observation, flowState, setFlowState, handoff: false, onActionChange: () => {}, onNavigate: setCurrent,
    onRetrySetup: () => {
      setObservation((previous) => ({ ...previous, runtimeRead: beginRegionRead(previous.runtimeRead) }));
      void retry().then(
        (result) => setObservation(result),
        () => setObservation((previous) => ({ ...previous, runtimeRead: failRegionRead(previous.runtimeRead) })),
      );
    },
  };
  return createElement('main', null,
    createElement('output', { 'aria-label': 'current screen' }, current),
    ...SETUP_SCREENS.map((id) => createElement(RuntimeConsumer, { ...common, key: id, active: id === current })),
    createElement('button', { disabled: !mayContinue, onClick: () => {
      if (current === 'assistants') complete();
      else setCurrent(sequence[sequence.indexOf(current) + 1]);
    } }, 'Primary'),
    createElement('button', { disabled: !back, onClick: () => { if (back) setCurrent(back); } }, 'Back'),
    createElement('button', { disabled: !setupCanAttemptInstall(capability, gatewayEnabled, runtimeRead), onClick: install }, 'Prepare runtime'));
}
const output = (name: string) => screen.getByRole('status', { name }).textContent;
const primary = () => screen.getByRole('button', { name: 'Primary' }) as HTMLButtonElement;

it('consumes unsupported admission without leaving providers, losing drafts or completing through Direct', async () => {
  let settle!: (observation: Observation) => void;
  const install = vi.fn(); const complete = vi.fn();
  const retry = vi.fn().mockResolvedValue({ capability: 'enabled', gatewayEnabled: true, runtimeRead: readyRegion(runtime('resolved', 'ok')) });
  const props = { capture: (value: typeof settle) => { settle = value; }, retry, install, complete };
  const view = render(createElement(RuntimeHarness, { ...props, mode: 'direct' }));
  expect(primary().disabled).toBe(true);
  act(() => settle({ capability: 'enabled', gatewayEnabled: true, runtimeRead: loadingRegion() }));
  fireEvent.click(primary()); // Support is first learned after provider bootstrap.
  expect(output('current screen')).toBe('providers');
  fireEvent.click(screen.getByRole('button', { name: 'Add source' }));
  const draft = output('draft');
  act(() => settle({ capability: 'enabled', gatewayEnabled: true, runtimeRead: readyRegion(runtime('unsupported')) }));
  expect(output('current screen')).toBe('providers');
  expect(primary().disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: 'Prepare runtime' }));
  expect(install).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Back' }));
  expect(output('current screen')).toBe('intro');
  fireEvent.click(primary());
  expect(output('current screen')).toBe('providers');
  expect(output('draft')).toBe(draft);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Recheck runtime' })); });
  expect(retry).toHaveBeenCalledOnce();
  expect(output('runtime read')).toBe('ready');
  expect(output('current screen')).toBe('providers');
  fireEvent.click(primary());
  expect(output('current screen')).toBe('assistants');
  expect(primary().disabled).toBe(true); // A healthy runtime alone never changes Direct custody.
  fireEvent.click(primary());
  expect(complete).not.toHaveBeenCalled();
  view.rerender(createElement(RuntimeHarness, { ...props, mode: 'hub' })); // Fresh persisted-mode evidence.
  expect(primary().disabled).toBe(false);
  expect(output('draft')).toBe(draft);
  fireEvent.click(primary());
  expect(complete).toHaveBeenCalledOnce();
});

it('consumes read failures as held errors, retries in place and preserves a healthy install-unsupported Hub', async () => {
  let settle!: (observation: Observation) => void;
  const retry = vi.fn().mockRejectedValueOnce(new Error('fixture 503'))
    .mockResolvedValue({ capability: 'enabled', gatewayEnabled: true, runtimeRead: readyRegion(runtime('unsupported', 'ok')) });
  const complete = vi.fn(); const install = vi.fn();
  render(createElement(RuntimeHarness, { capture: (value) => { settle = value; }, retry, complete, install, mode: 'hub' }));
  act(() => settle({ capability: 'enabled', gatewayEnabled: true, runtimeRead: loadingRegion() }));
  fireEvent.click(primary());
  for (const read of [unreadRegion<RuntimeDependency>(), beginRegionRead(readyRegion(runtime('resolved', 'ok'))), failRegionRead(readyRegion(runtime('unsupported', 'ok')))]) {
    act(() => settle({ capability: 'enabled', gatewayEnabled: true, runtimeRead: read }));
    expect(output('current screen')).toBe('providers');
    expect(output('capability')).toBe('enabled');
    expect(primary().disabled).toBe(true);
    expect(setupCanAttemptInstall('enabled', true, read)).toBe(false);
    expect(setupCandidateAllowed('enabled', true, read, 'direct')).toBe(false);
    expect(setupCandidateAllowed('enabled', true, read, 'hub')).toBe(false);
  }
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Recheck runtime' })); });
  expect(output('capability')).toBe('enabled');
  expect(primary().disabled).toBe(true);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Recheck runtime' })); });
  expect(output('runtime read')).toBe('ready');
  fireEvent.click(screen.getByRole('button', { name: 'Prepare runtime' }));
  expect(install).not.toHaveBeenCalled();
  fireEvent.click(primary());
  expect(output('current screen')).toBe('assistants');
  fireEvent.click(primary());
  expect(complete).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: 'Back' }));
  expect(output('current screen')).toBe('providers');
});


it.each(['deployment', 'saved-intent'] as const)('holds disabled %s without skipping providers or treating Direct as setup-ready', async (disabled) => {
  const config = { capability: disabled === 'deployment' ? 'disabled' as const : 'enabled' as const, gatewayEnabled: disabled !== 'saved-intent' };
  let settle!: (observation: Observation) => void;
  const complete = vi.fn(); const install = vi.fn();
  const retry = vi.fn().mockResolvedValue({ ...config, runtimeRead: loadingRegion() });
  render(createElement(RuntimeHarness, { capture: (value) => { settle = value; }, retry, complete, install, mode: 'direct' }));
  act(() => settle({ ...config, runtimeRead: readyRegion(runtime('resolved', 'ok')) }));
  expect(output('current screen')).toBe('intro');
  expect(primary().disabled).toBe(true);
  fireEvent.click(primary());
  fireEvent.click(screen.getByRole('button', { name: 'Prepare runtime' }));
  expect(complete).not.toHaveBeenCalled();
  expect(install).not.toHaveBeenCalled();
  expect(output('capability')).toBe(config.capability);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Recheck runtime' })); });
  expect(retry).toHaveBeenCalledOnce();
  expect(output('capability')).toBe(config.capability);
  expect(primary().disabled).toBe(true);
  // A disabled result arriving later holds the current screen, never redirects to Direct.
  act(() => settle({ capability: 'enabled', gatewayEnabled: true, runtimeRead: loadingRegion() }));
  fireEvent.click(primary());
  fireEvent.click(screen.getByRole('button', { name: 'Add source' }));
  const draft = output('draft');
  act(() => settle({ ...config, runtimeRead: readyRegion(runtime('resolved', 'ok')) }));
  expect(output('current screen')).toBe('providers');
  expect(primary().disabled).toBe(true);
  expect(output('draft')).toBe(draft);
  fireEvent.click(screen.getByRole('button', { name: 'Back' }));
  expect(output('current screen')).toBe('intro');
  expect(output('capability')).toBe(config.capability);
});
