/* @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PET_SUMMON_EVENT } from './petBridge';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const api = {
  getConfig: vi.fn(async () => ({ mode: 'self_host', setup_state: { needs_setup: true } })),
  getMainSession: vi.fn(async () => ({ status: 200, session: null })),
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
  it('shows setup in the fixed window without a resize and rechecks on summon', async () => {
    const invoke = vi.fn((command: string) => Promise.resolve(command === 'pet_ready'
      ? { binding: null, revision: 0, summon_pending: null, dismissed_ids: [] } : null));
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    render(<PetPage />);
    expect(await screen.findByText('pet.setupPending')).toBeTruthy();
    expect(invoke.mock.calls.some(([command]) => command === 'pet_set_expanded')).toBe(false);
    const before = api.getConfig.mock.calls.length;
    act(() => window.dispatchEvent(new CustomEvent(PET_SUMMON_EVENT, { detail: { intent: 'show' } })));
    await waitFor(() => expect(api.getConfig.mock.calls.length).toBeGreaterThan(before));
  });
});
