// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { installDesktopTitleBar } from './desktopShell';

const postMessage = vi.fn();
let uninstall: () => void = () => {};

function inShell({ handler = true } = {}) {
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
  if (handler) {
    Object.defineProperty(window, 'webkit', {
      value: { messageHandlers: { avibeShellTitleBar: { postMessage } } },
      configurable: true,
    });
  }
  uninstall = installDesktopTitleBar();
}

function press(target: Element, init: MouseEventInit & { type?: 'mousedown' | 'mouseup' } = {}) {
  const { type = 'mousedown', ...rest } = init;
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, detail: 1, ...rest });
  target.dispatchEvent(event);
  return event;
}

beforeEach(() => {
  document.documentElement.style.setProperty('--shell-titlebar-inset', '28px');
  document.body.innerHTML = `
    <div id="pane">
      <header id="setup" data-shell-title-bar=""><span id="brand">Avibe</span><button id="language"><svg id="icon"></svg></button></header>
      <div id="resizer" role="separator"></div>
    </div>`;
});

afterEach(() => {
  uninstall();
  uninstall = () => {};
  postMessage.mockReset();
  document.documentElement.style.removeProperty('--shell-titlebar-inset');
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
  delete (window as { webkit?: unknown }).webkit;
});

const byId = (id: string) => document.getElementById(id)!;

describe('desktop title bar', () => {
  it('moves the window from a free press anywhere on the overlay band', () => {
    inShell();
    const event = press(byId('pane'), { clientX: 900, clientY: 12 });
    expect(postMessage).toHaveBeenCalledWith('drag');
    expect(event.defaultPrevented).toBe(true);
  });

  it('leaves presses below the band to the page unless the element is marked as title bar', () => {
    inShell();
    const below = press(byId('pane'), { clientY: 60 });
    expect(postMessage).not.toHaveBeenCalled();
    expect(below.defaultPrevented).toBe(false);

    press(byId('brand'), { clientY: 60 });
    expect(postMessage).toHaveBeenCalledWith('drag');
  });

  it('never takes a press a control owns, on the band or in a marked row', () => {
    inShell();
    for (const id of ['language', 'icon', 'resizer']) {
      const event = press(byId(id), { clientY: 12 });
      expect(event.defaultPrevented, id).toBe(false);
    }
    const handled = new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0, detail: 1, clientY: 12 });
    handled.preventDefault();
    byId('pane').dispatchEvent(handled);
    press(byId('pane'), { clientY: 12, button: 2 });
    expect(postMessage).not.toHaveBeenCalled();
  });

  it('answers a double-click on mouseup only when the pointer did not move', () => {
    inShell();
    press(byId('pane'), { clientX: 400, clientY: 10, detail: 2 });
    press(byId('pane'), { type: 'mouseup', clientX: 400, clientY: 10, detail: 2 });
    expect(postMessage).toHaveBeenCalledTimes(1);
    expect(postMessage).toHaveBeenLastCalledWith('double-click');

    press(byId('pane'), { clientX: 400, clientY: 10, detail: 2 });
    press(byId('pane'), { type: 'mouseup', clientX: 420, clientY: 10, detail: 2 });
    expect(postMessage).toHaveBeenCalledTimes(1);
  });

  it('changes nothing outside the shell, under a shell without the handler, or under a standard title bar', () => {
    uninstall = installDesktopTitleBar();
    expect(press(byId('pane'), { clientY: 12 }).defaultPrevented).toBe(false);
    uninstall();

    inShell({ handler: false });
    expect(press(byId('brand'), { clientY: 12 }).defaultPrevented).toBe(false);
    uninstall();

    document.documentElement.style.setProperty('--shell-titlebar-inset', '0px');
    inShell();
    press(byId('pane'), { clientY: 12 });
    expect(postMessage).not.toHaveBeenCalled();
  });
});
