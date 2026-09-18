/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter, Route, useLocation, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const newSession = vi.hoisted(() => ({
  projects: [] as { id: string; display_name: string; folder_path: string }[],
  target: undefined as { id: string; display_name: string; folder_path: string } | undefined,
  setSelected: vi.fn(),
  sending: false,
  agents: [{ name: 'codex', backend: 'codex' }],
  agentRoute: { agent_name: 'codex', agent_backend: 'codex' } as Record<string, unknown>,
  setAgentRoute: vi.fn(),
  effectiveDefaultAgentName: 'codex',
  needsProject: false,
  error: null as string | null,
  upsertSelectProject: vi.fn(),
  send: vi.fn(),
}));
const authorization = vi.hoisted(() => ({
  capabilities: {
    can_chat: true,
    can_manage_projects: true,
    can_manage_instance: true,
  },
}));
vi.hoisted(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  });
});

vi.mock('../lib/useNewSession', () => ({ useNewSession: () => newSession }));
vi.mock('../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => authorization,
}));
vi.mock('../lib/sessionInfo', () => ({
  canCreateLocalProject: (caps: { can_manage_projects?: boolean }) => Boolean(caps.can_manage_projects),
}));
// The real dialog opens the directory browser as its own first phase; here we
// only need to see that the chip reaches it and that closing it changes nothing.
vi.mock('./workbench/NewProjectDialog', () => ({
  NewProjectDialog: ({ onClose }: { onClose: () => void }) => (
    <div role="dialog" aria-label="directory-browser">
      <button type="button" onClick={onClose}>cancel-folder</button>
    </div>
  ),
}));
vi.mock('./workbench/AgentRoutePicker', () => ({
  AgentRoutePicker: ({ onChange }: { onChange: (patch: Record<string, unknown>) => void }) => (
    <button type="button" onClick={() => onChange({ agent_name: 'claude', agent_backend: 'claude' })}>
      agent-picker
    </button>
  ),
}));
vi.mock('../lib/apiFetch', () => ({ apiFetch: vi.fn() }));
// Only the producer seam is stubbed; the predicate and the banner stay real, so
// these tests exercise the lifecycle the home actually runs.
const readiness = vi.hoisted(() => ({
  value: null as { backend: 'claude' | 'codex' | 'opencode'; ready: boolean } | null,
  /** What the home asked about, in order — the reader's own tests own the answering. */
  questions: [] as Array<{ backend: string | null; asked: boolean }>,
}));
vi.mock('./workbench/backendReadiness', async (importOriginal) => ({
  ...await importOriginal<typeof import('./workbench/backendReadiness')>(),
  useBackendReadiness: (backend: string | null, asked: boolean) => {
    readiness.questions.push({ backend, asked });
    return readiness.value;
  },
}));

import { ToastProvider } from '../context/ToastProvider';
import { SettingsOverlayRouteSurface } from './settings/SettingsOverlayRouteSurface';
import { settingsOverlayNavigationState } from '../lib/settingsOverlay';
import { forgetSetupHandoff } from './workbench/backendReadiness';
import en from '../i18n/en.json';
import { Workbench } from './Workbench';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const 项目 = { id: 'p1', display_name: '中文项目', folder_path: '/Users/max/工作区/中文项目' };

/** Reads back what the history entry actually holds after the home has run. */
const LocationState: React.FC = () => (
  <div data-testid="location-state">{JSON.stringify(useLocation().state ?? null)}</div>
);

/**
 * The ways out of the home, as buttons so they add no links for the
 * continuation-row cases to count: an ordinary departure and return, and
 * Settings opened over the home carrying the same origin the shell's navigation
 * boundary stamps on it.
 */
