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
  return <><h1 tabIndex={-1}>{id}</h1><input aria-label={`${id} draft`} value={value} onChange={(event) => setValue(event.target.value)} /></>;
}
const show = (capability: SetupScreenProps['capability'] = 'enabled', gatewayEnabled: boolean | null = true) =>
  <I18nextProvider i18n={i18n}><SetupFlowShell sequence={SETUP_SCREENS} capability={capability} gatewayEnabled={gatewayEnabled}
    onRetrySetup={vi.fn()} runtimeRead={loadingRegion()} renderScreen={(id, props, ref) => <Screen {...props} id={id} ref={ref} />} /></I18nextProvider>;
beforeEach(() => {
  savedFeeds.length = 0;
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })));
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('derives the interim and complete journey from a static registry alone', () => {
  expect(SETUP_REGISTERED_SCREENS).toEqual(['intro', 'assistants']);
  expect(setupBackTarget(SETUP_REGISTERED_SCREENS, 'assistants')).toBe('intro');
  expect(registeredSetupSequence({ intro: true, providers: true, assistants: true })).toEqual(SETUP_SCREENS);
});
it.each(['pending', 'disabled', 'enabled'] as const)('policy %s never removes a registered screen', (capability) => {
  const { container } = render(show(capability, false));
  expect([...container.querySelectorAll('[data-setup-screen-root]')].map((node) => node.getAttribute('data-setup-screen-root'))).toEqual(SETUP_SCREENS);
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
