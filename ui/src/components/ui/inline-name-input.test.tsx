/* @vitest-environment jsdom */

// The shared inline name editor: by default (file tree, folder browser, Apps file browser) leaving
// the field cancels; `commitOnBlur` (a label such as a wallet account's name) keeps what was typed.
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { InlineNameInput } from './inline-name-input';

afterEach(cleanup);

const mount = (commitOnBlur?: boolean) => {
  const onCommit = vi.fn();
  const onCancel = vi.fn();
  render(<InlineNameInput initial="old" onCommit={onCommit} onCancel={onCancel} commitOnBlur={commitOnBlur} />);
  return { input: screen.getByRole('textbox'), onCommit, onCancel };
};

it('by default, leaving the field cancels, even with a new name typed (file tree behaviour)', () => {
  const { input, onCommit, onCancel } = mount();
  fireEvent.change(input, { target: { value: 'new.txt' } });
  fireEvent.blur(input);
  expect(onCancel).toHaveBeenCalledOnce();
  expect(onCommit).not.toHaveBeenCalled();
});

it('by default, Enter commits once and Escape cancels once, without a second call from the blur', () => {
  let { input, onCommit, onCancel } = mount();
  fireEvent.change(input, { target: { value: 'new.txt' } });
  fireEvent.keyDown(input, { key: 'Enter' });
  fireEvent.blur(input);
  expect(onCommit).toHaveBeenCalledExactlyOnceWith('new.txt');
  expect(onCancel).not.toHaveBeenCalled();
  cleanup();
  ({ input, onCommit, onCancel } = mount());
  fireEvent.keyDown(input, { key: 'Escape' });
  fireEvent.blur(input);
  expect(onCancel).toHaveBeenCalledOnce();
  expect(onCommit).not.toHaveBeenCalled();
});

it('with commitOnBlur, leaving commits a typed name and cancels an empty one; Escape still cancels', () => {
  let { input, onCommit, onCancel } = mount(true);
  fireEvent.change(input, { target: { value: 'savings' } });
  fireEvent.blur(input);
  expect(onCommit).toHaveBeenCalledExactlyOnceWith('savings');
  expect(onCancel).not.toHaveBeenCalled();
  cleanup();
  ({ input, onCommit, onCancel } = mount(true));
  fireEvent.change(input, { target: { value: '  ' } });
  fireEvent.blur(input);
  expect(onCancel).toHaveBeenCalledOnce();
  expect(onCommit).not.toHaveBeenCalled();
  cleanup();
  ({ input, onCommit, onCancel } = mount(true));
  fireEvent.change(input, { target: { value: 'savings' } });
  fireEvent.keyDown(input, { key: 'Escape' });
  fireEvent.blur(input);
  expect(onCancel).toHaveBeenCalledOnce();
  expect(onCommit).not.toHaveBeenCalled();
});
