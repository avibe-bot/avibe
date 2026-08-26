/* @vitest-environment jsdom */

import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useShowPageAnnotation } from './useShowPageAnnotation';

let teardown: (() => void) | null = null;

afterEach(() => {
  teardown?.();
  teardown = null;
  document.body.replaceChildren();
});

const mountBridge = (initialSrc: string | null = '/show/session', shortcutActive = true) => {
  const iframe = document.createElement('iframe');
  document.body.append(iframe);
  const hook = renderHook(({ src, active }) => useShowPageAnnotation(src, active), {
    initialProps: { src: initialSrc, active: shortcutActive },
  });

  act(() => hook.result.current.setIframe(iframe));
  teardown = () => {
    act(() => hook.result.current.setIframe(null));
    hook.unmount();
    iframe.remove();
  };

  return { iframe, ...hook };
};

const reportState = (iframe: HTMLIFrameElement, enabled: boolean) => {
  const source = iframe.contentWindow;
  if (!source) throw new Error('Expected the attached iframe to have a contentWindow');

  act(() => {
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: window.location.origin,
        source,
        data: {
          type: 'avibe:annotation:state',
          enabled,
          mode: 'smart',
          available: true,
        },
      }),
    );
  });
};

const listenForFrameKeydown = (iframe: HTMLIFrameElement) => {
  const listener = vi.fn();
  const frameDocument = iframe.contentDocument;
  if (!frameDocument) throw new Error('Expected the attached iframe to have a contentDocument');
  frameDocument.addEventListener('keydown', listener);
  return listener;
};

const dispatchParentKeydown = (target: EventTarget, key = 'Escape', preventDefault = false) => {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
  if (preventDefault) event.preventDefault();
  act(() => target.dispatchEvent(event));
};

const dispatchAnnotationShortcut = (target: EventTarget) => {
  const event = new KeyboardEvent('keydown', {
    code: 'KeyX',
    altKey: true,
    bubbles: true,
    cancelable: true,
  });
  act(() => target.dispatchEvent(event));
  return event;
};

describe('useShowPageAnnotation shortcut', () => {
  it('enters annotation from the active parent surface and leaves exit to Escape', () => {
    const { iframe } = mountBridge();
    const postMessage = vi.spyOn(iframe.contentWindow!, 'postMessage');
    reportState(iframe, false);
    postMessage.mockClear();

    const enableEvent = dispatchAnnotationShortcut(document.body);
    expect(enableEvent.defaultPrevented).toBe(true);
    expect(postMessage).toHaveBeenLastCalledWith(
      { type: 'avibe:annotation:control', action: 'enable' },
      window.location.origin,
    );

    reportState(iframe, true);
    postMessage.mockClear();
    const enabledEvent = dispatchAnnotationShortcut(document.body);
    expect(enabledEvent.defaultPrevented).toBe(false);
    expect(postMessage).not.toHaveBeenCalled();
  });

  it('binds the same shortcut inside the focused Show Page iframe', () => {
    const { iframe } = mountBridge();
    const postMessage = vi.spyOn(iframe.contentWindow!, 'postMessage');
    reportState(iframe, false);
    postMessage.mockClear();

    dispatchAnnotationShortcut(iframe.contentWindow!);
    expect(postMessage).toHaveBeenLastCalledWith(
      { type: 'avibe:annotation:control', action: 'enable' },
      window.location.origin,
    );
  });

  it('leaves the iframe shortcut with a dialog or menu layered over the page', () => {
    const { iframe } = mountBridge();
    const postMessage = vi.spyOn(iframe.contentWindow!, 'postMessage');
    const frameDocument = iframe.contentDocument!;
    reportState(iframe, false);
    postMessage.mockClear();

    const dialog = frameDocument.createElement('div');
    dialog.setAttribute('role', 'dialog');
    const dialogButton = frameDocument.createElement('button');
    dialog.append(dialogButton);
    frameDocument.body.append(dialog);
    dialogButton.focus();
    const dialogEvent = dispatchAnnotationShortcut(iframe.contentWindow!);
    expect(dialogEvent.defaultPrevented).toBe(false);
    expect(postMessage).not.toHaveBeenCalled();

    dialog.remove();
    const trigger = frameDocument.createElement('button');
    trigger.setAttribute('aria-haspopup', 'menu');
    trigger.setAttribute('aria-expanded', 'true');
    frameDocument.body.append(trigger);
    trigger.focus();
    const menu = frameDocument.createElement('div');
    menu.setAttribute('role', 'menu');
    frameDocument.body.append(menu);
    const menuEvent = dispatchAnnotationShortcut(iframe.contentWindow!);
    expect(menuEvent.defaultPrevented).toBe(false);
    expect(postMessage).not.toHaveBeenCalled();
  });

  it('does not mistake a persistent iframe navigation menu for an open overlay', () => {
    const { iframe } = mountBridge();
    const postMessage = vi.spyOn(iframe.contentWindow!, 'postMessage');
    const frameDocument = iframe.contentDocument!;
    reportState(iframe, false);
    postMessage.mockClear();

    const menu = frameDocument.createElement('div');
    menu.setAttribute('role', 'menu');
    frameDocument.body.append(menu);
    const pageButton = frameDocument.createElement('button');
    frameDocument.body.append(pageButton);
    pageButton.focus();

    const event = dispatchAnnotationShortcut(iframe.contentWindow!);

    expect(event.defaultPrevented).toBe(true);
    expect(postMessage).toHaveBeenCalledWith(
      { type: 'avibe:annotation:control', action: 'enable' },
      window.location.origin,
    );
  });

  it('leaves parent and iframe chords alone when this Show Page is not in front', () => {
    const { iframe } = mountBridge('/show/session', false);
    const postMessage = vi.spyOn(iframe.contentWindow!, 'postMessage');
    reportState(iframe, false);
    postMessage.mockClear();

    const event = dispatchAnnotationShortcut(document.body);
    expect(event.defaultPrevented).toBe(false);
    expect(postMessage).not.toHaveBeenCalled();

    dispatchAnnotationShortcut(iframe.contentWindow!);
    expect(postMessage).not.toHaveBeenCalled();
  });
});

