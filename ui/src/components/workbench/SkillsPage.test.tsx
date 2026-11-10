/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import { SkillsPage } from './SkillsPage';
import { skillsLinkFromParams } from './skills/skillsLink';

const api = {
  listProjects: vi.fn(),
  listSkills: vi.fn(),
  checkSkills: vi.fn(),
  listMcpServers: vi.fn(),
  discoverMcp: vi.fn(),
  connectWorkbenchEvents: vi.fn(),
};

vi.mock('../../context/ApiContext', async (original) => ({
  ...(await original<typeof import('../../context/ApiContext')>()),
  useApi: () => api,
}));

vi.mock('../../context/InstanceAuthorizationContext', async (original) => ({
  ...(await original<typeof import('../../context/InstanceAuthorizationContext')>()),
  useInstanceAuthorization: () => ({ capabilities: OWNER_INSTANCE_CAPABILITIES }),
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const project = (id: string, name: string) => ({
  id,
  scope_id: `avibe::project::${id}`,
  display_name: name,
  folder_path: `/work/${name}`,
  created_at: '2026-10-01T00:00:00Z',
  last_active_at: '2026-11-01T00:00:00Z',
  archived: false,
  capabilities: { can_chat: true, has_folder: true },
});

const skill = (name: string, scope: 'global' | 'project') => ({
  name,
  scope,
  path: `/skills/${name}`,
  agents: [],
  description: `${name} description`,
});

function LocationProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="location">{location.pathname + location.search}</output>
      <button type="button" onClick={() => navigate('/skills')}>bare link</button>
    </>
  );
}

function renderPage(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <I18nextProvider i18n={i18n}>
        <ToastProvider>
          <SkillsPage />
          <LocationProbe />
        </ToastProvider>
      </I18nextProvider>
    </MemoryRouter>,
  );
}

const tab = (name: string) => screen.getByRole('radio', { name });

beforeEach(() => {
  // The phone capability strip scrolls its active tab into view; jsdom has no layout.
  Element.prototype.scrollIntoView = vi.fn();
  for (const fn of Object.values(api)) fn.mockReset();
  api.listProjects.mockResolvedValue({ projects: [project('proj_a', 'alpha'), project('proj_b', 'beta')] });
  api.listSkills.mockImplementation(async (params: { scope: string }) => ({
    ok: true,
    skills: params.scope === 'global' ? [skill('pdf-tools', 'global')] : [skill('repo-conventions', 'project'), skill('pdf-tools', 'global')],
  }));
  api.checkSkills.mockResolvedValue({ ok: true, skills: [] });
  api.listMcpServers.mockResolvedValue({
    ok: true,
    oauthMode: 'direct',
    servers: [
      { name: 'echo', auth: 'ok', source: 'avibe', transport: 'stdio', toolCount: 1 },
      { name: 'suno', auth: 'ok', source: 'avibe', transport: 'http', toolCount: 2, url: 'https://api.suno.com/mcp' },
    ],
  });
  api.discoverMcp.mockResolvedValue({ ok: true });
  api.connectWorkbenchEvents.mockReturnValue(() => {});
});

afterEach(() => {
  cleanup();
});

