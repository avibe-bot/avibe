/* @vitest-environment jsdom */
import { renderToStaticMarkup } from 'react-dom/server';
import { Link, MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { inAppChatPath } from './applicationRoutes';
import { internalPwaLinkTarget } from './pwaNavigation';
import { normalizeRestorablePwaPath } from './pwaRouteMemory';

const origin = window.location.origin;
const malformed = ['/\\outside.invalid', '\\\\outside.invalid', '/\\outside.invalid/chat/probe'];

describe('patched routing and application URL boundaries', () => {
  // Router deliberately supports external Links. Application input admission,
  // rather than the library alone, owns this same-origin constraint.
  it.each([...malformed, '//outside.invalid', 'https://outside.invalid/chat/probe', 'javascript:void(0)'])(
    'keeps every admitted application navigation on the current origin: %s',
    input => {
      const targets = [
        inAppChatPath(input, origin),
        normalizeRestorablePwaPath(input),
        internalPwaLinkTarget(input, origin)?.path,
      ];
      for (const target of targets) {
        if (!target) continue;
        const html = renderToStaticMarkup(<MemoryRouter><Link to={target}>probe</Link></MemoryRouter>);
        const container = document.createElement('div');
        container.innerHTML = html;
        const href = container.querySelector('a')!.getAttribute('href')!;
        expect(new URL(href, origin).origin).toBe(origin);
      }
    },
  );

  it('still admits normal internal chat navigation', () => {
    expect(inAppChatPath('/chat/probe', origin)).toBe('/chat/probe');
    expect(normalizeRestorablePwaPath('/chat/probe')).toBe('/chat/probe');
    expect(internalPwaLinkTarget('/chat/probe', origin)?.path).toBe('/chat/probe');
  });
});
