// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ChannelList } from './ChannelList';
import { UserList } from './UserList';

// Each scope save carries expected_agent_name, the Agent binding the form last read.
// The server refuses a binding change whose token is outdated, so after every save
// attempt the page must hold the stored row, keeping routing and token read together.

const api = vi.hoisted(() => ({
  getConfig: vi.fn(), getSettings: vi.fn(), saveSettings: vi.fn(), slackChannels: vi.fn(),
  getUsers: vi.fn(), saveUsers: vi.fn(), getBindCodes: vi.fn(),
}));
vi.mock('@/context/ApiContext', async (loadOriginal) => ({
  ...await loadOriginal<typeof import('@/context/ApiContext')>(), useApi: () => api,
}));
vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_manage_access_members: true } }),
}));
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../shared/RoutingConfigPanel', () => ({
  RoutingConfigPanel: ({ value, onChange }: { value: { routing: object }; onChange: (patch: object) => void }) => (
    <>
      {['vibey', 'reviewer'].map((agent) => (
        <button key={agent} type="button" onClick={() => onChange({ routing: { ...value.routing, agent_name: agent } })}>
          {`route:${agent}`}
        </button>
      ))}
    </>
  ),
}));

const agentCatalog = { default_agent_name: 'vibey', agents: [{ name: 'vibey', backend: 'vibey' }, { name: 'reviewer', backend: 'vibey' }] };
const scope = (agent: string | null) => ({
  display_name: 'Liu', is_admin: true, bound_at: '', enabled: true, show_message_types: ['assistant'], custom_cwd: null,
  require_mention: null, require_bind: null, routing: { agent_name: agent }, expected_agent_name: agent,
});
/** The server stores the posted rows and reports each row's binding as its new token. */
const stored = (rows: Record<string, { routing: { agent_name: string | null } }>) => Object.fromEntries(
  Object.entries(rows).map(([id, row]) => [id, { ...row, expected_agent_name: row.routing.agent_name }]),
);

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  api.getBindCodes.mockResolvedValue({ ok: true, bind_codes: [] });
});

const savedToken = (call: unknown[], kind: 'users' | 'channels', id: string) =>
  (call[0] as Record<string, Record<string, { expected_agent_name: string | null }>>)[kind][id].expected_agent_name;

describe('users page', () => {
  beforeEach(() => {
    api.getConfig.mockResolvedValue({ platform: 'lark', platforms: { enabled: ['lark'] } });
    api.getSettings.mockResolvedValue({ agent_catalog: agentCatalog });
    serveUsers(null);
  });

  /** Lark is the only enabled platform; the page reads others before its config loads. */
  function serveUsers(agent: string | null) {
    api.getUsers.mockImplementation(async (platform) => ({ ok: true, users: platform === 'lark' ? { ou_1: scope(agent) } : {} }));
  }
  const larkReads = () => api.getUsers.mock.calls.filter(([platform]) => platform === 'lark').length;

  async function openUser() {
    render(<UserList />);
    await waitFor(() => expect(larkReads()).toBe(1));
    await userEvent.click(await screen.findByText('Liu'));
  }

  it('sends the stored token after its own save changed the binding', async () => {
    api.saveUsers.mockImplementation(async (payload) => ({ ok: true, users: stored(payload.users) }));
    await openUser();
    await userEvent.click(screen.getByText('route:vibey'));
    await waitFor(() => expect(api.saveUsers).toHaveBeenCalledTimes(1));
    await userEvent.click(screen.getByText('route:reviewer'));
    await waitFor(() => expect(api.saveUsers).toHaveBeenCalledTimes(2));
    expect(savedToken(api.saveUsers.mock.calls[0], 'users', 'ou_1')).toBeNull();
    expect(savedToken(api.saveUsers.mock.calls[1], 'users', 'ou_1')).toBe('vibey');
  });

  it('reloads the stored rows after a refused save', async () => {
    api.saveUsers.mockRejectedValueOnce(new Error('settings_conflict'))
      .mockImplementation(async (payload) => ({ ok: true, users: stored(payload.users) }));
    await openUser();
    serveUsers('vibey');
    await userEvent.click(screen.getByText('route:reviewer'));
    await waitFor(() => expect(larkReads()).toBe(2));
    await userEvent.click(screen.getByText('route:reviewer'));
    await waitFor(() => expect(api.saveUsers).toHaveBeenCalledTimes(2));
    expect(savedToken(api.saveUsers.mock.calls[1], 'users', 'ou_1')).toBe('vibey');
  });
});

describe('channels page', () => {
  beforeEach(() => {
    api.getConfig.mockResolvedValue({ platform: 'slack', platforms: { enabled: ['slack'] }, slack: { bot_token: 'xoxb-test' } });
    api.getSettings.mockResolvedValue({ agent_catalog: agentCatalog, channels: { C1: { ...scope(null), enabled: true } } });
    api.slackChannels.mockResolvedValue({ ok: true, channels: [{ id: 'C1', name: 'general' }] });
  });

  async function openChannel() {
    render(<MemoryRouter><ChannelList isPage /></MemoryRouter>);
    await userEvent.click(await screen.findByText('general'));
  }

  it('sends the stored token after its own save changed the binding', async () => {
    api.saveSettings.mockImplementation(async (payload) => ({ channels: stored(payload.channels) }));
    await openChannel();
    await userEvent.click(screen.getByText('route:vibey'));
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledTimes(1));
    await userEvent.click(screen.getByText('route:reviewer'));
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledTimes(2));
    expect(savedToken(api.saveSettings.mock.calls[0], 'channels', 'C1')).toBeNull();
    expect(savedToken(api.saveSettings.mock.calls[1], 'channels', 'C1')).toBe('vibey');
  });

  it('reloads the stored rows after a refused save', async () => {
    api.saveSettings.mockRejectedValueOnce(new Error('settings_conflict'))
      .mockImplementation(async (payload) => ({ channels: stored(payload.channels) }));
    await openChannel();
    const reads = api.getSettings.mock.calls.length;
    api.getSettings.mockResolvedValue({ agent_catalog: agentCatalog, channels: { C1: { ...scope('vibey'), enabled: true } } });
    await userEvent.click(screen.getByText('route:reviewer'));
    await waitFor(() => expect(api.getSettings.mock.calls.length).toBeGreaterThan(reads));
    await userEvent.click(screen.getByText('route:reviewer'));
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledTimes(2));
    expect(savedToken(api.saveSettings.mock.calls[1], 'channels', 'C1')).toBe('vibey');
  });
});
