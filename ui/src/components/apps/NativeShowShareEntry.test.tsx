/** @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import { NativeShowShareEntry, NATIVE_SHOW_ANNOTATE_OPEN_EVENT } from './NativeShowShareEntry';
import { ShowPageAnnotationHostContext } from '../workbench/ShowPageAnnotationHostContext';
import type { AnnotationBridge } from '../workbench/useShowPageAnnotation';

vi.mock('../../context/InstanceAuthorizationContext', () => ({ useInstanceAuthorization: () => ({ capabilities: { can_use_show_pages: true } }) }));
vi.mock('../workbench/ShowPageShareControl', () => ({ ShowPageShareControl: () => <button aria-hidden tabIndex={-1} /> }));
const i18n = createInstance();
void i18n.use(initReactI18next).init({ lng: 'en', resources: { en: { translation: en } } });
afterEach(cleanup);
function setup(route = '/apps/show/ses%3A7', available = true) {
  const annotation: AnnotationBridge = {
    state: { enabled: false, available, mode: 'screenshot' },
    enable: vi.fn(), disable: vi.fn(), setMode: vi.fn(), setIframe: vi.fn(), handleIframeLoad: vi.fn(), handleShortcutKeyDown: vi.fn(),
  };
  const view = render(<I18nextProvider i18n={i18n}><MemoryRouter initialEntries={[route]}>
    <ShowPageAnnotationHostContext.Provider value={{ src: '/show/ses%3A7/', annotation }}>
      <NativeShowShareEntry sessionId="ses:7" />
    </ShowPageAnnotationHostContext.Provider>
  </MemoryRouter></I18nextProvider>);
  const press = () => act(() => { window.dispatchEvent(new Event(NATIVE_SHOW_ANNOTATE_OPEN_EVENT)); });
  return { annotation, press, ...view };
}
describe('native annotation entry uses the shipped picker and bridge', () => {
  it('enables the remembered mode, lets the user choose, and releases the shield on close', () => {
    const { annotation, press, container } = setup();
    press();
    expect(annotation.enable).toHaveBeenCalledExactlyOnceWith();
    expect(screen.getByRole('radiogroup')).toBeTruthy();
    expect(container.querySelector('[data-share-shield]')).toBeTruthy();
    fireEvent.click(screen.getByRole('radio', { name: /Screenshot/i }));
    expect(annotation.setMode).toHaveBeenCalledWith('screenshot');
    fireEvent.click(screen.getByRole('button', { name: 'Turn off annotation' }));
    expect(annotation.disable).toHaveBeenCalledOnce();
    expect(container.querySelector('[data-share-shield]')).toBeNull();
  });
  it('ignores presses away from its own Show', () => {
    const { annotation, press } = setup('/chat/elsewhere');
    press();
    expect(annotation.enable).not.toHaveBeenCalled();
    expect(screen.queryByRole('radiogroup')).toBeNull();
  });
  it('keeps unavailable annotation inert', () => {
    const { annotation, press } = setup(undefined, false);
    press();
    expect(annotation.enable).not.toHaveBeenCalled();
  });
});
