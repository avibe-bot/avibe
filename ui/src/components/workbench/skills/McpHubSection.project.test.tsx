/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { McpServerSummary } from '../../../context/ApiContext';
import { ToastProvider } from '../../../context/ToastProvider';
import en from '../../../i18n/en.json';
import { DENIED_INSTANCE_CAPABILITIES, OWNER_INSTANCE_CAPABILITIES } from '../../../lib/sessionInfo';
import { McpHubSection } from './McpHubSection';

const api = {
  listMcpServers: vi.fn(),
  addMcp: vi.fn(),
  discoverMcp: vi.fn(),
  refreshMcp: vi.fn(),
  removeMcp: vi.fn(),
  approveMcp: vi.fn(),
  configureMcp: vi.fn(),
  completeMcpLogin: vi.fn(),
};
let capabilities = OWNER_INSTANCE_CAPABILITIES;
const assign = vi.fn();

vi.mock('../../../context/ApiContext', async (original) => ({
  ...(await original<typeof import('../../../context/ApiContext')>()),
  useApi: () => api,
}));

vi.mock('../../../context/InstanceAuthorizationContext', async (original) => ({
  ...(await original<typeof import('../../../context/InstanceAuthorizationContext')>()),
  useInstanceAuthorization: () => ({ capabilities }),
}));

vi.mock('../../../lib/desktopShell', () => ({ isDesktopShell: () => false }));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const PROJECT = { id: 'proj_a', name: 'alpha' };
const row = (name: string, state: string, extra: Partial<McpServerSummary> = {}): McpServerSummary => ({
  name,
  source: 'project',
  scope: 'project:proj_a',
  sourcePath: '/work/alpha/.mcp.json',
  state,
  auth: state === 'needs_login' ? 'needs_login' : 'none',
  transport: 'http',
  url: `https://${name}.example/mcp`,
  missingVariables: [],
  placeholders: [],
  toolCount: 0,
  tools: [],
  ...extra,
});
const servers: McpServerSummary[] = [
  row('pending', 'pending_approval', { transport: 'stdio', url: '', command: 'node ./tools/mcp.js' }),
  row('tokened', 'needs_config', { missingVariables: ['API_TOKEN'], placeholders: ['API_TOKEN', 'REGION'] }),
  row('docs', 'needs_login'),
  row('ready', 'ok', { toolCount: 1, tools: [{ name: 'search' }] }),
  row('stale', 'not_generated'),
  { name: 'echo', source: 'avibe', scope: 'global', state: 'needs_login', auth: 'needs_login', transport: 'stdio', command: 'echo-mcp', toolCount: 1 },
];

function renderHub(project: typeof PROJECT | null = PROJECT) {
  return render(
    <MemoryRouter initialEntries={['/skills?tab=mcp&scope=project&project=proj_a']}>
      <I18nextProvider i18n={i18n}>
        <ToastProvider>
          <McpHubSection project={project} />
        </ToastProvider>
      </I18nextProvider>
    </MemoryRouter>,
  );
}

const rowButton = (name: string) => screen.getByRole('button', { name: new RegExp(`^${name}`) });
const detail = () => screen.getByRole('button', { name: en.common.close }).closest('div.rounded-2xl') as HTMLElement;

beforeEach(() => {
  capabilities = OWNER_INSTANCE_CAPABILITIES;
  for (const fn of Object.values(api)) fn.mockReset();
  assign.mockReset();
  api.listMcpServers.mockResolvedValue({ ok: true, oauthMode: 'direct', servers });
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { origin: 'http://127.0.0.1:5123', pathname: '/skills', search: '', href: 'http://127.0.0.1:5123/skills', assign },
  });
});

afterEach(() => {
  cleanup();
});

