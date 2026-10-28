// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ChannelList } from './ChannelList';
import { UserList } from './UserList';

// Each settings edit sends only what it changed on one scope, with the Agent the
// edited view was based on. Sending the page's whole copy back reverted other
// writers' changes and deleted channels enabled since the page loaded.

const api = vi.hoisted(() => ({
  getConfig: vi.fn(), getSettings: vi.fn(), slackChannels: vi.fn(), telegramChats: vi.fn(),
  getUsers: vi.fn(), getBindCodes: vi.fn(), patchScopeSettings: vi.fn(),
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
const row = (agent: string | null) => ({
  display_name: 'Liu', is_admin: true, bound_at: '', enabled: true, show_message_types: ['assistant'], custom_cwd: null,
  require_mention: null, require_bind: null, routing: { agent_name: agent, model: null }, expected_agent_name: agent,
});
type Patch = { changes: { routing?: { agent_name?: string | null } }; expected_agent_name?: string | null };
/** The server applies the routing change to the scope's row and reports the stored row. */
const storedAfter = (request: Patch, before = row(null)) => {
  const agent = request.changes.routing?.agent_name ?? before.routing.agent_name;
  return { ok: true, settings: { ...before, ...row(agent) } };
};
const sent = (index: number) => api.patchScopeSettings.mock.calls[index][0];

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  api.getBindCodes.mockResolvedValue({ ok: true, bind_codes: [] });
});

describe('users page', () => {
  beforeEach(() => {
    api.getConfig.mockResolvedValue({ platform: 'lark', platforms: { enabled: ['lark'] } });
    api.getSettings.mockResolvedValue({ agent_catalog: agentCatalog });
    serveUsers(null);
  });

  /** Lark is the only enabled platform; the page reads others before its config loads. */
  function serveUsers(agent: string | null) {
    api.getUsers.mockImplementation(async (platform) => ({ ok: true, users: platform === 'lark' ? { ou_1: row(agent) } : {} }));
  }
  const larkReads = () => api.getUsers.mock.calls.filter(([platform]) => platform === 'lark').length;

  async function openUser() {
    render(<UserList />);
    await waitFor(() => expect(larkReads()).toBe(1));
    await userEvent.click(await screen.findByText('Liu'));
  }

  it('sends only the changed routing, based on the Agent its own last save stored', async () => {
    api.patchScopeSettings.mockImplementation(async (request) => storedAfter(request));
    await openUser();
    await userEvent.click(screen.getByText('route:vibey'));
    await waitFor(() => expect(api.patchScopeSettings).toHaveBeenCalledTimes(1));
    await userEvent.click(screen.getByText('route:reviewer'));
    await waitFor(() => expect(api.patchScopeSettings).toHaveBeenCalledTimes(2));

    expect(sent(0)).toEqual({
      platform: 'lark', scope: 'user', id: 'ou_1',
      changes: { routing: { agent_name: 'vibey' } }, expected_agent_name: null,
    });
    expect(sent(1)).toMatchObject({ changes: { routing: { agent_name: 'reviewer' } }, expected_agent_name: 'vibey' });
  });

  it('reloads the stored rows after a refused save', async () => {
    api.patchScopeSettings.mockRejectedValueOnce(new Error('settings_conflict'))
      .mockImplementation(async (request) => storedAfter(request, row('vibey')));
    await openUser();
    serveUsers('vibey');
    await userEvent.click(screen.getByText('route:reviewer'));
    await waitFor(() => expect(larkReads()).toBe(2));
    await userEvent.click(screen.getByText('route:reviewer'));
    await waitFor(() => expect(api.patchScopeSettings).toHaveBeenCalledTimes(2));
    expect(sent(1)).toMatchObject({ changes: { routing: { agent_name: 'reviewer' } }, expected_agent_name: 'vibey' });
  });
});

describe('channels page', () => {
  beforeEach(() => {
    api.getConfig.mockResolvedValue({ platform: 'slack', platforms: { enabled: ['slack'] }, slack: { bot_token: 'xoxb-test' } });
    api.getSettings.mockResolvedValue({
      agent_catalog: agentCatalog,
      channels: { C1: { ...row(null), enabled: true }, C2: { ...row(null), enabled: true, custom_cwd: '/c2' } },
    });
    api.slackChannels.mockResolvedValue({ ok: true, channels: [{ id: 'C1', name: 'general' }, { id: 'C2', name: 'random' }] });
  });

  async function openChannel() {
    render(<MemoryRouter><ChannelList isPage /></MemoryRouter>);
    await userEvent.click(await screen.findByText('general'));
  }

  it('sends only the edited channel and fields, based on the Agent its own last save stored', async () => {
    api.patchScopeSettings.mockImplementation(async (request) => storedAfter(request, { ...row(null), enabled: true }));
    await openChannel();
    await userEvent.click(screen.getByText('route:vibey'));
    await waitFor(() => expect(api.patchScopeSettings).toHaveBeenCalledTimes(1));
    await userEvent.click(screen.getByText('route:reviewer'));
    await waitFor(() => expect(api.patchScopeSettings).toHaveBeenCalledTimes(2));

    expect(sent(0)).toEqual({
      platform: 'slack', scope: 'channel', id: 'C1',
      changes: { routing: { agent_name: 'vibey' } }, expected_agent_name: null,
    });
    expect(sent(1)).toMatchObject({ id: 'C1', changes: { routing: { agent_name: 'reviewer' } }, expected_agent_name: 'vibey' });
  });

  it('reloads the stored rows after a refused save', async () => {
    api.patchScopeSettings.mockRejectedValueOnce(new Error('settings_conflict'))
      .mockImplementation(async (request) => storedAfter(request, { ...row('vibey'), enabled: true }));
    await openChannel();
    const reads = api.getSettings.mock.calls.length;
    api.getSettings.mockResolvedValue({ agent_catalog: agentCatalog, channels: { C1: { ...row('vibey'), enabled: true } } });
    await userEvent.click(screen.getByText('route:reviewer'));
    await waitFor(() => expect(api.getSettings.mock.calls.length).toBeGreaterThan(reads));
    await userEvent.click(screen.getByText('route:reviewer'));
    await waitFor(() => expect(api.patchScopeSettings).toHaveBeenCalledTimes(2));
    expect(sent(1)).toMatchObject({ changes: { routing: { agent_name: 'reviewer' } }, expected_agent_name: 'vibey' });
  });
});

describe('telegram topics', () => {
  beforeEach(() => {
    api.getConfig.mockResolvedValue({ platform: 'telegram', platforms: { enabled: ['telegram'] }, telegram: { bot_token: 'bot-test' } });
    api.getSettings.mockResolvedValue({
      agent_catalog: agentCatalog, channels: { '-1001': { ...row('vibey'), enabled: true } }, threads: {},
    });
    api.telegramChats.mockResolvedValue({
      ok: true, channels: [{ id: '-1001', name: 'forum', supports_topics: true, topics: [{ id: '7', name: 'Releases' }] }],
    });
  });

  it('creates an override from the stored channel, then edits only what changed', async () => {
    api.patchScopeSettings.mockImplementation(async (request) => storedAfter(request, { ...row('vibey'), enabled: true }));
    render(<MemoryRouter><ChannelList isPage /></MemoryRouter>);
    await userEvent.click(await screen.findByText('forum'));
    await userEvent.click(await screen.findByText('channelList.topicCustomize'));
    await waitFor(() => expect(api.patchScopeSettings).toHaveBeenCalledTimes(1));
    const topicEditor = await waitFor(() => {
      const buttons = screen.getAllByText('route:reviewer');
      expect(buttons).toHaveLength(2);
      return buttons[1];
    });
    await userEvent.click(topicEditor);
    await waitFor(() => expect(api.patchScopeSettings).toHaveBeenCalledTimes(2));

    expect(sent(0)).toEqual({ platform: 'telegram', scope: 'thread', id: '-1001', thread_id: '7', changes: {} });
    expect(sent(1)).toEqual({
      platform: 'telegram', scope: 'thread', id: '-1001', thread_id: '7',
      changes: { routing: { agent_name: 'reviewer' } }, expected_agent_name: 'vibey',
    });
  });
});
