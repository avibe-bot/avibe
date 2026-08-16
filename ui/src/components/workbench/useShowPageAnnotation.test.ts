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

const mountBridge = (initialSrc: string | null = '/show/session') => {
  const iframe = document.createElement('iframe');
  document.body.append(iframe);
  const hook = renderHook(({ src }) => useShowPageAnnotation(src), {
    initialProps: { src: initialSrc },
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
    openPopover.dataset.state = 'open';
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
