import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { SessionPinAction } from './SessionPinAction';

const renderAction = (pinned: boolean, pending = false) =>
  renderToStaticMarkup(
    <SessionPinAction
      pinned={pinned}
      pending={pending}
      pinLabel="Pin to top"
      unpinLabel="Unpin"
      onToggle={() => undefined}
    />,
  );

describe('SessionPinAction', () => {
  it('reveals an unpinned action on row hover, keyboard focus, and coarse pointers', () => {
    const html = renderAction(false);

    expect(html).toContain('aria-pressed="false"');
    expect(html).toContain('opacity-0');
    expect(html).toContain('group-hover/sess:opacity-100');
    expect(html).toContain('group-focus-within/sess:opacity-100');
    expect(html).toContain('pointer-coarse:opacity-100');
  });

  it('keeps a pinned action visible and gives hover feedback', () => {
    const html = renderAction(true);

    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('opacity-100');
    expect(html).toContain('hover:bg-cyan/[0.18]');
    expect(html).toContain('hover:scale-105');
    expect(html).toContain('group-hover/pin:-rotate-12');
  });

  it('shows a disabled progress state while persistence is pending', () => {
    const html = renderAction(false, true);

    expect(html).toContain('disabled=""');
    expect(html).toContain('animate-spin');
    expect(html).toContain('cursor-wait');
  });
});
