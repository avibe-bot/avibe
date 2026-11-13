/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { Dialog, DialogContent, DialogTitle } from './dialog';
import { InfoHint } from './info-hint';

afterEach(cleanup);

// The Add key dialog's ⓘ: a hover hint inside a dialog that keeps itself open on outside clicks.
const renderHint = () => render(
  <Dialog open onOpenChange={() => {}}>
    <DialogContent keepOnOutsideClick>
      <DialogTitle>Add a key</DialogTitle>
      {/* The dialog focuses its first field on open, as the Add key form does. */}
      <input aria-label="Name" />
      <p>Body</p>
      <InfoHint hover label="More info" content="Help text" />
    </DialogContent>
  </Dialog>,
);

const tap = (target: Element) => {
  fireEvent.pointerDown(target, { pointerType: 'touch', button: 0 });
  fireEvent.pointerUp(target, { pointerType: 'touch', button: 0 });
  fireEvent.click(target);
};

it('closes on a tap elsewhere and stays closed (closing used to refocus the ⓘ and reopen it)', async () => {
  renderHint();
  await act(async () => { fireEvent.click(screen.getByLabelText('More info')); });
  expect(screen.getByText('Help text')).toBeTruthy();
  await act(async () => { tap(screen.getByText('Body')); });
  expect(screen.queryByText('Help text')).toBeNull();
  // Focus is back on the ⓘ for keyboard users, without reopening it.
  expect(document.activeElement).toBe(screen.getByLabelText('More info'));
});

it('closes on Escape and does not reopen when focus returns', async () => {
  renderHint();
  await act(async () => { fireEvent.click(screen.getByLabelText('More info')); });
  expect(screen.getByText('Help text')).toBeTruthy();
  await act(async () => { fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' }); });
  expect(screen.queryByText('Help text')).toBeNull();
});

it('opens on mouse hover without taking the page (and the ⓘ itself) away from the pointer', async () => {
  renderHint();
  const trigger = screen.getByLabelText('More info');
  await act(async () => { fireEvent.pointerEnter(trigger, { pointerType: 'mouse' }); });
  const panel = screen.getByText('Help text');
  // A modal hint hid everything else, the ⓘ included: the pointer "left" it, the hint closed and
  // reopened in a loop. Now neither the ⓘ nor the panel is hidden.
  expect(panel.closest('[aria-hidden="true"]')).toBeNull();
  expect(trigger.closest('[aria-hidden="true"]')).toBeNull();
  expect(trigger.closest('[data-aria-hidden="true"]')).toBeNull();
  await act(async () => { fireEvent.pointerEnter(trigger, { pointerType: 'mouse' }); });
  expect(screen.getByText('Help text')).toBeTruthy();
});
