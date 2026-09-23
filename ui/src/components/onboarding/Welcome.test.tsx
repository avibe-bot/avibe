// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Welcome } from '../steps/Welcome';
import { SetupFlowShell } from '../Wizard';
import { loadingRegion } from '../settings/models/regionRead';
import { useRef, useState, type ComponentProps } from 'react';
import { useTranslation } from 'react-i18next';
import { SETUP_SCREENS, type SetupAction, type SetupScreenHandle } from './setupFlow';
import { SETUP_REGISTERED_SCREENS } from './setupScreenRegistry';
import { ASSISTANT_ORDER } from './collaborationTimeline';
import { RouteSurfaceActiveContext } from '@/lib/routeSurfaceActivity';
import { CollaborationStory } from './CollaborationStory';
import { AccessTiles } from './AccessTiles';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';

const mocks = vi.hoisted(() => ({ reduced: false, api: { detectCli: vi.fn() } }));
vi.mock('framer-motion', () => ({ useReducedMotion: () => mocks.reduced }));
vi.mock('../../context/ApiContext', () => ({ useApi: () => mocks.api }));
const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false } });
const wrap = (element: React.ReactNode) => <I18nextProvider i18n={i18n}>{element}</I18nextProvider>;

function Intro(props: Omit<ComponentProps<typeof Welcome>, 'active' | 'onActionChange'> & { active?: boolean }) {
  const { t } = useTranslation();
  const ref = useRef<SetupScreenHandle>(null);
  const [action, setAction] = useState<SetupAction | null>(null);
  return <><Welcome {...props} active={props.active ?? true} ref={ref} onActionChange={setAction} />
    {action && <button disabled={action.disabled} onClick={() => ref.current?.activate()}>{t(action.labelKey)}</button>}
    {/* The setup shell collapses the entry block on every screen; the wrapper mirrors it. */}
    <AccessTiles active={false} /></>;
}

/** The tab going to the background, which jsdom exposes no other way. */
const setHidden = (hidden: boolean) => act(() => {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') });
  document.dispatchEvent(new Event('visibilitychange'));
});

/**
 * The composition scrolling out of sight, which jsdom exposes no other way either: it
 * ships no IntersectionObserver at all. The stub keeps the real contract — observe an
 * element, report whether it intersects — and hands back a switch the test can flip.
 */
function stubIntersectionObserver() {
  const report: ((onScreen: boolean) => void)[] = [];
  class Stub {
    constructor(callback: IntersectionObserverCallback) {
      report.push((onScreen) => callback(
        [{ isIntersecting: onScreen } as IntersectionObserverEntry],
        this as unknown as IntersectionObserver,
      ));
    }
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal('IntersectionObserver', Stub);
  return (onScreen: boolean) => act(() => { for (const notify of report) notify(onScreen); });
}

beforeEach(() => { mocks.reduced = false; vi.clearAllMocks(); void i18n.changeLanguage('en'); });
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  delete (document as Partial<Document>).hidden;
  delete (document as Partial<Document>).visibilityState;
});

