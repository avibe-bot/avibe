/** @vitest-environment jsdom */
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { DesktopShowChatCommand, DESKTOP_SHOW_CHAT_EVENT } from './DesktopShowChatCommand';

function Location() {
  const location = useLocation();
  return <output>{location.pathname}{location.search}</output>;
}
function open(sessionId: unknown) {
  act(() => { window.dispatchEvent(new CustomEvent(DESKTOP_SHOW_CHAT_EVENT, { detail: sessionId })); });
}
afterEach(() => {
  cleanup();
  Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
  Reflect.deleteProperty(window, '__AVIBE_NATIVE_APP_WINDOW__');
});
describe('native Show opens chat in the existing main document', () => {
  it('forces chat mode even for the session already on screen, with no document navigation', () => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    render(<MemoryRouter initialEntries={['/chat/ses:7?view=show']}><DesktopShowChatCommand /><Location /></MemoryRouter>);
    const documentBefore = document.documentElement;
    open('ses:7');
    expect(screen.getByRole('status').textContent).toBe('/chat/ses%3A7?view=chat');
    expect(document.documentElement).toBe(documentBefore);
    open('another');
    expect(screen.getByRole('status').textContent).toBe('/chat/another?view=chat');
  });
  it.each(['browser', 'child'])('leaves a %s document in place', (kind) => {
    if (kind === 'child') {
      Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
      Object.defineProperty(window, '__AVIBE_NATIVE_APP_WINDOW__', { value: true, configurable: true });
    }
    render(<MemoryRouter initialEntries={['/apps/show/ses-7']}><DesktopShowChatCommand /><Location /></MemoryRouter>);
    open('ses-7');
    expect(screen.getByRole('status').textContent).toBe('/apps/show/ses-7');
  });
});
