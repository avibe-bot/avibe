/* @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PET_SUMMON_EVENT } from './petBridge';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const api = {
  getConfig: vi.fn(async () => ({ mode: 'self_host', setup_state: { needs_setup: true } })),
  getSessionResult: vi.fn(async () => ({ status: 200, session: null })),
  listSessionMessages: vi.fn(async () => ({ messages: [], next_after_id: null, next_before_id: null })),
  getTurnState: vi.fn(async () => ({
    in_flight: false, foreground: 'idle', native_turn_started: false,
    pending_input_count: 0, background_activities: [], pending_activity_output_count: 0,
    connection: 'connected',
  })),
  listSessions: vi.fn(async () => ({ sessions: [], next_before_id: null })),
  getVaultRequests: vi.fn(async () => ({ requests: [] })),
  sendSessionMessage: vi.fn(),
  connectWorkbenchEvents: () => () => undefined,
};

vi.mock('@/context/ApiContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/context/ApiContext')>()),
  useApi: () => api,
}));
vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_chat: true } }),
}));
vi.mock('@/context/WorkbenchInboxContext', () => ({
  useWorkbenchInbox: () => ({ unreadBySession: {}, markRead: vi.fn() }),
}));

Object.defineProperty(document, 'hasFocus', { value: () => true, configurable: true });

let PetPage: typeof import('./PetPage').PetPage;

beforeEach(async () => {
  vi.resetModules();
  ({ PetPage } = await import('./PetPage'));
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  delete (window as { __AVIBE_DESKTOP_SHELL__?: unknown }).__AVIBE_DESKTOP_SHELL__;
  delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
});

describe('setup-pending expansion', () => {
  it('grows the native frame before showing the setup-pending card', async () => {
    let resolveGrow: (value: unknown) => void = () => undefined;
    const grow = new Promise((resolve) => { resolveGrow = resolve; });
    const invoke = vi.fn((command: string) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: null, revision: 0, summon_pending: null });
      if (command === 'pet_set_expanded') {
        if (invoke.mock.calls.filter(([c]) => c === 'pet_set_expanded').length > 8) {
          throw new Error('pet_set_expanded loop');
        }
        return grow;
      }
      return Promise.resolve(null);
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    render(<PetPage />);
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_set_expanded', { expanded: true }));
    expect(screen.queryByText('pet.setupPending')).toBeNull();
    await act(async () => resolveGrow({ panel_side: 'left', panel_edge: 'bottom' }));
    expect(await screen.findByText('pet.setupPending')).toBeTruthy();
    const expands = invoke.mock.calls.filter(([command]) => command === 'pet_set_expanded').length;
    act(() => {
      window.dispatchEvent(new CustomEvent(PET_SUMMON_EVENT, { detail: { intent: 'show' } }));
    });
    await act(async () => new Promise((resolve) => setTimeout(resolve, 40)));
    expect(invoke.mock.calls.filter(([command]) => command === 'pet_set_expanded')).toHaveLength(expands + 1);
  });
});
