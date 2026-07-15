import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { WindowBodyGestureShield, shouldShieldWindowBody } from './windowGesture';

describe('shouldShieldWindowBody (§7.1i arm/disarm predicate)', () => {
  it('shields only a visible window while a gesture is active', () => {
    expect(shouldShieldWindowBody(true, false)).toBe(true); // gesture + visible → shield
    expect(shouldShieldWindowBody(true, true)).toBe(false); // minimized → no body to shield
    expect(shouldShieldWindowBody(false, false)).toBe(false); // no gesture → no shield
    expect(shouldShieldWindowBody(false, true)).toBe(false);
  });
});

describe('WindowBodyGestureShield', () => {
  it('renders a filling transparent overlay when active', () => {
    const html = renderToStaticMarkup(<WindowBodyGestureShield active />);
    expect(html).toContain('data-gesture-shield');
    expect(html).toContain('absolute');
    expect(html).toContain('inset-0');
    // It must CATCH stray pointer events (shield the iframe), so NOT pointer-events-none.
    expect(html).not.toContain('pointer-events-none');
  });

  it('renders nothing when inactive', () => {
    expect(renderToStaticMarkup(<WindowBodyGestureShield active={false} />)).toBe('');
  });
});
