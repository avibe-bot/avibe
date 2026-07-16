import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { ShowPageAvatarContent, ShowPageAvatarTile } from './showPageAvatarTile';

// Guards the Dock-drag invariant (§7.1h item 3): a Dock tile is a framer-motion
// Reorder.Item whose drag pan starts ONLY when the press lands on a non-interactive
// CHILD, never on the interactive <button> itself. So the avatar content must render
// as a FILLING child element (never a bare text node) and must NOT be
// `pointer-events-none` (which would push the press back to the button).
describe('ShowPageAvatarContent — Dock-drag press-target invariant', () => {
  it('renders the letter as a filling wrapping element, not a bare text node', () => {
    const html = renderToStaticMarkup(<ShowPageAvatarContent iconUrl={null} letter="S" />);
    expect(html.startsWith('<span')).toBe(true); // a real element, not the bare string "S"
    expect(html).toContain('>S</span>'); // the letter lives inside that element
    expect(html).toContain('size-full'); // it FILLS the tile → any press lands on the child
    expect(html).not.toContain('pointer-events-none');
  });

  it('renders the icon as a filling <img draggable="false"> with no pointer-events-none', () => {
    const html = renderToStaticMarkup(
      <ShowPageAvatarContent iconUrl="/api/show-pages/ses_1/icon?v=abc" letter="S" />,
    );
    expect(html).toContain('<img');
    expect(html).toContain('draggable="false"'); // native image DnD stays disabled (#906)
    expect(html).toContain('size-full'); // the img is the filling press target
    // The #907 pointer-events-none is reverted: it pushed the press back to the
    // button and re-broke drag for icon tiles.
    expect(html).not.toContain('pointer-events-none');
  });
});

// §7.1k item 1: the shared chokepoint is borderless — the per-session accent border
// (the 34% color-mix borderColor + the `border` class) read as noisy across many
// tiles, so it is gone everywhere ShowPageAvatarTile renders (App Library rows +
// ⌘K results). The accent survives only as the 16% background tint + letter color,
// and the tile keeps its 12px radius (`rounded-lg` = --radius-lg in this theme).
describe('ShowPageAvatarTile — §7.1k borderless accent tile', () => {
  it('renders no border, keeping the accent tint + letter color + 12px radius', () => {
    const html = renderToStaticMarkup(<ShowPageAvatarTile sessionId="ses_border" title="Hello" />);
    // No accent border anywhere: neither the `border` utility class nor a
    // `border-color` inline style (which is how React serializes borderColor).
    expect(html).not.toContain('border');
    // The accent still shows as the background tint + letter color.
    expect(html).toContain('background-color:');
    expect(html).toContain('color:var(');
    // 12px radius preserved (unified with the Dock tile).
    expect(html).toContain('rounded-lg');
    // The letter avatar still renders (no icon version supplied).
    expect(html).toContain('>H</span>');
  });
});
