import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { SessionPinIndicator } from './SessionPinIndicator';

describe('SessionPinIndicator', () => {
  it('renders no passive status for an unpinned session', () => {
    expect(renderToStaticMarkup(<SessionPinIndicator pinned={false} label="Pinned" />)).toBe('');
  });

  it('renders a non-interactive status icon for a pinned session', () => {
    const html = renderToStaticMarkup(<SessionPinIndicator pinned label="Pinned" />);

    expect(html).toContain('role="img"');
    expect(html).toContain('aria-label="Pinned"');
    expect(html).toContain('text-cyan-ink');
    expect(html).not.toContain('<button');
  });
});
