// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

import { answerPetResizes, PET_FRAME_EVENT } from './petBridge';

// The shell hides the pet window across a change of its height and shows it
// again once the page answers that it drew at the new height.
describe('answering the shell resizes', () => {
  const invoke = vi.fn(async () => undefined);
  let stop = () => {};
  const setup = (height: number) => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    Object.defineProperty(window, 'innerHeight', { value: height, configurable: true });
    stop = answerPetResizes();
  };
  const frames = async () => {
    for (let i = 0; i < 3; i += 1) await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
  };
  afterEach(() => {
    stop();
    invoke.mockClear();
    Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
    Reflect.deleteProperty(window, '__TAURI_INTERNALS__');
  });

  it('answers once the viewport has the new height and a frame was drawn', async () => {
    setup(163);
    window.dispatchEvent(new CustomEvent(PET_FRAME_EVENT, { detail: { token: 7, height: 600 } }));
    await frames();
    // Not yet: the viewport still has the old height.
    expect(invoke).not.toHaveBeenCalled();
    Object.defineProperty(window, 'innerHeight', { value: 600, configurable: true });
    window.dispatchEvent(new Event('resize'));
    await frames();
    expect(invoke).toHaveBeenCalledWith('pet_frame_drawn', { token: 7 });
    expect(invoke).toHaveBeenCalledTimes(1);
  });

  it('answers at once when the resize reached the page before the event, and only the latest token', async () => {
    setup(600);
    window.dispatchEvent(new CustomEvent(PET_FRAME_EVENT, { detail: { token: 1, height: 163 } }));
    window.dispatchEvent(new CustomEvent(PET_FRAME_EVENT, { detail: { token: 2, height: 600 } }));
    await frames();
    expect(invoke.mock.calls).toEqual([['pet_frame_drawn', { token: 2 }]]);
  });

  it('ignores a malformed event', async () => {
    setup(600);
    window.dispatchEvent(new CustomEvent(PET_FRAME_EVENT, { detail: { token: 'x', height: 600 } }));
    await frames();
    expect(invoke).not.toHaveBeenCalled();
  });
});