describe('Welcome', () => {
  it('checks saved paths before advancing without persisting configuration', async () => {
    mocks.api.detectCli.mockImplementation(async (binary: string) => ({ found: binary !== 'codex', path: `/test/${binary}` }));
    const next = vi.fn();
    render(wrap(<Intro data={{ agents: { claude: { cli_path: '/custom/claude' } } }} onNext={next} />));
    fireEvent.click(screen.getByRole('button', { name: 'Get started' }));
    await waitFor(() => expect(next).toHaveBeenCalledOnce());
    expect(mocks.api.detectCli.mock.calls.map(([binary]) => binary)).toEqual(['/custom/claude', 'codex', 'opencode']);
    expect(next.mock.calls[0][0]).toMatchObject({ __onboardingDetected: true, agents: { claude: { status: 'ok' }, codex: { status: 'missing' }, opencode: { status: 'ok' } } });
  });
  it('retains a failed detection on Welcome and permits retry', async () => {
    mocks.api.detectCli.mockRejectedValueOnce(new Error('Probe unavailable')).mockResolvedValue({ found: false });
    const next = vi.fn();
    render(wrap(<Intro onNext={next} />));
    fireEvent.click(screen.getByRole('button', { name: 'Get started' }));
    await screen.findByRole('alert');
    expect(next).not.toHaveBeenCalled();
    expect(screen.getByText('Probe unavailable')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(next).toHaveBeenCalledOnce());
  });
  // XpVZ/B: in the shell the failure must land in the slot AFTER the shared pair. Left
  // in the screen it sits before the footer, and a long diagnostic there moves the
  // anchor Welcome and the setup screen are supposed to share.
  it('hands a failed detection to the slot below the shared pair instead of the screen', async () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })));
    vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
    mocks.api.detectCli.mockRejectedValue(new Error('Probe unavailable'));
    const { container } = render(wrap(<SetupFlowShell sequence={SETUP_SCREENS} capability="enabled" gatewayEnabled
      onRetrySetup={vi.fn()} runtimeRead={loadingRegion()}
      renderScreen={(id, { active, onActionChange }, ref) => (id === 'intro'
        ? <Welcome active={active} onActionChange={onActionChange} ref={ref} onNext={vi.fn()} />
        : <h1 tabIndex={-1}>{id}</h1>)} />));
    fireEvent.click(screen.getByRole('button', { name: 'Get started' }));
    const alert = await screen.findByRole('alert');
    const aside = container.querySelector('[data-setup-action-aside]') as HTMLElement;
    const footer = container.querySelector('.onboarding-setup-footer') as HTMLElement;
    expect(aside.contains(alert)).toBe(true);
    expect(container.querySelector('[data-setup-screen-root="intro"]')!.contains(alert)).toBe(false);
    // Order, not just ownership: only content after the footer is content the anchor
    // cannot feel.
    expect(footer.compareDocumentPosition(aside) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' }).closest('.onboarding-primary-action')).toBeTruthy();
  });
  // e-Dq: detection awaits three CLI probes and only then asks to move. That
  // continuation captured its answer before the await, so it cannot know the setup
  // route stopped being the surface being read — and teaching this screen to re-check
  // would only move the obligation onto the next screen that forgets. Movement has one
  // owner, the shell, which is where a journey sitting behind another retained surface
  // is held still and where it is handed back when the route returns.
  it('refuses a detection that settles after the route went quiet, then moves when it returns', async () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })));
    vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
    const probes: ((result: { found: boolean; path: string }) => void)[] = [];
    mocks.api.detectCli.mockImplementation(() => new Promise((resolve) => { probes.push(resolve); }));
    const onward = SETUP_REGISTERED_SCREENS[1];
    const shell = (routeActive: boolean) => wrap(
      <RouteSurfaceActiveContext.Provider value={routeActive}>
        <SetupFlowShell sequence={SETUP_REGISTERED_SCREENS} capability="enabled" gatewayEnabled
          onRetrySetup={vi.fn()} runtimeRead={loadingRegion()}
          renderScreen={(id, { active, onActionChange, onNavigate }, ref) => (id === 'intro'
            ? <Welcome active={active} onActionChange={onActionChange} ref={ref} onNext={() => { onNavigate(onward); }} />
            : <h1 tabIndex={-1}>{id}</h1>)} />
      </RouteSurfaceActiveContext.Provider>);
    const { container, rerender } = render(shell(true));
    const current = () => container.querySelector('[data-setup-screen]')?.getAttribute('data-setup-screen');
    const answer = async () => act(async () => { for (const resolve of probes.splice(0)) resolve({ found: true, path: '/test/bin' }); });

    fireEvent.click(screen.getByRole('button', { name: 'Get started' }));
    await waitFor(() => expect(probes).toHaveLength(ASSISTANT_ORDER.length));
    rerender(shell(false));
    await answer();
    expect(current()).toBe('intro');
    expect(document.activeElement?.textContent).not.toBe(onward);
    // Refused, not broken: nothing failed, so there is nothing to report to anybody.
    expect(screen.queryByRole('alert')).toBeNull();

    rerender(shell(true));
    fireEvent.click(screen.getByRole('button', { name: 'Get started' }));
    await waitFor(() => expect(probes).toHaveLength(ASSISTANT_ORDER.length));
    await answer();
    await waitFor(() => expect(current()).toBe(onward));
  });
  it('renders the approved Chinese copy and keeps the entry block mounted, hidden and inert', async () => {
    await i18n.changeLanguage('zh');
    const { container } = render(wrap(<Intro onNext={vi.fn()} />));
    expect(screen.getByRole('heading', { name: '各有所长，接力完成' })).toBeTruthy();
    const block = container.querySelector('.onboarding-access') as HTMLElement;
    expect(block.querySelectorAll('.onboarding-access-tile')).toHaveLength(6);
    expect(block.hasAttribute('hidden')).toBe(true);
    expect(block.hasAttribute('inert')).toBe(true);
  });
});

