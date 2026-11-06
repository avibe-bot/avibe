/* @vitest-environment jsdom */
import { render } from '@testing-library/react';
import { expect, it } from 'vitest';

import { GithubIcon } from './GithubIcon';

// The three skills consumers formerly imported a removed Lucide brand icon.
// Keep their shipped two-stroke glyph, styling hook and decorative semantics;
// the page-level browser tests cannot distinguish a substituted or blank SVG.
it('preserves the GitHub mark at the skills icon boundary', () => {
  const { container, unmount } = render(<GithubIcon className="size-3 text-muted" />);
  try {
    const svg = container.querySelector('svg')!;
    expect(svg.classList.contains('size-3')).toBe(true);
    expect(svg.classList.contains('text-muted')).toBe(true);
    expect(svg.getAttribute('aria-hidden')).toBe('true');
    expect(svg.getAttribute('viewBox')).toBe('0 0 24 24');
    expect(svg.getAttribute('fill')).toBe('none');
    expect(svg.getAttribute('stroke')).toBe('currentColor');
    expect(svg.getAttribute('stroke-width')).toBe('2');
    expect(Array.from(svg.querySelectorAll('path'), (path) => path.getAttribute('d'))).toEqual([
      'M15 22v-4a4.8 4.8 0 0 0-1-3.5c3 0 6-2 6-5.5.08-1.25-.27-2.48-1-3.5.28-1.15.28-2.35 0-3.5 0 0-1 0-3 1.5-2.64-.5-5.36-.5-8 0C6 2 5 2 5 2c-.3 1.15-.3 2.35 0 3.5A5.403 5.403 0 0 0 4 9c0 3.5 3 5.5 6 5.5-.39.49-.68 1.05-.85 1.65-.17.6-.22 1.23-.15 1.85v4',
      'M9 18c-4.51 2-5-2-7-2',
    ]);
  } finally {
    unmount();
  }
});