const WaysOut: React.FC = () => {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <button type="button" onClick={() => void navigate('/projects')}>leave-home</button>
      <button type="button" onClick={() => void navigate('/')}>return-home</button>
      <button type="button" onClick={() => void navigate(-1)}>go-back</button>
      <button
        type="button"
        onClick={() => void navigate('/settings/general', {
          state: settingsOverlayNavigationState({
            destinationPathname: '/settings/general',
            desktop: true,
            source: location,
            targetState: undefined,
          }),
        })}
      >
        open-settings
      </button>
    </>
  );
};

const homeTree = (state?: unknown) => (
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <MemoryRouter initialEntries={[{ pathname: '/', state }]}>
        <WaysOut />
        {/* The real route surface, because how long the setup handoff lives is a
            fact about the route: it has to outlive the guard remounting this home
            in place and end when the user actually leaves, and this is the
            component that knows which route is being shown. */}
        <SettingsOverlayRouteSurface fallbackElement={<div>no-such-route</div>}>
          <Route path="/" element={<><Workbench /><LocationState /></>} />
          <Route path="/projects" element={<div>projects-page</div>} />
          <Route path="/chat/:sessionId" element={<div>chat-page</div>} />
          <Route path="/settings/general" element={<div>settings-page</div>} />
        </SettingsOverlayRouteSurface>
      </MemoryRouter>
    </ToastProvider>
  </I18nextProvider>
);

const renderHome = (state?: unknown) => render(homeTree(state));

const locationState = () => JSON.parse(screen.getByTestId('location-state').textContent || 'null');
const banner = () => screen.queryByText(/is ready to go/);

const input = () => screen.getByPlaceholderText(en.workbench.home.inputPlaceholder) as HTMLTextAreaElement;

