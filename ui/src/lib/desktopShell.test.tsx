// @vitest-environment jsdom
import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SetupHeader } from '../components/Wizard';
import { ChatHeaderBar } from '../components/workbench/ChatPage';
import { DesktopDragRegion } from '../components/DesktopDragRegion';
import { ToastProvider } from '../context/ToastProvider';
import type { WorkbenchSession } from '../context/ApiContext';
import en from '../i18n/en.json';
import zh from '../i18n/zh.json';

vi.mock('../context/InstanceAuthorizationContext', async (original) => ({
  ...await original<typeof import('../context/InstanceAuthorizationContext')>(),
  useInstanceAuthorization: () => ({ capabilities: { can_manage_instance: false } }),
}));
vi.mock('../context/ApiContext', async (original) => ({
  ...await original<typeof import('../context/ApiContext')>(),
  useApi: () => ({}),
}));

vi.hoisted(() => {
  window.matchMedia = vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() });
});

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en', fallbackLng: 'en', resources: { en: { translation: en }, zh: { translation: zh } },
  interpolation: { escapeValue: false },
});

function inShell({ native = true, supported = true } = {}) {
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
  if (native) Object.defineProperty(window, '__AVIBE_DESKTOP_DRAG__', { configurable: true, value: true });
  if (supported) document.head.innerHTML = '<meta name="avibe-shell-drag-regions" content="tauri">';
}

function renderHeaders() {
  document.body.innerHTML = renderToStaticMarkup(
    <I18nextProvider i18n={i18n}><ToastProvider><MemoryRouter>
      <SetupHeader />
      <DesktopDragRegion className="absolute inset-x-0 top-0" />
      <ChatHeaderBar
        session={{ id: 'test', title: 'Header title', status: 'archived' } as WorkbenchSession}
        agents={[]} defaultAgentName={null} onPatch={async () => undefined}
        onBack={() => undefined} working={false} showPageMode={false}
        showPageBusy={false} onToggleShowPage={() => undefined}
        onPrepareShowPageLaunch={async () => false}
        annotation={{ state: null, iframeRef: { current: null }, handleIframeLoad: () => undefined,
          handleShortcutKeyDown: () => undefined, enable: () => undefined, disable: () => undefined, setMode: () => undefined }}
        readOnlyReason="archived"
      />
    </MemoryRouter></ToastProvider></I18nextProvider>,
  );
}

afterEach(() => {
  document.head.innerHTML = '';
  document.body.innerHTML = '';
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
  delete (window as { __AVIBE_DESKTOP_DRAG__?: true }).__AVIBE_DESKTOP_DRAG__;
});

describe('native desktop drag surfaces', () => {
  it('marks existing header blanks and clearance without marking controls or their descendants', () => {
    inShell();
    renderHeaders();
    const regions = [...document.querySelectorAll('[data-tauri-drag-region]')];
    expect(regions).toHaveLength(5); // Setup header + clearance, sidebar clearance, chat outer + inner.
    expect(regions.every((region) => region.getAttribute('data-tauri-drag-region') === '')).toBe(true);
    expect(document.querySelector('header')?.hasAttribute('data-tauri-drag-region')).toBe(true);
    const controls = [...document.querySelectorAll('button')];
    expect(controls.length).toBeGreaterThanOrEqual(2); // Real language trigger and Back.
    for (const control of controls) {
      expect(control.closest('[data-tauri-drag-region]')).not.toBeNull();
      expect(control.hasAttribute('data-tauri-drag-region')).toBe(false);
      expect(control.querySelector('[data-tauri-drag-region]')).toBeNull();
    }
  });

  it('keeps the setup language switcher in browsers and older shells without enabling native dragging', () => {
    for (const mode of ['browser', 'older-shell', 'unsupported-layout'] as const) {
      if (mode !== 'browser') inShell({ native: mode !== 'older-shell', supported: false });
      renderHeaders();
      expect(document.querySelector('button[aria-haspopup="listbox"]')).not.toBeNull();
      expect(document.querySelector('[data-tauri-drag-region]')).toBeNull();
    }
  });
});
