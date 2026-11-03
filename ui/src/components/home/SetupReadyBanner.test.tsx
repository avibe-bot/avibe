/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter, Route, useLocation, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.hoisted(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  });
});

// Only the producer seam is stubbed; the predicate, the handoff and the banner stay real, so these
// tests exercise the lifecycle the home actually runs.
const readiness = vi.hoisted(() => ({
  value: null as { backend: 'claude' | 'codex' | 'opencode'; ready: boolean } | null,
  /** What the home asked about, in order — the reader's own tests own the answering. */
  questions: [] as Array<{ backend: string | null; asked: boolean }>,
}));
vi.mock('../workbench/backendReadiness', async (importOriginal) => ({
  ...await importOriginal<typeof import('../workbench/backendReadiness')>(),
  useBackendReadiness: (backend: string | null, asked: boolean) => {
    readiness.questions.push({ backend, asked });
    return readiness.value;
  },
}));

import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';
import { settingsOverlayNavigationState } from '../../lib/settingsOverlay';
import { SettingsOverlayRouteSurface } from '../settings/SettingsOverlayRouteSurface';
import { forgetSetupHandoff } from '../workbench/backendReadiness';
import { SetupReadyBanner } from './SetupReadyBanner';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

/** Reads back what the history entry actually holds after the home has run. */
const LocationState: React.FC = () => (
  <div data-testid="location-state">{JSON.stringify(useLocation().state ?? null)}</div>
);

/** The ways out of the home: an ordinary departure and return, and Settings opened over the home
 *  carrying the same origin the shell's navigation boundary stamps on it. */
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

// The home as far as the handoff is concerned: the banner, mounted at `/` like the home mounts it,
// beside the composer it shares the page with.
const homeTree = (state?: unknown) => (
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <MemoryRouter initialEntries={[{ pathname: '/', state }]}>
        <WaysOut />
        {/* The real route surface, because how long the setup handoff lives is a fact about the
            route: it has to outlive the guard remounting this home in place and end when the user
            actually leaves, and this is the component that knows which route is being shown. */}
        <SettingsOverlayRouteSurface fallbackElement={<div>no-such-route</div>}>
          <Route path="/" element={<><SetupReadyBanner backend="codex" /><textarea aria-label="home-composer" /><LocationState /></>} />
          <Route path="/projects" element={<div>projects-page</div>} />
          <Route path="/settings/general" element={<div>settings-page</div>} />
        </SettingsOverlayRouteSurface>
      </MemoryRouter>
    </ToastProvider>
  </I18nextProvider>
);

const renderHome = (state?: unknown) => render(homeTree(state));
const locationState = () => JSON.parse(screen.getByTestId('location-state').textContent || 'null');
const banner = () => screen.queryByText(/is ready to go/);
const composer = () => screen.queryByLabelText('home-composer');

beforeEach(() => {
  readiness.value = null;
  readiness.questions.length = 0;
  // Each case is its own page load; the handoff a page holds is not shared.
  forgetSetupHandoff();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('the setup wizard’s completion on the home', () => {
  const ready = { backend: 'codex' as const, ready: true };
  const wizardArrival = { onboardingCompleted: true, settingsBackgroundLocation: { pathname: '/chat/会话' } };

  it('shows no readiness banner on the wizard claim alone', () => {
    renderHome({ onboardingCompleted: true });
    // The backend has not corroborated anything yet, so there is nothing true to say about it.
    expect(banner()).toBeNull();
    expect(screen.queryByRole('button', { name: en.workbench.home.readyDismiss })).toBeNull();
  });

  it('takes the completion out of history and leaves the rest of the entry alone', async () => {
    readiness.value = ready;
    renderHome(wizardArrival);
    expect(await screen.findByText('Codex is ready to go')).toBeTruthy();
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
    await waitFor(() => expect(locationState()).toBeNull());
    expect(banner()).toBeNull();
    readiness.value = ready;
    view.rerender(homeTree({ onboardingCompleted: true }));
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
    // Crossing the setup boundary makes the route guard re-validate, remounting the home after the
    // entry was consumed. Same page, same announcement still owed.
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
    await user.click(screen.getByText('leave-home'));
    await waitFor(() => expect(screen.getByText('projects-page')).toBeTruthy());
    expect(composer()).toBeNull();
    await user.click(screen.getByText('return-home'));
    await waitFor(() => expect(composer()).toBeTruthy());
    expect(banner()).toBeNull();
    expect(readiness.questions.at(-1)).toEqual({ backend: 'codex', asked: false });
  });

  it('keeps the announcement through Settings opened over the home', async () => {
    readiness.value = ready;
    const user = userEvent.setup();
    renderHome({ onboardingCompleted: true });
    expect(await screen.findByText('Codex is ready to go')).toBeTruthy();
    await user.click(screen.getByText('open-settings'));
    expect(await screen.findByText('settings-page')).toBeTruthy();
    expect(composer()).toBeTruthy();
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
    // A reload is a new page: nothing carries over but the history entry itself.
    forgetSetupHandoff();
    renderHome(afterConsuming);
    await waitFor(() => expect(screen.getByTestId('location-state')).toBeTruthy());
    expect(banner()).toBeNull();
  });
});
