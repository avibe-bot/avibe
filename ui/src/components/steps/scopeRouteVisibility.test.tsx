// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ChannelList } from './ChannelList';
import { UserList } from './UserList';

// What a settings row tells the user about its route: the route the scope runs
// (its own model and effort over its Agent's), a save still in flight, and
// whether the conversation's current session keeps its previous Agent.

const api = vi.hoisted(() => ({
  getConfig: vi.fn(), getSettings: vi.fn(), slackChannels: vi.fn(),
  getUsers: vi.fn(), getBindCodes: vi.fn(), patchScopeSettings: vi.fn(),
}));
const toast = vi.hoisted(() => vi.fn());
vi.mock('@/context/ApiContext', async (loadOriginal) => ({
  ...await loadOriginal<typeof import('@/context/ApiContext')>(), useApi: () => api,
}));
vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_manage_access_members: true } }),
}));
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: toast }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../shared/RoutingConfigPanel', () => ({
  RoutingConfigPanel: ({ value, onChange, saving, routeNotice }: {
    value: { routing: object }; onChange: (patch: object) => void; saving?: boolean; routeNotice?: string | null;
  }) => (
    <div data-testid="panel" data-saving={saving ? 'yes' : 'no'}>
      {['vibey', 'reviewer'].map((agent) => (
        <button key={agent} type="button" onClick={() => onChange({ routing: { ...value.routing, agent_name: agent } })}>
          {`route:${agent}`}
        </button>
      ))}
      <button type="button" onClick={() => onChange({ show_message_types: ['assistant', 'toolcall'] })}>types</button>
      {routeNotice && <p>{routeNotice}</p>}
    </div>
  ),
}));

const agentCatalog = {
  default_agent_name: 'vibey',
  agents: [
    { name: 'vibey', display_name: 'vibey', backend: 'vibey', model: 'claude-sonnet-5-5', reasoning_effort: null },
    { name: 'reviewer', display_name: 'reviewer', backend: 'vibey', model: 'gpt-6-astra', reasoning_effort: 'low' },
  ],
};
const user = (routing: object) => ({
  display_name: 'Liu', is_admin: true, bound_at: '', enabled: true, show_message_types: ['assistant'],
  custom_cwd: null, routing, expected_agent_name: null,
});
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
};

afterEach(cleanup);
beforeEach(() => {
  vi.resetAllMocks();
  api.getBindCodes.mockResolvedValue({ ok: true, bind_codes: [] });
  api.getConfig.mockResolvedValue({ platform: 'lark', platforms: { enabled: ['lark'] } });
  api.getSettings.mockResolvedValue({ agent_catalog: agentCatalog });
});

function serveUsers(users: Record<string, object>) {
  api.getUsers.mockImplementation(async (platform) => ({ ok: true, users: platform === 'lark' ? users : {} }));
}

