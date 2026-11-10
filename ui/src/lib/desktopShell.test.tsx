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
import {
  canConfigureDesktopNotifications,
  canConfigureDesktopPet,
  desktopNotificationsActions,
  isDesktopShell,
  listenDesktopNotifications,
  tryOpenDesktopAppWindow,
} from './desktopShell';

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
  delete (window as { __AVIBE_DESKTOP_PET_SETTINGS__?: true }).__AVIBE_DESKTOP_PET_SETTINGS__;
  delete (window as { __AVIBE_DESKTOP_NOTIFICATIONS__?: true }).__AVIBE_DESKTOP_NOTIFICATIONS__;
  delete (window as { __AVIBE_DESKTOP_APP_WINDOWS__?: true }).__AVIBE_DESKTOP_APP_WINDOWS__;
  delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
});

describe('native desktop drag surfaces', () => {
  it('uses the MobileBrandHeader height on phones and preserves desktop padding', () => {
    renderHeaders();
    const header = document.querySelector('[data-chat-header]');

    expect(header?.className).toContain('h-[calc(2.75rem+env(safe-area-inset-top))]');
    expect(header?.className).toContain('pt-[env(safe-area-inset-top)]');
    expect(header?.className).toContain('bg-background/92');
    expect(header?.className).toContain('md:h-auto');
    expect(header?.className).toContain('md:bg-surface/70');
    expect(header?.className).toContain('md:py-2.5');
  });

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

  it.each(['browser', 'older-shell', 'without-metadata'] as const)(
    'keeps Setup language switching and derives dragging from shell capability: %s',
    (mode) => {
      if (mode !== 'browser') inShell({ native: mode !== 'older-shell', supported: mode !== 'without-metadata' });
      renderHeaders();
      expect(document.querySelector('button[aria-haspopup="listbox"]')).not.toBeNull();
      if (mode === 'without-metadata') {
        expect(document.querySelector('[data-tauri-drag-region]')).not.toBeNull();
      } else {
        expect(document.querySelector('[data-tauri-drag-region]')).toBeNull();
      }
    },
  );
});

describe('Desktop Mini settings capability', () => {
  it.each([
    ['web', false, false],
    ['older shell', true, false],
    ['current shell', true, true],
  ] as const)('probes the dedicated capability marker for %s', (_label, shell, settings) => {
    if (shell) Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    if (settings) {
      Object.defineProperty(window, '__AVIBE_DESKTOP_PET_SETTINGS__', { configurable: true, value: true });
    }
    expect(isDesktopShell()).toBe(shell);
    expect(canConfigureDesktopPet()).toBe(settings);
  });
});

describe('native desktop notification capability', () => {
  it.each([
    ['web', false, false],
    ['older shell', true, false],
    ['current shell', true, true],
  ] as const)('probes the dedicated capability marker for %s', (_label, shell, notifications) => {
    if (shell) Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    if (notifications) {
      Object.defineProperty(window, '__AVIBE_DESKTOP_NOTIFICATIONS__', { configurable: true, value: true });
      Object.defineProperty(window, '__TAURI_INTERNALS__', {
        configurable: true,
        value: { invoke: vi.fn() },
      });
    }
    expect(canConfigureDesktopNotifications()).toBe(notifications);
    if (notifications) {
      expect(desktopNotificationsActions()).not.toBeNull();
    } else {
      expect(desktopNotificationsActions()).toBeNull();
    }
  });

  it('uses named IPC arguments and receives menu updates', async () => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    Object.defineProperty(window, '__AVIBE_DESKTOP_NOTIFICATIONS__', { configurable: true, value: true });
    const invoke = vi.fn(async (command: string) => command === 'desktop_notifications_state' ? false : true);
    Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } });

    const actions = desktopNotificationsActions();
    if (!actions) throw new Error('desktop notification actions should be available');
    await expect(actions.get()).resolves.toBe(false);
    await expect(actions.set(true)).resolves.toBe(true);
    expect(invoke).toHaveBeenNthCalledWith(1, 'desktop_notifications_state');
    expect(invoke).toHaveBeenNthCalledWith(2, 'desktop_notifications_set_enabled', { enabled: true });

    const handler = vi.fn();
    const stop = listenDesktopNotifications(handler);
    window.dispatchEvent(new CustomEvent('avibe:desktop-notifications', { detail: { enabled: false } }));
    expect(handler).toHaveBeenCalledWith(false);
    stop();
  });
});

describe('native app window capability', () => {
  it.each([
    ['web', false, false],
    ['older shell', true, false],
    ['current shell', true, true],
  ] as const)('probes the dedicated marker for %s', async (_label, shell, appWindows) => {
    if (shell) Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    if (appWindows) Object.defineProperty(window, '__AVIBE_DESKTOP_APP_WINDOWS__', { configurable: true, value: true });
    const invoke = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } });

    expect(await tryOpenDesktopAppWindow({
      appId: 'files',
      sessionId: null,
      title: 'Files',
      newWindow: false,
    })).toBe(appWindows);
    if (appWindows) {
      expect(invoke).toHaveBeenCalledWith('open_app_window', {
        request: {
          appId: 'files',
          sessionId: null,
          title: 'Files',
          newWindow: false,
        },
      });
    } else {
      expect(invoke).not.toHaveBeenCalled();
    }
  });

  it('falls back when the native bridge rejects the request', async () => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    Object.defineProperty(window, '__AVIBE_DESKTOP_APP_WINDOWS__', { configurable: true, value: true });
    const invoke = vi.fn().mockRejectedValue(new Error('denied'));
    Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } });

    await expect(tryOpenDesktopAppWindow({
      appId: 'showpage',
      sessionId: 'session-1',
      title: 'Show Page',
      newWindow: true,
    })).resolves.toBe(false);
  });
});