describe('collaboration lifecycle and access interaction', () => {
  it('starts on its own and offers no playback control', () => {
    vi.useFakeTimers();
    const { container } = render(wrap(<CollaborationStory />));
    act(() => vi.advanceTimersByTime(1800));
    expect(screen.getByTestId('handoff-pulse')).toBeTruthy();
    expect(container.querySelectorAll('[data-state="complete"]')).toHaveLength(1);
    // The approved design has no playback toolbar, so the story ships no control at all —
    // not a renamed one, not a hidden one, not a keyboard-only one.
    expect(container.querySelectorAll('button, [role="button"], input')).toHaveLength(0);
  });
  it('holds the story still while the tab is hidden and resumes in the same phase', () => {
    vi.useFakeTimers();
    const { container } = render(wrap(<CollaborationStory />));
    const diagram = () => container.querySelector('.onboarding-collaboration') as HTMLElement;
    act(() => vi.advanceTimersByTime(5000));
    expect(container.querySelectorAll('[data-state="complete"]')).toHaveLength(2);
    expect(diagram().dataset.motion).toBe('running');

    setHidden(true);
    // `data-motion` is what holds the rendered CSS animations; the cleared timer is what
    // holds the React frame. Both have to stop, or the diagram plays on against a clock
    // nobody is watching and jumps when the tab comes back.
    expect(diagram().dataset.motion).toBe('paused');
    expect(vi.getTimerCount()).toBe(0);
    const frozen = container.innerHTML;
    act(() => vi.advanceTimersByTime(3000));
    expect(container.innerHTML).toBe(frozen);

    setHidden(false);
    expect(diagram().dataset.motion).toBe('running');
    expect(vi.getTimerCount()).toBe(1);
    // Continuity, not restart: the 3000ms spent hidden would have carried the third card
    // past its completion, and a restart would have emptied all three.
    act(() => vi.advanceTimersByTime(100));
    expect(container.querySelectorAll('[data-state="complete"]')).toHaveLength(2);
    act(() => vi.advanceTimersByTime(400));
    expect(container.querySelectorAll('[data-state="complete"]')).toHaveLength(3);
  });
  it('holds the story still while it is scrolled out of sight, with the tab still visible', () => {
    vi.useFakeTimers();
    const scroll = stubIntersectionObserver();
    const { container } = render(wrap(<CollaborationStory />));
    const diagram = () => container.querySelector('.onboarding-collaboration') as HTMLElement;
    act(() => vi.advanceTimersByTime(5000));
    expect(container.querySelectorAll('[data-state="complete"]')).toHaveLength(2);
    expect(diagram().dataset.motion).toBe('running');

    // A hidden tab is only half the lifecycle. Here the document is perfectly visible
    // and it is the diagram that has left the screen — on a short window it can sit
    // entirely above the viewport while the user reads the button below it.
    scroll(false);
    expect(document.hidden).toBeFalsy();
    expect(diagram().dataset.motion).toBe('paused');
    expect(vi.getTimerCount()).toBe(0);
    const frozen = container.innerHTML;
    act(() => vi.advanceTimersByTime(3000));
    expect(container.innerHTML).toBe(frozen);

    scroll(true);
    expect(diagram().dataset.motion).toBe('running');
    expect(vi.getTimerCount()).toBe(1);
    // Continuity, not restart, exactly as for the hidden tab above.
    act(() => vi.advanceTimersByTime(100));
    expect(container.querySelectorAll('[data-state="complete"]')).toHaveLength(2);
    act(() => vi.advanceTimersByTime(400));
    expect(container.querySelectorAll('[data-state="complete"]')).toHaveLength(3);
  });
  it('shows completed work and stops all timers for reduced motion', () => {
    vi.useFakeTimers(); mocks.reduced = true;
    const { container } = render(wrap(<Intro onNext={vi.fn()} />));
    expect(container.querySelectorAll('[data-state="complete"]')).toHaveLength(3);
    expect(screen.queryByTestId('handoff-pulse')).toBeNull();
    // Get started is the only button on the screen.
    expect(screen.getAllByRole('button').map((node) => node.textContent?.trim())).toEqual(['Get started']);
    expect(vi.getTimerCount()).toBe(0);
  });
  it('pauses idle emphasis throughout pointer and keyboard interaction', () => {
    vi.useFakeTimers();
    const { container } = render(wrap(<AccessTiles />));
    // An active consumer still draws the block: hiding it in setup is shell policy.
    expect((container.querySelector('.onboarding-access') as HTMLElement).hasAttribute('hidden')).toBe(false);
    act(() => vi.advanceTimersByTime(2000));
    expect(container.querySelectorAll('[data-emphasis="true"]')).toHaveLength(1);
    fireEvent.pointerEnter(screen.getByRole('list'));
    expect(container.querySelectorAll('[data-emphasis="true"]')).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
    fireEvent.pointerLeave(screen.getByRole('list'));
    fireEvent.focus(screen.getAllByRole('listitem')[0]);
    expect(vi.getTimerCount()).toBe(0);
    fireEvent.blur(screen.getAllByRole('listitem')[0], { relatedTarget: null });
    expect(vi.getTimerCount()).toBe(1);
  });
  it('stops the access rotation with the tab and keeps it stopped for reduced motion', () => {
    vi.useFakeTimers();
    const { container } = render(wrap(<AccessTiles />));
    act(() => vi.advanceTimersByTime(2000));
    expect(container.querySelectorAll('[data-emphasis="true"]')).toHaveLength(1);
    setHidden(true);
    expect(vi.getTimerCount()).toBe(0);
    expect(container.querySelectorAll('[data-emphasis="true"]')).toHaveLength(0);
    setHidden(false);
    expect(vi.getTimerCount()).toBe(1);
  });
  it('stops the access rotation when the tiles themselves scroll out of sight', () => {
    vi.useFakeTimers();
    const scroll = stubIntersectionObserver();
    const { container } = render(wrap(<AccessTiles />));
    act(() => vi.advanceTimersByTime(2000));
    expect(vi.getTimerCount()).toBe(1);
    scroll(false);
    expect(vi.getTimerCount()).toBe(0);
    scroll(true);
    expect(vi.getTimerCount()).toBe(1);
    // The rotation is what suspends; the six tiles it rotates through are always there.
    expect(container.querySelectorAll('.onboarding-access-tile')).toHaveLength(6);
  });
});
