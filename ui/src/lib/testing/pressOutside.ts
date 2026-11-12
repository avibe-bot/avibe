import { act, fireEvent } from '@testing-library/react';

/**
 * A full primary-button click outside any dialog. Radix arms its outside-pointer
 * listener a tick after a dialog opens and settles a primary press on the click
 * that follows it, so a bare `pointerdown` would test nothing.
 */
export async function pressOutside(): Promise<void> {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  fireEvent.pointerDown(document.body, { button: 0 });
  fireEvent.click(document.body, { button: 0 });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}
