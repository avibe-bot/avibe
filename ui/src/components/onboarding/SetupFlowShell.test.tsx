// @vitest-environment jsdom
import { useEffect, useImperativeHandle, useState, type Ref } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { SetupFlowShell } from '../Wizard';
import { SETUP_SCREENS, setupBackTarget, type SetupScreenHandle, type SetupScreenId, type SetupScreenProps } from './setupFlow';
import { registeredSetupSequence, SETUP_REGISTERED_SCREENS } from './setupScreenRegistry';
import { loadingRegion } from '../settings/models/regionRead';
import en from '../../i18n/en.json';

const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
const savedFeeds: SetupScreenProps[] = [];
function Screen({ id, ref, ...props }: SetupScreenProps & { id: SetupScreenId; ref?: Ref<SetupScreenHandle> }) {
  const [value, setValue] = useState('');
  useEffect(() => {
    if (props.active) {
      savedFeeds.push(props);
      props.onActionChange({ labelKey: 'onboarding.welcome.getStarted', disabled: false, busy: false, icon: 'none' });
    }
  }, [props.active, props.onActionChange]);
  useImperativeHandle(ref, () => ({ activate: () => props.onNavigate(id === 'intro' ? 'providers' : 'assistants') }));
  return <><h1 tabIndex={-1}>{id}</h1>
    {id === 'providers' && <><div className="setup-provider-stage" data-testid="incoming-diagram" />
      <div className="setup-destinations" data-testid="incoming-destinations" /></>}
    {id === 'assistants' && <div className="onboarding-assistant" data-testid="incoming-assistant" />}
    <input aria-label={`${id} draft`} value={value} onChange={(event) => setValue(event.target.value)} /></>;
}
const show = (capability: SetupScreenProps['capability'] = 'enabled', gatewayEnabled: boolean | null = true,
  extra: { navigationLocked?: boolean; onRetrySetup?: () => void } = {}) =>
  <I18nextProvider i18n={i18n}><SetupFlowShell sequence={SETUP_SCREENS} capability={capability} gatewayEnabled={gatewayEnabled}
    onRetrySetup={extra.onRetrySetup ?? vi.fn()} navigationLocked={extra.navigationLocked} runtimeRead={loadingRegion()}
    renderScreen={(id, props, ref) => <Screen {...props} id={id} ref={ref} />} /></I18nextProvider>;
