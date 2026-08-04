import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { SessionPinIndicator } from './SessionPinIndicator';
import { sessionRowActionPaddingClass } from './sessionRowLayout';

describe('SessionPinIndicator', () => {
  it('renders no passive status for an unpinned session', () => {
    expect(renderToStaticMarkup(<SessionPinIndicator pinned={false} label="Pinned" />)).toBe('');
  });

  it('renders a non-interactive status icon for a pinned session', () => {
    const html = renderToStaticMarkup(<SessionPinIndicator pinned label="Pinned" />);

    expect(html).toContain('role="img"');
    expect(html).toContain('aria-label="Pinned"');
    expect(html).toContain('text-cyan');
    // Pin / unpin lives in the row's ⋯ menu now; this glyph must not look clickable.
    expect(html).not.toContain('<button');
  });
});

describe('sessionRowActionPaddingClass', () => {
  it('gives the title the full width until the ⋯ trigger is revealed', () => {
    const className = sessionRowActionPaddingClass(false);

    expect(className).toContain('pr-2.5');
    expect(className).toContain('hover:pr-10');
    expect(className).toContain('focus-within:pr-10');
    expect(className).toContain('pointer-coarse:pr-10');
  });

  it('holds the rail open while the menu is open, so the row cannot shift under it', () => {
    expect(sessionRowActionPaddingClass(true)).toBe('pr-10');
  });
});