describe('SkillsPage links', () => {
  it('opens the MCP tab on the server an OAuth login returns to, and says the login completed', async () => {
    renderPage('/skills?mcp=suno&oauth=success');
    expect(tab(en.skills.tabMcp).getAttribute('aria-checked')).toBe('true');
    expect(await screen.findByText(en.mcpHub.callHint.replace('{{name}}', 'suno'))).toBeTruthy();
    expect(screen.getByText(en.mcpHub.loginComplete)).toBeTruthy();
  });

  it('opens on global Skills without a query, and a tab click leaves the URL alone', async () => {
    renderPage('/skills');
    expect(tab(en.skills.tabSkills).getAttribute('aria-checked')).toBe('true');
    expect(tab(en.skills.scopeGlobal).getAttribute('aria-checked')).toBe('true');
    await screen.findByText('pdf-tools');
    expect(api.listSkills).toHaveBeenCalledWith({ scope: 'global' });
    expect(api.listMcpServers).not.toHaveBeenCalled();
    fireEvent.click(tab(en.skills.tabMcp));
    await screen.findByText('suno');
    expect(screen.getByTestId('location').textContent).toBe('/skills');
  });

  it('opens a linked project scope with the linked skill in the detail panel', async () => {
    renderPage('/skills?scope=project&project=proj_b&skill=repo-conventions');
    expect(tab(en.skills.scopeProject).getAttribute('aria-checked')).toBe('true');
    expect(await screen.findByText(en.skills.detail.subtitleProject.replace('{{project}}', 'beta'))).toBeTruthy();
    expect(api.listSkills).toHaveBeenCalledWith({ scope: 'all', projectId: 'proj_b' });
    fireEvent.click(screen.getByRole('button', { name: en.common.close }));
    await waitFor(() => expect(screen.queryByText(en.skills.detail.subtitleProject.replace('{{project}}', 'beta'))).toBeNull());
  });

  it('follows a link that changes while the page is open, back to the default on a bare one', async () => {
    renderPage('/skills?tab=mcp');
    await screen.findByText('suno');
    fireEvent.click(screen.getByRole('button', { name: 'bare link' }));
    await waitFor(() => expect(tab(en.skills.tabSkills).getAttribute('aria-checked')).toBe('true'));
  });
});

describe('SkillsPage toolbar on the MCP tab', () => {
  it('runs MCP discovery from the header refresh only on the MCP tab, for the viewed project', async () => {
    renderPage('/skills');
    await screen.findByText('pdf-tools');
    const skillLists = api.listSkills.mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: en.common.refresh }));
    await waitFor(() => expect(api.listSkills.mock.calls.length).toBeGreaterThan(skillLists));
    expect(api.discoverMcp).not.toHaveBeenCalled();

    fireEvent.click(tab(en.skills.tabMcp));
    fireEvent.click(tab(en.skills.scopeProject));
    await waitFor(() => expect(api.listMcpServers).toHaveBeenLastCalledWith('proj_a'));
    const mcpLists = api.listMcpServers.mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: en.common.refresh }));
    await waitFor(() => expect(api.discoverMcp).toHaveBeenCalledWith('proj_a'));
    await waitFor(() => expect(api.listMcpServers.mock.calls.length).toBe(mcpLists + 1));
    expect(api.discoverMcp).toHaveBeenCalledTimes(1);
  });

  it('opens the add dialog from the search row', async () => {
    renderPage('/skills?tab=mcp');
    await screen.findByText('suno');
    const row = screen.getByPlaceholderText(en.mcpHub.searchPlaceholder).closest('div')!.parentElement!;
    const [add, ...others] = screen.getAllByRole('button', { name: en.mcpHub.add });
    expect(others).toEqual([]);
    expect(row.contains(add)).toBe(true);
    fireEvent.click(add);
    expect(await screen.findByRole('dialog', { name: en.mcpHub.addTitle })).toBeTruthy();
  });
});