beforeEach(() => {
  savedFeeds.length = 0;
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })));
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('derives the journey from a static registry alone, in the contract order', () => {
  expect(SETUP_REGISTERED_SCREENS).toEqual(SETUP_SCREENS);
  expect(setupBackTarget(SETUP_REGISTERED_SCREENS, 'assistants')).toBe('providers');
  expect(setupBackTarget(SETUP_REGISTERED_SCREENS, 'intro')).toBeNull();
  // Registration is build-time membership, not order: a registry written in any order
  // still yields the one journey C2 declares, and an unregistered screen leaves no slot.
  expect(registeredSetupSequence({ assistants: true, providers: true, intro: true })).toEqual(SETUP_SCREENS);
  expect(registeredSetupSequence({ intro: true, assistants: true })).toEqual(['intro', 'assistants']);
});
it.each(['pending', 'disabled', 'enabled'] as const)('policy %s never removes a registered screen', (capability) => {
  const { container } = render(show(capability, false));
  expect([...container.querySelectorAll('[data-setup-screen-root]')].map((node) => node.getAttribute('data-setup-screen-root'))).toEqual(SETUP_SCREENS);
});
// XpVU: a recovery holds the journey by holding the pair. A screen's own `onNavigate`
// reaches the same journey without touching either button, so the hold lives there too.
it('a held journey refuses the screen own navigate, not only the buttons', () => {
  const { container, rerender } = render(show('enabled', true, { navigationLocked: true }));
  const current = () => container.querySelector('[data-setup-screen]')?.getAttribute('data-setup-screen');
  expect(screen.getByRole('button', { name: 'Get started' }).hasAttribute('disabled')).toBe(false);
  // Held, not busy: nothing is running, so nothing spins. The spinner carries Tailwind's
  // `motion-safe:` variant, so the token in the DOM is the whole `motion-safe:animate-spin`
  // — feeding a genuinely busy action proves this selector can find one, which is what
  // makes its absence above an answer about the shell rather than about the query.
  const spinners = () => container.querySelectorAll('.onboarding-primary-action [class~="motion-safe:animate-spin"]').length;
  const feed = (busy: boolean) => act(() => savedFeeds[0].onActionChange({ labelKey: 'onboarding.welcome.getStarted', disabled: false, busy, icon: 'none' }));
  expect(spinners()).toBe(0);
  feed(true); expect(spinners()).toBe(1);
  feed(false); expect(spinners()).toBe(0);
  act(() => savedFeeds[0].onNavigate('providers'));
  expect(current()).toBe('intro');
  rerender(show('enabled', true, { navigationLocked: false }));
  act(() => savedFeeds[0].onNavigate('providers'));
  expect(current()).toBe('providers');
});
it('a held journey does not hand the shared primary to the config retry either', () => {
  const onRetrySetup = vi.fn();
  render(show('disabled', false, { navigationLocked: true, onRetrySetup }));
  const primary = screen.getByRole('button', { name: en.common.retry });
  expect(primary.hasAttribute('disabled')).toBe(false);
  fireEvent.click(primary);
  expect(onRetrySetup).not.toHaveBeenCalled();
});
it('keeps drafts and DOM identity, focuses each heading, and rejects old activation callbacks', async () => {
  const { container } = render(show());
  const intro = screen.getByRole('textbox', { name: 'intro draft' });
  fireEvent.change(intro, { target: { value: '保留 / draft' } });
  const obsolete = savedFeeds[0];
  fireEvent.click(screen.getByRole('button', { name: 'Get started' }));
  expect(document.activeElement?.textContent).toBe('providers');
  const root = container.querySelector('[data-setup-screen-root="intro"]')!;
  expect(root.hasAttribute('hidden')).toBe(true);
  expect(root.hasAttribute('inert')).toBe(true);
  act(() => {
    obsolete.onActionChange({ labelKey: 'common.cancel', disabled: false, busy: false, icon: 'none' });
    obsolete.onNavigate('assistants');
  });
  expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull();
  expect(document.activeElement?.textContent).toBe('providers');
  fireEvent.click(screen.getByRole('button', { name: 'Back to introduction' }));
  expect(screen.getByRole('textbox', { name: 'intro draft' })).toBe(intro);
  expect((intro as HTMLInputElement).value).toBe('保留 / draft');
  act(() => obsolete.onNavigate('assistants'));
  await waitFor(() => expect(document.activeElement?.textContent).toBe('intro'));
  expect(container.querySelectorAll('.onboarding-setup-footer')).toHaveLength(1);
  expect(container.querySelectorAll('.onboarding-primary-action')).toHaveLength(1);
});

it('focuses the arriving heading only after its animated handoff clears inert', () => {
  vi.useFakeTimers();
  const originalAnimate = HTMLElement.prototype.animate;
  Object.defineProperty(HTMLElement.prototype, 'animate', {
    configurable: true,
    value: vi.fn(() => ({ cancel: vi.fn() })),
  });
  try {
    const { container } = render(show());
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'intro' }));
    fireEvent.click(screen.getByRole('button', { name: 'Get started' }));
    const arriving = container.querySelector<HTMLElement>('[data-setup-screen-root="providers"]')!;
    expect(arriving.hasAttribute('hidden')).toBe(false);
    expect(arriving.querySelector('h1')?.textContent).toBe('providers');
    expect(arriving.querySelector('[data-testid="incoming-diagram"]')).toBeTruthy();
    expect(container.querySelector('[data-handoff="providers"] .setup-destinations')).toBeTruthy();
    expect(arriving.hasAttribute('inert')).toBe(true);
    expect(savedFeeds).toHaveLength(1);
    expect(document.activeElement).not.toBe(arriving.querySelector('h1'));
    act(() => vi.advanceTimersByTime(900));
    expect(container.querySelector('[data-handoff]')).toBeNull();
    expect(arriving.querySelector('[data-testid="incoming-destinations"]')).toBeTruthy();
    expect(arriving.hasAttribute('inert')).toBe(false);
    expect(document.activeElement).toBe(arriving.querySelector('h1'));
  } finally {
    vi.useRealTimers();
    if (originalAnimate) Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, value: originalAnimate });
    else Reflect.deleteProperty(HTMLElement.prototype, 'animate');
  }
});