describe('users page rows', () => {
  it('shows the route each user runs, marking an inherited one', async () => {
    serveUsers({
      ou_default: { ...user({ agent_name: null, model: null, reasoning_effort: null }), display_name: 'Default' },
      ou_custom: { ...user({ agent_name: null, model: 'gpt-6-astra', reasoning_effort: 'high' }), display_name: 'Custom' },
      ou_agent: { ...user({ agent_name: 'reviewer', model: null, reasoning_effort: null }), display_name: 'Agent' },
    });
    render(<MemoryRouter><UserList /></MemoryRouter>);

    expect(await screen.findByText('common.default · vibey/claude-sonnet-5-5 · ou_default')).toBeTruthy();
    expect(screen.getByText('vibey/gpt-6-astra · chat.picker.effortOptions.high · ou_custom')).toBeTruthy();
    expect(screen.getByText('reviewer/gpt-6-astra · chat.picker.effortOptions.low · ou_agent')).toBeTruthy();
  });

  it('opens the row a chat links to, even a disabled one, and drops the link', async () => {
    serveUsers({
      ou_1: user({ agent_name: 'vibey', model: null, reasoning_effort: null }),
      ou_2: { ...user({ agent_name: 'vibey', model: null, reasoning_effort: null }), display_name: 'Wang', enabled: false },
    });
    let search = '';
    const Location = () => { search = useLocation().search; return null; };
    render(
      <MemoryRouter initialEntries={['/settings/platforms/users?platform=lark&user=ou_2']}>
        <UserList />
        <Location />
      </MemoryRouter>
    );

    const panel = await screen.findByTestId('panel');
    expect(screen.getByText('Wang').closest('.rounded-xl')?.contains(panel)).toBe(true);
    expect(search).toBe('');
  });

  it('shows a save on its row, announces only failures, and says when the session keeps its Agent', async () => {
    serveUsers({ ou_1: user({ agent_name: 'vibey', model: null, reasoning_effort: null }) });
    const pending = deferred<object>();
    api.patchScopeSettings.mockReturnValueOnce(pending.promise);
    render(<MemoryRouter><UserList /></MemoryRouter>);
    await userEvent.click(await screen.findByText('Liu'));

    await userEvent.click(screen.getByText('route:reviewer'));
    expect(screen.getByTestId('panel').dataset.saving).toBe('yes');
    expect(screen.getByLabelText('common.saving')).toBeTruthy();
    pending.resolve({ ok: true, settings: user({ agent_name: 'reviewer' }), needs_new_session: true });

    expect(await screen.findByText('channelList.newSessionHint')).toBeTruthy();
    expect(screen.getByTestId('panel').dataset.saving).toBe('no');
    expect(toast).not.toHaveBeenCalled();

    api.patchScopeSettings.mockRejectedValueOnce(new Error('settings_conflict'));
    await userEvent.click(screen.getByText('route:vibey'));
    await waitFor(() => expect(toast).toHaveBeenCalledWith('userList.settingsSaveFailed', 'error'));
  });
  it('keeps the session note of a route change answered after a later edit of the same row', async () => {
    serveUsers({ ou_1: user({ agent_name: 'vibey', model: null, reasoning_effort: null }) });
    const route = deferred<object>();
    api.patchScopeSettings.mockReturnValueOnce(route.promise)
      .mockResolvedValueOnce({ ok: true, settings: { ...user({ agent_name: 'reviewer' }), show_message_types: ['assistant', 'toolcall'] } });
    render(<MemoryRouter><UserList /></MemoryRouter>);
    await userEvent.click(await screen.findByText('Liu'));

    await userEvent.click(screen.getByText('route:reviewer'));
    await userEvent.click(screen.getByText('types'));
    await waitFor(() => expect(api.patchScopeSettings).toHaveBeenCalledTimes(2));
    route.resolve({ ok: true, settings: user({ agent_name: 'reviewer' }), needs_new_session: true });

    expect(await screen.findByText('channelList.newSessionHint')).toBeTruthy();
  });
});

describe('groups page rows', () => {
  beforeEach(() => {
    api.getConfig.mockResolvedValue({ platform: 'slack', platforms: { enabled: ['slack'] }, slack: { bot_token: 'xoxb-test' } });
    api.slackChannels.mockResolvedValue({ ok: true, channels: [{ id: 'C1', name: 'general' }] });
  });

  it('shows the route a group runs and keeps the notice off when the session follows the route', async () => {
    const channel = {
      enabled: true, show_message_types: ['assistant'], custom_cwd: null, require_mention: null, require_bind: null,
      routing: { agent_name: null, model: 'gpt-6-astra', reasoning_effort: null }, expected_agent_name: null,
    };
    api.getSettings.mockResolvedValue({ agent_catalog: agentCatalog, channels: { C1: channel } });
    api.patchScopeSettings.mockResolvedValue({
      ok: true, settings: { ...channel, routing: { agent_name: 'reviewer' } }, needs_new_session: false,
    });
    render(<MemoryRouter><ChannelList isPage /></MemoryRouter>);

    expect(await screen.findByText('vibey/gpt-6-astra · ID: C1')).toBeTruthy();
    await userEvent.click(screen.getByText('general'));
    await userEvent.click(within(screen.getByTestId('panel')).getByText('route:reviewer'));
    await waitFor(() => expect(api.patchScopeSettings).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId('panel').dataset.saving).toBe('no'));
    expect(screen.queryByText('channelList.newSessionHint')).toBeNull();
    expect(toast).not.toHaveBeenCalled();
  });
});