describe('SkillsPage MCP project view', () => {
  it('lists the selected project, follows the picker, and goes back to the global hub', async () => {
    const listed = api.listMcpServers.getMockImplementation()!;
    let releaseAlpha: () => void = () => undefined;
    const projectRow = (name: string, project: string) => ({ name, source: 'project', scope: `project:${project}`, state: 'ok', auth: 'none', transport: 'http', url: `https://${name}.example/mcp`, toolCount: 0 });
    api.listMcpServers.mockImplementation((project: string | null) => {
      if (project === 'proj_a') {
        // Alpha's list arrives only after the user has moved on to beta.
        return new Promise((resolve) => { releaseAlpha = () => resolve({ ok: true, oauthMode: 'direct', servers: [projectRow('alpha-only', 'proj_a')] }); });
      }
      if (project === 'proj_b') return Promise.resolve({ ok: true, oauthMode: 'direct', servers: [projectRow('beta-only', 'proj_b')] });
      return listed(project);
    });
    renderPage('/skills?tab=mcp');
    await screen.findByText('suno');
    expect(api.listMcpServers).toHaveBeenLastCalledWith(null);
    fireEvent.click(tab(en.skills.scopeProject));
    await waitFor(() => expect(api.listMcpServers).toHaveBeenLastCalledWith('proj_a'));
    fireEvent.click(screen.getByRole('button', { name: 'alpha' }));
    fireEvent.click(await screen.findByRole('button', { name: 'beta' }));
    await waitFor(() => expect(api.listMcpServers).toHaveBeenLastCalledWith('proj_b'));
    await screen.findByText('beta-only');
    await act(async () => releaseAlpha());
    expect(screen.queryByText('alpha-only')).toBeNull();
    expect(screen.getByText('beta-only')).toBeTruthy();
    fireEvent.click(tab(en.skills.scopeGlobal));
    await waitFor(() => expect(api.listMcpServers).toHaveBeenLastCalledWith(null));
  });

  it('shows the global hub for a project without a folder and never asks for its project servers', async () => {
    api.listProjects.mockResolvedValue({
      projects: [{ ...project('proj_c', 'gamma'), folder_path: '', capabilities: { can_chat: true, has_folder: false } }],
    });
    renderPage('/skills?tab=mcp&scope=project&project=proj_c');
    expect(await screen.findByText(en.mcpHub.projectNoFolder)).toBeTruthy();
    await screen.findByText('suno');
    expect(api.listMcpServers.mock.calls.every(([project]) => project === null)).toBe(true);
  });

  it('returns a project OAuth login to that project and its own same-named row', async () => {
    api.listMcpServers.mockImplementation(async (project: string | null) => ({
      ok: true,
      oauthMode: 'direct',
      servers: [
        project
          ? { name: 'suno', source: 'project', scope: 'project:proj_b', state: 'ok', auth: 'none', transport: 'http', url: 'https://project.example/mcp', toolCount: 1 }
          : { name: 'suno', source: 'avibe', scope: 'global', state: 'ok', auth: 'ok', transport: 'http', url: 'https://global.example/mcp', toolCount: 1 },
      ],
    }));
    renderPage('/skills?mcp=suno&scope=project&project=proj_b&oauth=success');
    expect(await screen.findByText(en.mcpHub.loginComplete)).toBeTruthy();
    expect(api.listMcpServers.mock.calls).toEqual([['proj_b']]);
    expect(await screen.findByText(en.mcpHub.callHint.replace('{{name}}', 'suno'))).toBeTruthy();
    expect(screen.getAllByText('https://project.example/mcp').length).toBeGreaterThan(0);
    expect(screen.queryByText('https://global.example/mcp')).toBeNull();
  });
});

describe('skillsLinkFromParams', () => {
  const link = (query: string) => skillsLinkFromParams(new URLSearchParams(query));

  it('is the default view without a query', () => {
    expect(link('')).toEqual({ tab: 'skills', scope: 'global', project: null, skill: null });
  });

  it('opens the MCP tab for a bare server link, unless the link names the Skills tab', () => {
    expect(link('mcp=suno&oauth=success').tab).toBe('mcp');
    expect(link('tab=mcp').tab).toBe('mcp');
    expect(link('tab=skills&mcp=suno').tab).toBe('skills');
    expect(link('tab=other').tab).toBe('skills');
  });

  it('reads the scope, project and skill', () => {
    expect(link('scope=project&project=proj_b&skill=repo-conventions')).toEqual({
      tab: 'skills',
      scope: 'project',
      project: 'proj_b',
      skill: 'repo-conventions',
    });
    expect(link('scope=other').scope).toBe('global');
  });
});
