// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useEffect, useRef } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchEventHandlers } from './ApiContext';
import { BotsProvider } from './BotsProvider';
import { useBots } from './BotsContext';
import { InstanceAuthorizationContext } from './InstanceAuthorizationContext';
import { ToastContext } from './ToastContext';
import { BotProfileDialogHost } from '../components/workbench/bots/BotProfileDialog';
import type { Bot } from '../lib/bots';
import { DENIED_INSTANCE_CAPABILITIES } from '../lib/sessionInfo';
import type { WorkbenchEventConnectionState } from '../lib/workbenchEventConnection';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

const apiRef = { current: null as unknown };
vi.mock('./ApiContext', async () => {
  const actual = await vi.importActual<typeof import('./ApiContext')>('./ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});

const MAIN: Bot = {
  handle: 'main', display_name: '', avatar: { kind: 'mascot' }, role: 'First point of contact',
  agent_name: 'main', session_id: 'ses_main', position: 0, builtin: true,
};
const CSO: Bot = {
  handle: 'cso', display_name: 'Security Lead', avatar: null, role: 'Audits',
  agent_name: 'cso', session_id: 'ses_cso', position: 1, builtin: false,
};

function fakeApi() {
  const stream: { handlers: WorkbenchEventHandlers | null } = { handlers: null };
  const api = {
    listBots: vi.fn(async () => [MAIN, CSO]),
    getSession: vi.fn(async () => ({ agent_status: 'idle' })),
    listVibeAgents: vi.fn(async () => ({ ok: true, agents: [], default_agent_name: null })),
    updateBot: vi.fn(async (_handle: string, patch: { display_name?: string }) => ({ ...CSO, ...patch })),
    connectWorkbenchEvents: vi.fn((handlers: WorkbenchEventHandlers) => {
      stream.handlers = handlers;
      return () => {
        stream.handlers = null;
      };
    }),
  };
  return { api, stream };
}

const OpenEdit = () => {
  const { loaded, openProfile } = useBots();
  const opened = useRef(false);
  useEffect(() => {
    if (!loaded || opened.current) return;
    opened.current = true;
    openProfile({ kind: 'edit', handle: 'cso' });
  }, [loaded, openProfile]);
  return null;
};

async function flush() {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve();
  });
}

afterEach(() => {
  cleanup();
  apiRef.current = null;
});

// A Bot write from this page reaches the list through the server's `bots.changed`
// once it commits. With the stream live that event is the one refetch, so a save
// from the dialog reads `GET /api/bots` once, not once for the save and again for
// the event. Without a live stream nothing else would read it, so the write does.
describe('saving a Bot from the dialog reads the Bots once', () => {
  it.each<[WorkbenchEventConnectionState, number]>([
    ['connected', 1],
    ['reconnecting', 1],
  ])('with the event stream %s', async (state, expectedReads) => {
    const { api, stream } = fakeApi();
    apiRef.current = api;
    render(
      <MemoryRouter>
        <ToastContext.Provider value={{ showToast: () => undefined }}>
          <InstanceAuthorizationContext.Provider
            value={{
              remote: false,
              instanceKind: null,
              instanceRole: null,
              capabilities: { ...DENIED_INSTANCE_CAPABILITIES, is_instance_owner: true },
            }}
          >
            <BotsProvider>
              <OpenEdit />
              <BotProfileDialogHost />
            </BotsProvider>
          </InstanceAuthorizationContext.Provider>
        </ToastContext.Provider>
      </MemoryRouter>,
    );
    await flush();
    act(() => stream.handlers?.onConnectionState?.(state));
    const name = await screen.findByDisplayValue('Security Lead');
    const readsBefore = api.listBots.mock.calls.length;

    fireEvent.change(name, { target: { value: 'Security Chief' } });
    fireEvent.click(screen.getByRole('button', { name: 'bots.dialog.save' }));
    await flush();
    expect(api.updateBot).toHaveBeenCalledWith('cso', { display_name: 'Security Chief' });
    // The server publishes the write; a live stream delivers it.
    if (state === 'connected') {
      act(() => stream.handlers?.onBotsChanged?.({ handle: 'cso' }));
      await flush();
    }

    expect(api.listBots.mock.calls.length - readsBefore).toBe(expectedReads);
  });
});
