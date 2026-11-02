/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '../../../context/ToastProvider';
import en from '../../../i18n/en.json';
import { RouteSurfaceActiveContext } from '../../../lib/routeSurfaceActivity';
import { OWNER_INSTANCE_CAPABILITIES } from '../../../lib/sessionInfo';
import { McpHubSection } from './McpHubSection';

const listMcpServers = vi.fn();
const addMcp = vi.fn();
const assign = vi.fn();
const replaceState = vi.fn();

vi.mock('../../../context/ApiContext', async (original) => ({
  ...(await original<typeof import('../../../context/ApiContext')>()),
  useApi: () => ({
    listMcpServers,
    addMcp,
    discoverMcp: vi.fn(),
    refreshMcp: vi.fn(),
    removeMcp: vi.fn(),
    completeMcpLogin: vi.fn(),
  }),
}));

vi.mock('../../../context/InstanceAuthorizationContext', async (original) => ({
  ...(await original<typeof import('../../../context/InstanceAuthorizationContext')>()),
  useInstanceAuthorization: () => ({ capabilities: OWNER_INSTANCE_CAPABILITIES }),
}));

vi.mock('../../../lib/desktopShell', () => ({ isDesktopShell: () => false }));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}

function renderHub(entry = '/skills') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <RouteSurfaceActiveContext.Provider value={true}>
        <I18nextProvider i18n={i18n}>
          <ToastProvider>
            <McpHubSection />
            <LocationProbe />
          </ToastProvider>
        </I18nextProvider>
      </RouteSurfaceActiveContext.Provider>
    </MemoryRouter>,
  );
}

async function submitPaste(text: string) {
  await screen.findByText('echo');
  fireEvent.click(screen.getByRole('button', { name: en.mcpHub.add }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.change(within(dialog).getByPlaceholderText((value) => value.includes('mcpServers')), {
    target: { value: text },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: en.mcpHub.add }));
  return dialog;
}

beforeEach(() => {
  listMcpServers.mockReset();
  addMcp.mockReset();
  assign.mockReset();
  replaceState.mockReset();
  listMcpServers.mockResolvedValue({
    ok: true,
    oauthMode: 'direct',
    servers: [{ name: 'echo', auth: 'ok', source: 'avibe', transport: 'stdio', toolCount: 1 }],
  });
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: {
      origin: 'http://127.0.0.1:5123',
      pathname: '/skills',
      search: '',
      href: 'http://127.0.0.1:5123/skills',
      assign,
    },
  });
  vi.spyOn(window.history, 'replaceState').mockImplementation(replaceState);
});

afterEach(() => {
  cleanup();
});

describe('McpHubSection add OAuth', () => {
  it('assigns the current tab to the start URL for one direct needs_login add', async () => {
    addMcp.mockResolvedValue({
      ok: true,
      added: [{ name: 'suno', auth: 'needs_login' }],
      unchanged: [],
      failed: [],
    });
    renderHub();
    await submitPaste('{"mcpServers":{"suno":{"url":"https://api.suno.com/mcp"}}}');
    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith('http://127.0.0.1:5123/api/mcp/suno/oauth/start');
    });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('keeps a result list of start links when two servers need login', async () => {
    addMcp.mockResolvedValue({
      ok: true,
      added: [
        { name: 'suno', auth: 'needs_login' },
        { name: 'github', auth: 'needs_login' },
      ],
      unchanged: [],
      failed: [],
    });
    renderHub();
    const dialog = await submitPaste('{"mcpServers":{}}');
    await within(dialog).findByText(en.mcpHub.needsLoginAfterAdd.replace('{{name}}', 'suno'));
    expect(assign).not.toHaveBeenCalled();
    expect(within(dialog).queryByPlaceholderText((value) => value.includes('mcpServers'))).toBeNull();
    expect(within(dialog).queryByRole('button', { name: en.mcpHub.add })).toBeNull();
    const links = within(dialog).getAllByRole('link', { name: en.mcpHub.goAuthorize });
    expect(links.map((node) => node.getAttribute('href'))).toEqual([
      'http://127.0.0.1:5123/api/mcp/suno/oauth/start',
      'http://127.0.0.1:5123/api/mcp/github/oauth/start',
    ]);
    fireEvent.click(within(dialog).getByRole('button', { name: en.common.done }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(listMcpServers.mock.calls.length).toBeGreaterThan(1);
  });

  it('renders the row login control as a start-link with href, target, and rel', async () => {
    listMcpServers.mockResolvedValue({
      ok: true,
      oauthMode: 'direct',
      servers: [{ name: 'suno', auth: 'needs_login', source: 'avibe', transport: 'http', toolCount: 0, url: 'https://api.suno.com/mcp' }],
    });
    renderHub();
    const rowLogin = await screen.findByRole('link', { name: en.mcpHub.login });
    expect(rowLogin.getAttribute('href')).toBe('http://127.0.0.1:5123/api/mcp/suno/oauth/start');
    expect(rowLogin.getAttribute('target')).toBe('_blank');
    expect(rowLogin.getAttribute('rel')).toBe('noopener');
    fireEvent.click(screen.getByRole('button', { name: /suno/ }));
    const logins = screen.getAllByRole('link', { name: en.mcpHub.login });
    expect(logins).toHaveLength(2);
    for (const login of logins) {
      expect(login.getAttribute('href')).toBe('http://127.0.0.1:5123/api/mcp/suno/oauth/start');
      expect(login.getAttribute('target')).toBe('_blank');
      expect(login.getAttribute('rel')).toBe('noopener');
    }
  });

  it('selects the returned server once then lets another click take over', async () => {
    listMcpServers.mockResolvedValue({
      ok: true,
      oauthMode: 'direct',
      servers: [
        { name: 'suno', auth: 'ok', source: 'avibe', transport: 'http', toolCount: 1 },
        { name: 'echo', auth: 'ok', source: 'avibe', transport: 'stdio', toolCount: 1 },
      ],
    });
    renderHub('/skills?mcp=suno&oauth=success');
    const suno = await screen.findByRole('button', { name: /suno/ });
    await waitFor(() => {
      expect(suno.className).toContain('border-mint');
      // The login is announced once; a reload keeps the server but not the announcement.
      expect(screen.getByTestId('location').textContent).toBe('/skills?mcp=suno');
    });
    expect(screen.getAllByText(en.mcpHub.loginComplete)).toHaveLength(1);
    // The router owns the URL; writing window.history behind it would leave the two disagreeing.
    expect(replaceState).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /echo/ }));
    expect(screen.getByRole('button', { name: /echo/ }).className).toContain('border-mint');
    expect(suno.className).not.toContain('border-mint');
  });
});