beforeEach(() => {
  window.localStorage.clear();
  newSession.projects = [项目];
  newSession.target = 项目;
  newSession.sending = false;
  newSession.needsProject = false;
  newSession.error = null;
  newSession.agentRoute = { agent_name: 'codex', agent_backend: 'codex' };
  newSession.send.mockResolvedValue({ sessionId: 'ses_1', initialMessage: 'x' });
  authorization.capabilities.can_chat = true;
  authorization.capabilities.can_manage_projects = true;
  authorization.capabilities.can_manage_instance = true;
  readiness.value = null;
  readiness.questions.length = 0;
  // Each case is its own page load; the handoff a page holds is not shared.
  forgetSetupHandoff();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Workbench first-task home', () => {
  it('seeds a starting task into the draft and focuses it, without sending', async () => {
    const user = userEvent.setup();
    renderHome();

    await user.click(screen.getByText(en.workbench.home.suggestions.researchTitle));

    await waitFor(() => expect(input().value).toBe(en.workbench.home.suggestions.researchDraft));
    expect(document.activeElement).toBe(input());
    expect(input().selectionStart).toBe(en.workbench.home.suggestions.researchDraft.length);
    expect(newSession.send).not.toHaveBeenCalled();
  });

  it('lets the seeded task be edited before it is sent', async () => {
    const user = userEvent.setup();
    renderHome();

    await user.click(screen.getByText(en.workbench.home.suggestions.planTitle));
    await waitFor(() => expect(input().value).toBe(en.workbench.home.suggestions.planDraft));
    await user.type(input(), '：先做中文文档');
    await user.click(screen.getByRole('button', { name: en.chat.compose.send }));

    expect(newSession.send).toHaveBeenCalledWith(`${en.workbench.home.suggestions.planDraft}：先做中文文档`);
    expect(await screen.findByText('chat-page')).toBeTruthy();
  });

  it('replaces the draft when a second starting point is picked', async () => {
    const user = userEvent.setup();
    renderHome();

    await user.click(screen.getByText(en.workbench.home.suggestions.exploreTitle));
    await waitFor(() => expect(input().value).toBe(en.workbench.home.suggestions.exploreDraft));
    await user.click(screen.getByText(en.workbench.home.suggestions.planTitle));
    await waitFor(() => expect(input().value).toBe(en.workbench.home.suggestions.planDraft));
    expect(newSession.send).not.toHaveBeenCalled();
  });

  it('opens the folder picker straight from the workspace chip and keeps the draft when it is cancelled', async () => {
    const user = userEvent.setup();
    renderHome();

    await user.type(input(), '帮我看看这个仓库');
    const chip = screen.getByRole('button', { name: `Workspace: ${项目.folder_path}` });
    expect(chip.getAttribute('title')).toBe(项目.folder_path);
    expect(chip.textContent).toContain(项目.display_name);

    await user.click(chip);
    // No menu in between: the chip reaches the folder picker itself.
    expect(screen.getByRole('dialog', { name: 'directory-browser' })).toBeTruthy();

    await user.click(screen.getByText('cancel-folder'));
    expect(screen.queryByRole('dialog', { name: 'directory-browser' })).toBeNull();
    expect(input().value).toBe('帮我看看这个仓库');
    expect(newSession.setSelected).not.toHaveBeenCalled();
    expect(newSession.upsertSelectProject).not.toHaveBeenCalled();
  });

  it('keeps the draft when the Agent route changes', async () => {
    const user = userEvent.setup();
    renderHome();

    await user.type(input(), '写一份中文周报');
    await user.click(screen.getByText('agent-picker'));

    expect(newSession.setAgentRoute).toHaveBeenCalledWith({ agent_name: 'claude', agent_backend: 'claude' });
    expect(input().value).toBe('写一份中文周报');
    expect(newSession.send).not.toHaveBeenCalled();
  });

  it('offers to add a workspace instead of sending into nowhere', async () => {
    newSession.needsProject = true;
    newSession.send.mockResolvedValue(null);
    const user = userEvent.setup();
    renderHome();

    await user.type(input(), '开始一个新项目');
    await user.click(screen.getByRole('button', { name: en.chat.compose.send }));

    expect(await screen.findByRole('dialog', { name: 'directory-browser' })).toBeTruthy();
    // The prompt survives the detour — the user retries it, not retypes it.
    expect(input().value).toBe('开始一个新项目');
  });

  it('shows no readiness banner on the wizard claim alone', () => {
    renderHome({ onboardingCompleted: true });

    // The backend has not corroborated anything yet, so there is nothing true
    // to say about it — the claim is what makes the question worth asking, not
    // an answer in itself.
    expect(screen.queryByText(/is ready to go/)).toBeNull();
    expect(screen.queryByRole('button', { name: en.workbench.home.readyDismiss })).toBeNull();
  });

  it('spells the chat-app continuation as one sentence with a real link', () => {
    renderHome();

    expect(screen.getByRole('link', { name: en.workbench.home.continueOnPhone }).getAttribute('href'))
      .toBe('/settings/remote-access');
    const settingsLink = screen.getByRole('link', { name: 'Settings' });
    expect(settingsLink.getAttribute('href')).toBe('/settings/platforms');
    // Guards the source board's missing space: "…apps inSettings".
    expect(settingsLink.parentElement?.textContent).toBe('Connect chat apps in Settings');
  });

  it('sends someone who cannot chat to their projects instead of an unusable box', () => {
    authorization.capabilities.can_chat = false;
    renderHome();

    expect(screen.getByText('projects-page')).toBeTruthy();
  });
});

