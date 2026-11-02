/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
