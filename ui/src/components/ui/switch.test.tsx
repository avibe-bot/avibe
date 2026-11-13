/* @vitest-environment jsdom */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { Switch, SwitchTrack } from './switch';

afterEach(cleanup);

// The thumb is not a fixed-size box centred in the rail and moved by a transform: that was drawn on
// its own layer and, at some zooms and pixel ratios, rounded a pixel high (owner, locked switch).
it.each([
  ['default', true], ['default', false], ['sm', true], ['sm', false],
] as const)('fills the rail inside its padding, moved by margin (%s, checked %s)', (size, checked) => {
  render(<Switch size={size} checked={checked} disabled={checked} onCheckedChange={() => {}} label="Ask" />);
  const rail = screen.getByRole('switch', { name: 'Ask' });
  const thumb = rail.firstElementChild as HTMLElement;
  expect(rail.className).toContain('items-stretch');
  expect(thumb.className).toContain('aspect-square');
  expect(thumb.className).not.toMatch(/\btranslate-|\bsize-\d/);
  expect(thumb.className).toContain(checked ? (size === 'sm' ? 'ml-3' : 'ml-5') : 'ml-0');
});

it('draws SwitchTrack the same way', () => {
  const { container } = render(<SwitchTrack checked />);
  const thumb = container.querySelector('span > span') as HTMLElement;
  expect(thumb.className).toContain('aspect-square');
  expect(thumb.className).not.toMatch(/\btranslate-/);
});