describe('Workbench onboarding-completion event', () => {
  const ready = { backend: 'codex', ready: true };
  const wizardArrival = { onboardingCompleted: true, settingsBackgroundLocation: { pathname: '/chat/会话' } };

  it('takes the completion out of history and leaves the rest of the entry alone', async () => {
    readiness.value = ready;
    renderHome(wizardArrival);

    expect(await screen.findByText('Codex is ready to go')).toBeTruthy();
    // Consumed, not read: the key is gone from the entry, and the overlay origin
    // the wizard was also carrying is still there for whoever needs it next.
    await waitFor(() => expect(locationState()).toEqual({ settingsBackgroundLocation: { pathname: '/chat/会话' } }));
  });

  it('clears the entry entirely when the completion was all it carried', async () => {
    readiness.value = ready;
    renderHome({ onboardingCompleted: true });

    await waitFor(() => expect(locationState()).toBeNull());
    expect(banner()).toBeTruthy();
  });

  it('still announces a readiness answer that arrives after the entry was rewritten', async () => {
    const view = renderHome({ onboardingCompleted: true });

    // Nothing corroborated yet, and the history entry has already been rewritten.
    await waitFor(() => expect(locationState()).toBeNull());
    expect(banner()).toBeNull();

    readiness.value = ready;
    view.rerender(homeTree({ onboardingCompleted: true }));

    // The event is latched for the visit, so the late answer is not wasted.
    expect(await screen.findByText('Codex is ready to go')).toBeTruthy();
  });

  it('stays dismissed for the rest of the visit', async () => {
    readiness.value = ready;
    const user = userEvent.setup();
    const view = renderHome({ onboardingCompleted: true });

    await user.click(await screen.findByRole('button', { name: en.workbench.home.readyDismiss }));
    expect(banner()).toBeNull();

    view.rerender(homeTree({ onboardingCompleted: true }));
    expect(banner()).toBeNull();
  });

  it('asks the backend nothing on a visit where no completion can be announced', () => {
    readiness.value = ready;
    renderHome();

    // Not a claim about the banner — the home never even asks. A visit that
    // could not announce anything costs the backend no read.
    expect(readiness.questions.length).toBeGreaterThan(0);
    expect(readiness.questions.some((question) => question.asked)).toBe(false);
    expect(banner()).toBeNull();
  });

  it('stops asking about the backend once the banner is dismissed', async () => {
    readiness.value = ready;
    const user = userEvent.setup();
    renderHome({ onboardingCompleted: true });

    await user.click(await screen.findByRole('button', { name: en.workbench.home.readyDismiss }));

    expect(readiness.questions.some((question) => question.asked)).toBe(true);
    expect(readiness.questions.at(-1)).toEqual({ backend: 'codex', asked: false });
  });

  it('still announces after the route guard remounts the home mid-handoff', async () => {
    readiness.value = ready;
    const first = renderHome({ onboardingCompleted: true });
    await waitFor(() => expect(locationState()).toBeNull());
    first.unmount();

    // Crossing the setup boundary is exactly what makes the route guard
    // re-validate, and the home is unmounted and remounted underneath while it
    // does — by which time the entry has already been consumed. Same page, same
    // announcement still owed: losing it here is losing it for good.
    renderHome(null);

    expect(await screen.findByText('Codex is ready to go')).toBeTruthy();
  });

  it('does not come back when that remount happens after a dismissal', async () => {
    readiness.value = ready;
    const user = userEvent.setup();
    const first = renderHome({ onboardingCompleted: true });

    await user.click(await screen.findByRole('button', { name: en.workbench.home.readyDismiss }));
    first.unmount();
    renderHome(null);

    await waitFor(() => expect(screen.getByTestId('location-state')).toBeTruthy());
    expect(banner()).toBeNull();
    expect(readiness.questions.at(-1)).toEqual({ backend: 'codex', asked: false });
  });

  it('does not announce the consumed setup again after leaving the home and coming back', async () => {
    readiness.value = ready;
    const user = userEvent.setup();
    renderHome(wizardArrival);
    expect(await screen.findByText('Codex is ready to go')).toBeTruthy();
    await waitFor(() => expect(locationState()).toEqual({ settingsBackgroundLocation: { pathname: '/chat/会话' } }));

    // A real departure, unlike the guard's remount above: the home is gone
    // because the user went somewhere else, and the visit the wizard handed off
    // to is over with it.
    await user.click(screen.getByText('leave-home'));
    await waitFor(() => expect(screen.getByText('projects-page')).toBeTruthy());
    expect(screen.queryByPlaceholderText(en.workbench.home.inputPlaceholder)).toBeNull();

    await user.click(screen.getByText('return-home'));
    await waitFor(() => expect(screen.getByPlaceholderText(en.workbench.home.inputPlaceholder)).toBeTruthy());
    expect(banner()).toBeNull();
    // Nothing left to announce, so the backend is not asked again either.
    expect(readiness.questions.at(-1)).toEqual({ backend: 'codex', asked: false });
  });

  it('keeps the announcement through Settings opened over the home', async () => {
    readiness.value = ready;
    const user = userEvent.setup();
    renderHome({ onboardingCompleted: true });
    expect(await screen.findByText('Codex is ready to go')).toBeTruthy();

    // Settings keeps this home mounted behind it — on a phone as much as on a
    // desktop — so the user has not left, and the banner they have not answered
    // yet is still theirs to answer when they come back out.
    await user.click(screen.getByText('open-settings'));
    expect(await screen.findByText('settings-page')).toBeTruthy();
    expect(screen.getByPlaceholderText(en.workbench.home.inputPlaceholder)).toBeTruthy();

    await user.click(screen.getByText('go-back'));
    await waitFor(() => expect(screen.queryByText('settings-page')).toBeNull());
    expect(banner()).toBeTruthy();
  });

  it('does not re-announce the same setup on a reload or a return visit', async () => {
    readiness.value = ready;
    const first = renderHome(wizardArrival);
    await waitFor(() => expect(locationState()).toEqual({ settingsBackgroundLocation: { pathname: '/chat/会话' } }));
    const afterConsuming = locationState();
    first.unmount();
    // A reload is a new page, which is what tells it apart from the remount
    // above: nothing carries over but the history entry itself.
    forgetSetupHandoff();

    // And that entry, as it now stands, no longer claims a setup just finished,
    // so the home says nothing about one.
    renderHome(afterConsuming);
    await waitFor(() => expect(screen.getByTestId('location-state')).toBeTruthy());
    expect(banner()).toBeNull();
  });
});

