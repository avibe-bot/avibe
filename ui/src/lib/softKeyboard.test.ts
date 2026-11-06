/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';

// Closing the on-screen keyboard after a send: only where one is open. The device
// is set up before the module loads, as the app meets it.

class FakeViewport extends EventTarget {
  height = 844;
  offsetTop = 0;
  scale = 1;
}

async function load({ touch }: { touch: boolean }) {
  vi.resetModules();
  Object.defineProperty(navigator, 'maxTouchPoints', { configurable: true, value: touch ? 5 : 0 });
  const viewport = new FakeViewport();
  Object.defineProperty(window, 'visualViewport', { configurable: true, value: viewport });
  const keyboard = await import('./softKeyboard');
  return { ...keyboard, viewport };
}

const field = () => {
  const textarea = document.createElement('textarea');
  document.body.append(textarea);
  textarea.focus();
  return textarea;
};

afterEach(() => {
  document.body.replaceChildren();
  Reflect.deleteProperty(navigator, 'maxTouchPoints');
  Reflect.deleteProperty(window, 'visualViewport');
});

describe('dismissSoftKeyboard', () => {
  it('blurs the focused field while a soft keyboard is open', async () => {
    const { dismissSoftKeyboard, viewport } = await load({ touch: true });
    const textarea = field();
    viewport.height = 500;
    viewport.dispatchEvent(new Event('resize'));

    expect(dismissSoftKeyboard()).toBe(true);
    expect(document.activeElement).not.toBe(textarea);
  });

  it('keeps the field focused on a touch screen typing on a hardware keyboard', async () => {
    const { dismissSoftKeyboard } = await load({ touch: true });
    const textarea = field();

    expect(dismissSoftKeyboard()).toBe(false);
    expect(document.activeElement).toBe(textarea);
  });

  it('keeps the field focused on a desktop', async () => {
    const { dismissSoftKeyboard, viewport } = await load({ touch: false });
    const textarea = field();
    // A desktop window made shorter is not a keyboard.
    viewport.height = 500;
    viewport.dispatchEvent(new Event('resize'));

    expect(dismissSoftKeyboard()).toBe(false);
    expect(document.activeElement).toBe(textarea);
  });

  it('leaves a focused button alone', async () => {
    const { dismissSoftKeyboard, viewport } = await load({ touch: true });
    const textarea = field();
    viewport.height = 500;
    viewport.dispatchEvent(new Event('resize'));
    const button = document.createElement('button');
    document.body.append(button);
    button.focus();

    expect(dismissSoftKeyboard()).toBe(false);
    expect(document.activeElement).toBe(button);
    expect(textarea).not.toBe(document.activeElement);
  });
});