describe('McpHubSection project view', () => {
  it('renders each row state and its actions from the API row', async () => {
    renderHub();
    await screen.findByText('pending');
    expect(api.listMcpServers).toHaveBeenCalledWith('proj_a');
    expect(within(rowButton('pending')).getByText(en.mcpHub.state.pending_approval)).toBeTruthy();
    expect(within(rowButton('tokened')).getByText(`${en.mcpHub.state.needs_config}: API_TOKEN`)).toBeTruthy();
    expect(within(rowButton('docs')).getByText(en.mcpHub.state.needs_login)).toBeTruthy();
    expect(within(rowButton('ready')).getByText(en.mcpHub.state.ok)).toBeTruthy();
    expect(within(rowButton('stale')).getByText(en.mcpHub.state.not_generated)).toBeTruthy();
    // An inherited global row carries the Skills-style Global chip, no source chip, no project state.
    expect(within(rowButton('echo')).getByText(en.mcpHub.globalBadge)).toBeTruthy();
    expect(within(rowButton('echo')).queryByText(en.mcpHub.source.avibe)).toBeNull();
    expect(within(rowButton('echo')).queryByText(en.mcpHub.state.ok)).toBeNull();

    const actions = (name: string) => {
      fireEvent.click(rowButton(name));
      return within(detail()).getAllByRole('button').map((node) => node.textContent).filter((text) => text && text !== '');
    };
    expect(actions('pending')).toEqual([en.mcpHub.approve, en.mcpHub.configure, en.mcpHub.remove]);
    expect(within(detail()).getByText(en.mcpHub.pendingApprovalHint)).toBeTruthy();
    expect(actions('tokened')).toEqual([en.mcpHub.configure, en.mcpHub.remove]);
    expect(within(detail()).getByText(en.mcpHub.needsConfigHint.replace('{{names}}', 'API_TOKEN'))).toBeTruthy();
    expect(actions('stale')).toEqual([en.mcpHub.configure, en.mcpHub.refresh, en.mcpHub.remove]);
    expect(actions('ready')).toEqual([en.mcpHub.configure, en.mcpHub.refresh, en.mcpHub.remove]);
    // The inherited global row is view-only here; manage it in the Global tab.
    expect(actions('echo')).toEqual([]);
    expect(within(rowButton('echo')).queryByRole('link')).toBeNull();
    expect(within(detail()).queryByRole('link')).toBeNull();
  });

  it.each(['project', 'global'])('configures changed or cleared maps in %s without showing stored values', async (scope) => {
    const projectId = scope === 'project' ? 'proj_a' : null;
    if (!projectId) api.listMcpServers.mockResolvedValue({ ok: true, servers: servers.slice(0, 5).map((item) => ({ ...item, source: 'avibe', scope: 'global' })) });
    api.configureMcp
      .mockResolvedValueOnce({ ok: false, error: { type: 'invalid_definition', message: 'env: bad key', field: 'env' } })
      .mockRejectedValueOnce(new Error('csrf'))
      .mockResolvedValueOnce({ ok: true, state: 'ok' });
    renderHub(projectId ? PROJECT : null);
    await screen.findByText('tokened');
    fireEvent.click(rowButton('tokened'));
    fireEvent.click(within(detail()).getByRole('button', { name: en.mcpHub.configure }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(en.mcpHub.configureMissing.replace('{{names}}', 'API_TOKEN'))).toBeTruthy();
    const variables = en.mcpHub.configureVariables;
    const keys = within(dialog).getAllByLabelText(`${variables} key`) as HTMLInputElement[];
    const values = () => within(dialog).getAllByLabelText(`${variables} value`) as HTMLInputElement[];
    expect(keys.map((node) => node.value)).toEqual(['API_TOKEN', 'REGION']);
    expect(values().map((node) => [node.value, node.type])).toEqual([['', 'password'], ['', 'password']]);
    const save = within(dialog).getByRole('button', { name: en.mcpHub.configureSave }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);

    // A named row left blank blocks Save until the user explicitly chooses an empty value.
    fireEvent.change(values()[0], { target: { value: 'tok-1' } });
    expect(save.disabled).toBe(true);
    fireEvent.click(within(dialog).getAllByRole('button', { name: en.mcpHub.configureEmptyValue })[1]);
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    // Untouched headers are omitted; the typed error keeps the dialog and its input.
    await waitFor(() => expect(api.configureMcp).toHaveBeenNthCalledWith(1, 'tokened', { env: { API_TOKEN: 'tok-1', REGION: '' } }, projectId));
    expect(await within(dialog).findByText(en.mcpHub.errors.invalid_definition.replace('{{message}}', 'env: bad key'))).toBeTruthy();

    // Clearing a map is its own visible action and sends an empty map; a rejected request keeps the dialog.
    fireEvent.click(within(dialog).getAllByRole('button', { name: en.mcpHub.configureClear })[1]);
    expect(within(dialog).getByText(en.mcpHub.configureCleared)).toBeTruthy();
    fireEvent.click(save);
    await waitFor(() => expect(api.configureMcp).toHaveBeenNthCalledWith(2, 'tokened', { env: { API_TOKEN: 'tok-1', REGION: '' }, headers: {} }, projectId));
    expect(await within(dialog).findByText(en.mcpHub.failed)).toBeTruthy();
    expect(values()[0].value).toBe('tok-1');

    // Undo restores the untouched headers; new header values replace the stored map.
    fireEvent.click(within(dialog).getByRole('button', { name: en.mcpHub.configureUndoClear }));
    const headers = en.mcpHub.form.headers;
    fireEvent.click(within(dialog).getAllByRole('button', { name: en.mcpHub.form.addRow })[1]);
    fireEvent.change(within(dialog).getByLabelText(`${headers} key`), { target: { value: 'X-Team' } });
    fireEvent.change(within(dialog).getByLabelText(`${headers} value`), { target: { value: 'core' } });
    fireEvent.click(save);
    await waitFor(() => expect(api.configureMcp).toHaveBeenNthCalledWith(3, 'tokened', { env: { API_TOKEN: 'tok-1', REGION: '' }, headers: { 'X-Team': 'core' } }, projectId));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('adds to the project and keeps the project on every login link', async () => {
    api.addMcp.mockResolvedValue({ ok: true, added: [{ name: 'docs', auth: 'needs_login' }], unchanged: [], failed: [] });
    renderHub();
    await screen.findByText('docs');
    const start = 'http://127.0.0.1:5123/api/mcp/docs/oauth/start?project=proj_a';
    expect(within(rowButton('docs')).getByRole('link', { name: en.mcpHub.login }).getAttribute('href')).toBe(start);
    fireEvent.click(rowButton('docs'));
    expect(within(detail()).getByRole('link', { name: en.mcpHub.login }).getAttribute('href')).toBe(start);

    fireEvent.click(screen.getByRole('button', { name: en.mcpHub.add }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(en.mcpHub.addProjectSubtitle.replace('{{project}}', 'alpha'))).toBeTruthy();
    const text = '{"mcpServers":{"docs":{"url":"https://docs.example/mcp"}}}';
    fireEvent.change(within(dialog).getByPlaceholderText((value) => value.includes('mcpServers')), { target: { value: text } });
    fireEvent.click(within(dialog).getByRole('button', { name: en.mcpHub.add }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(start));
    expect(api.addMcp).toHaveBeenCalledWith(text, 'proj_a');
  });

  it('approves only after the trust confirmation', async () => {
    api.approveMcp.mockRejectedValueOnce(new Error('csrf')).mockResolvedValueOnce({ ok: true, state: 'ok' });
    renderHub();
    await screen.findByText('pending');
    fireEvent.click(rowButton('pending'));
    fireEvent.click(within(detail()).getByRole('button', { name: en.mcpHub.approve }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(en.mcpHub.approveCommandHint.replace('{{path}}', '/work/alpha/.mcp.json'))).toBeTruthy();
    expect(within(dialog).getByText('node ./tools/mcp.js')).toBeTruthy();
    expect(api.approveMcp).not.toHaveBeenCalled();
    // A rejected request keeps the confirmation open and reports the failure.
    fireEvent.click(within(dialog).getByRole('button', { name: en.mcpHub.approve }));
    expect(await screen.findByText(en.mcpHub.failed)).toBeTruthy();
    expect(screen.getByRole('dialog')).toBe(dialog);
    fireEvent.click(within(dialog).getByRole('button', { name: en.mcpHub.approve }));
    await waitFor(() => expect(api.approveMcp).toHaveBeenNthCalledWith(2, 'pending', 'proj_a'));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(api.listMcpServers).toHaveBeenCalledTimes(2);
  });

  it('offers a non-Owner no mutation', async () => {
    capabilities = DENIED_INSTANCE_CAPABILITIES;
    renderHub();
    await screen.findByText('pending');
    expect(screen.queryByRole('button', { name: en.mcpHub.add })).toBeNull();
    expect(screen.queryByRole('button', { name: en.mcpHub.refreshAll })).toBeNull();
    for (const name of ['pending', 'tokened', 'stale', 'ready']) {
      fireEvent.click(rowButton(name));
      const buttons = within(detail()).getAllByRole('button').map((node) => node.textContent);
      expect(buttons.filter((text) => text)).toEqual([]);
    }
  });

  it('groups project rows first, then inherited globals under the Skills-style divider', async () => {
    renderHub();
    await screen.findByText('pending');
    expect(screen.getByPlaceholderText(en.mcpHub.searchProjectPlaceholder)).toBeTruthy();
    const divider = screen.getByText(en.mcpHub.projectSectionGlobal);
    expect(divider.parentElement?.textContent).toContain(en.mcpHub.projectSectionGlobalHint);
    expect(screen.getByText(en.mcpHub.projectSectionLocal).parentElement?.textContent).toContain(en.mcpHub.projectSectionLocalHint);
    // Project rows come before the inherited group; `echo` is the only global row.
    const positions = ['pending', 'tokened', 'docs', 'ready', 'stale'].map((name) =>
      rowButton(name).compareDocumentPosition(divider));
    expect(positions.every((position) => position & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
    expect(rowButton('echo').compareDocumentPosition(divider) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
  });

  it('shows the empty project group with the inherited globals still listed', async () => {
    api.listMcpServers.mockResolvedValue({ ok: true, oauthMode: 'direct', servers: [servers[servers.length - 1]] });
    renderHub();
    await screen.findByText(en.mcpHub.projectEmpty);
    expect(screen.getByText(en.mcpHub.projectSectionGlobal)).toBeTruthy();
    expect(screen.getByText(en.mcpHub.globalBadge)).toBeTruthy();
  });
});