describe('useShowPageAnnotation host Escape forwarding', () => {
  it('forwards parent Escape to the iframe document while annotation is enabled', () => {
    const { iframe } = mountBridge();
    const frameKeydown = listenForFrameKeydown(iframe);
    reportState(iframe, true);

    dispatchParentKeydown(document.body, 'Enter');
    expect(frameKeydown).not.toHaveBeenCalled();

    dispatchParentKeydown(document.body);

    expect(frameKeydown).toHaveBeenCalledTimes(1);
    expect(frameKeydown.mock.calls[0]?.[0]).toMatchObject({ key: 'Escape', bubbles: true, cancelable: true });
  });

  it('leaves Escape with an editable target or editable ancestor', () => {
    const { iframe } = mountBridge();
    const frameKeydown = listenForFrameKeydown(iframe);
    reportState(iframe, true);

    const textarea = document.createElement('textarea');
    document.body.append(textarea);
    dispatchParentKeydown(textarea);

    const input = document.createElement('input');
    document.body.append(input);
    dispatchParentKeydown(input);

    const editor = document.createElement('div');
    editor.setAttribute('contenteditable', 'true');
    const child = document.createElement('span');
    editor.append(child);
    document.body.append(editor);
    dispatchParentKeydown(child);

    expect(frameKeydown).not.toHaveBeenCalled();
  });

  it('leaves Escape with targets inside an open overlay or dialog', () => {
    const { iframe } = mountBridge();
    const frameKeydown = listenForFrameKeydown(iframe);
    reportState(iframe, true);

    const openPopover = document.createElement('div');
    openPopover.dataset.shortcutOverlay = 'open';
    const popoverTarget = document.createElement('button');
    openPopover.append(popoverTarget);
    document.body.append(openPopover);
    dispatchParentKeydown(popoverTarget);

    const roleDialog = document.createElement('div');
    roleDialog.setAttribute('role', 'dialog');
    const roleDialogTarget = document.createElement('button');
    roleDialog.append(roleDialogTarget);
    document.body.append(roleDialog);
    dispatchParentKeydown(roleDialogTarget);

    const nativeDialog = document.createElement('dialog');
    nativeDialog.open = true;
    const nativeDialogTarget = document.createElement('button');
    nativeDialog.append(nativeDialogTarget);
    document.body.append(nativeDialog);
    dispatchParentKeydown(nativeDialogTarget);

    expect(frameKeydown).not.toHaveBeenCalled();
  });

  it('leaves Escape with a target inside a custom menu', () => {
    const { iframe } = mountBridge();
    const frameKeydown = listenForFrameKeydown(iframe);
    reportState(iframe, true);

    const menu = document.createElement('div');
    menu.setAttribute('role', 'menu');
    menu.dataset.shortcutOverlay = 'open';
    const menuItem = document.createElement('button');
    menu.append(menuItem);
    document.body.append(menu);

    dispatchParentKeydown(menuItem);

    expect(frameKeydown).not.toHaveBeenCalled();
  });

  it('leaves Escape with an outside target while a custom menu is open', () => {
    const { iframe } = mountBridge();
    const frameKeydown = listenForFrameKeydown(iframe);
    reportState(iframe, true);

    const menu = document.createElement('div');
    menu.setAttribute('role', 'menu');
    menu.dataset.shortcutOverlay = 'open';
    document.body.append(menu);
    const invokingButton = document.createElement('button');
    document.body.append(invokingButton);

    dispatchParentKeydown(invokingButton);

    expect(frameKeydown).not.toHaveBeenCalled();
  });

  it('leaves Escape with an expanded popup trigger', () => {
    const { iframe } = mountBridge();
    const frameKeydown = listenForFrameKeydown(iframe);
    reportState(iframe, true);

    const trigger = document.createElement('button');
    trigger.setAttribute('aria-expanded', 'true');
    trigger.setAttribute('aria-haspopup', 'menu');
    document.body.append(trigger);

    dispatchParentKeydown(trigger);

    expect(frameKeydown).not.toHaveBeenCalled();
  });

  it('forwards Escape from an expanded non-popup control', () => {
    const { iframe } = mountBridge();
    const frameKeydown = listenForFrameKeydown(iframe);
    reportState(iframe, true);

    const accordionTrigger = document.createElement('button');
    accordionTrigger.setAttribute('aria-expanded', 'true');
    document.body.append(accordionTrigger);

    dispatchParentKeydown(accordionTrigger);

    expect(frameKeydown).toHaveBeenCalledTimes(1);
  });

  it('forwards Escape from the non-modal pinned Show Page window container', () => {
    const { iframe } = mountBridge();
    const frameKeydown = listenForFrameKeydown(iframe);
    reportState(iframe, true);

    const appWindow = document.createElement('div');
    appWindow.setAttribute('role', 'dialog');
    appWindow.dataset.windowId = 'show-window';
    const titleBarButton = document.createElement('button');
    appWindow.append(titleBarButton);
    document.body.append(appWindow);

    dispatchParentKeydown(titleBarButton);

    expect(frameKeydown).toHaveBeenCalledTimes(1);
  });

  it('does not forward an already-prevented parent Escape', () => {
    const { iframe } = mountBridge();
    const frameKeydown = listenForFrameKeydown(iframe);
    reportState(iframe, true);

    dispatchParentKeydown(document.body, 'Escape', true);

    expect(frameKeydown).not.toHaveBeenCalled();
  });

  it('disarms forwarding when annotation becomes disabled', () => {
    const { iframe } = mountBridge();
    const frameKeydown = listenForFrameKeydown(iframe);
    reportState(iframe, true);
    dispatchParentKeydown(document.body);
    expect(frameKeydown).toHaveBeenCalledTimes(1);

    reportState(iframe, false);
    dispatchParentKeydown(document.body);

    expect(frameKeydown).toHaveBeenCalledTimes(1);
  });

  it('does not arm forwarding before the overlay reports state', () => {
    const { iframe } = mountBridge();
    const frameKeydown = listenForFrameKeydown(iframe);

    dispatchParentKeydown(document.body);

    expect(frameKeydown).not.toHaveBeenCalled();
  });

  it('disarms forwarding when the source resets state to unknown', () => {
    const { iframe, result, rerender } = mountBridge();
    const frameKeydown = listenForFrameKeydown(iframe);
    reportState(iframe, true);
    dispatchParentKeydown(document.body);
    expect(frameKeydown).toHaveBeenCalledTimes(1);

    rerender({ src: null });
    expect(result.current.state).toBeNull();
    dispatchParentKeydown(document.body);

    expect(frameKeydown).toHaveBeenCalledTimes(1);
  });

  it('disarms forwarding when the iframe unmounts', () => {
    const { iframe, result } = mountBridge();
    const frameKeydown = listenForFrameKeydown(iframe);
    reportState(iframe, true);

    act(() => result.current.setIframe(null));
    dispatchParentKeydown(document.body);

    expect(frameKeydown).not.toHaveBeenCalled();
  });
});
