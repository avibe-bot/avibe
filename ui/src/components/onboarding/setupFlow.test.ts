// @vitest-environment jsdom
// Contract consumers live only in this test; PR0 introduces no feature shell.
import { createElement, useState } from 'react';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import {
  INITIAL_SETUP_FLOW_STATE,
  SETUP_SCREENS,
  setupBackTarget,
  setupCapability,
  setupNavigationReady,
  setupScreenSequence,
  type SetupCapability,
  type SetupFlowState,
  type SetupScreenId,
  type SetupScreenProps,
} from './setupFlow';

afterEach(cleanup);

const CAPABILITIES: readonly SetupCapability[] = ['pending', 'enabled', 'disabled'];

describe('setup screen sequence', () => {
  it('keeps the flow a prefix-stable ordering of the declared screens', () => {
    // A property rather than three copied lists: whatever the capability says, the flow
    // opens on the introduction, closes on the assistants, and never reorders what is
    // between them — so a screen added later is covered without editing this test.
    for (const capability of CAPABILITIES) {
      const sequence = setupScreenSequence(capability);
      expect(sequence[0]).toBe('intro');
      expect(sequence[sequence.length - 1]).toBe('assistants');
      const declared = SETUP_SCREENS.filter((screen) => sequence.includes(screen));
      expect([...sequence]).toEqual([...declared]);
    }
  });

  it('drops only the providers screen when the capability is explicitly off', () => {
    expect(setupScreenSequence('disabled')).toEqual(['intro', 'assistants']);
    expect(setupScreenSequence('enabled')).toEqual([...SETUP_SCREENS]);
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
    expect(setupNavigationReady('pending')).toBe(false);
    for (const capability of CAPABILITIES.filter((value) => value !== 'pending')) {
      expect(setupNavigationReady(capability)).toBe(true);
    }
  });
});

describe('setup back target', () => {
  it('leaves to the previous screen of the sequence actually running', () => {
    expect(setupBackTarget(setupScreenSequence('enabled'), 'assistants')).toBe('providers');
    expect(setupBackTarget(setupScreenSequence('enabled'), 'providers')).toBe('intro');
    // The degraded flow has no providers screen, so Back from the assistants screen is the
    // introduction rather than a screen this instance never renders.
    expect(setupBackTarget(setupScreenSequence('disabled'), 'assistants')).toBe('intro');
  });

  it('has nowhere to go from the first screen', () => {
    expect(setupBackTarget(setupScreenSequence('enabled'), 'intro')).toBeNull();
    expect(setupBackTarget(setupScreenSequence('disabled'), 'intro')).toBeNull();
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
