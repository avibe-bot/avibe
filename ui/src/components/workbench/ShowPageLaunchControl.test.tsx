/** @vitest-environment jsdom */
import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import { ShowPageLaunchControl } from './ShowPageLaunchControl';

const windowManager = vi.hoisted(() => ({ openApp: vi.fn(), setGestureActive: vi.fn() }));
const dock = vi.hoisted(() => ({ isPinned: vi.fn(() => false), dock: vi.fn(), pin: vi.fn() }));
const showPageDrag = vi.hoisted(() => ({ begin: vi.fn(), end: vi.fn() }));

vi.mock('../../context/WindowManagerContext', () => ({ useWindowManager: () => windowManager }));
vi.mock('../../context/DockContext', () => ({ useDock: () => dock }));
vi.mock('../../context/showPageDrag', () => ({ useShowPageDrag: () => showPageDrag }));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const DESKTOP_SHELL_FLAG = '__AVIBE_DESKTOP_SHELL__';

afterEach(() => {
  cleanup();
  Reflect.deleteProperty(window, DESKTOP_SHELL_FLAG);
  vi.clearAllMocks();
});

/** Renders the control and opens its launch menu the way a keyboard user does. */
const openLaunchMenu = () => {
  render(
    <I18nextProvider i18n={i18n}>
      <ShowPageLaunchControl
        sessionId="ses-1"
        title="Plan"
        showPageMode={false}
        busy={false}
        onToggle={vi.fn()}
        onPrepareLaunch={vi.fn(async () => true)}
      />
    </I18nextProvider>,
  );
  fireEvent.keyDown(screen.getByRole('button', { name: en.chat.showPage.open }), { key: 'ArrowDown' });
  return screen.getByRole('menu', { name: en.chat.showPage.launchMenu });
};

describe('ShowPageLaunchControl launch menu', () => {
  it('offers a new window and a new link in a browser', () => {
    openLaunchMenu();

    expect(screen.getByRole('menuitem', { name: en.chat.showPage.newWindow })).toBeTruthy();
    expect(screen.getByRole('menuitem', { name: en.chat.showPage.newLink })).toBeTruthy();
  });

  it('offers only the new window inside the desktop shell, which never yields a pre-opened tab', () => {
    Object.defineProperty(window, DESKTOP_SHELL_FLAG, { value: true, configurable: true });

    openLaunchMenu();

    expect(screen.getByRole('menuitem', { name: en.chat.showPage.newWindow })).toBeTruthy();
    expect(screen.queryByRole('menuitem', { name: en.chat.showPage.newLink })).toBeNull();
  });
});