// Both continuations point at OWNER_ONLY_ROUTES, so these cases are written as
// the capability the route guard itself reads rather than as role labels the
// test invents. The server projects `can_manage_instance` from member upward
// and `can_chat` from editor upward (vibe/authorization.py), so the two states
// below are the only ones this row can be in: an instance manager (owner or
// member) sees it, an editor does not, and a viewer never reaches this page at
// all — that redirect is covered above.
describe('Workbench continuation row', () => {
  const ownerOnlyLinks = () => screen
    .queryAllByRole('link')
    .map((link) => link.getAttribute('href') ?? '')
    .filter((href) => href.startsWith('/settings/'));

  it('offers both continuations to someone who can manage the instance', () => {
    renderHome();

    expect(screen.getByText(en.workbench.home.continueOnPhone)).toBeTruthy();
    expect(screen.getByText('Connect chat apps in', { exact: false })).toBeTruthy();
    expect(ownerOnlyLinks().sort()).toEqual(['/settings/platforms', '/settings/remote-access']);
  });

  it('hides the whole row from someone who cannot, while chat stays usable', async () => {
    authorization.capabilities.can_manage_instance = false;
    const user = userEvent.setup();
    renderHome();

    // No dead CTA: neither the links nor the sentence that only exists to
    // introduce them survives.
    expect(screen.queryByText(en.workbench.home.continueOnPhone)).toBeNull();
    expect(screen.queryByText('Connect chat apps in', { exact: false })).toBeNull();
    expect(ownerOnlyLinks()).toEqual([]);

    await user.type(input(), '继续用聊天');
    await user.click(screen.getByRole('button', { name: en.chat.compose.send }));
    expect(newSession.send).toHaveBeenCalledWith('继续用聊天');
  });
});
