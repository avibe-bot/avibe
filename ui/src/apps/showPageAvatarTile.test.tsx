/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, waitFor } from '@testing-library/react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ShowPageAvatarContent, ShowPageAvatarTile } from './showPageAvatarTile';

// jsdom has no object URLs; mint distinct ones so a test can tell them apart.
let minted = 0;
const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  minted = 0;
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  URL.createObjectURL = vi.fn(() => `blob:icon-${++minted}`);
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const iconResponse = () => new Response('<svg/>', { headers: { 'Content-Type': 'image/svg+xml' } });

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

  it('fills the tile while the icon downloads and once it shows as <img draggable="false">', async () => {
    fetchMock.mockResolvedValueOnce(iconResponse());
    const { container } = render(
      <ShowPageAvatarContent iconUrl="/api/show-pages/ses_drag/icon?v=abc" letter="S" />,
    );
    const placeholder = container.firstElementChild!;
    expect(placeholder.tagName).toBe('SPAN');
    expect(placeholder.className).toContain('size-full');
    expect(placeholder.className).not.toContain('pointer-events-none');

    await waitFor(() => expect(container.querySelector('img')).not.toBeNull());
    const img = container.querySelector('img')!;
    expect(img.getAttribute('draggable')).toBe('false'); // native image DnD stays disabled (#906)
    expect(img.className).toContain('size-full'); // the img is the filling press target
    // The #907 pointer-events-none is reverted: it pushed the press back to the
    // button and re-broke drag for icon tiles.
    expect(img.className).not.toContain('pointer-events-none');
  });
});

describe('ShowPageAvatarContent — icon loading', () => {
  // Remote icons are served `no-store`, so the browser re-downloads one for every
  // freshly mounted <img>; the hover Dock remounts its tiles on every reveal.
  it('paints a remounted icon on its first frame without downloading it again', async () => {
    const url = '/api/show-pages/ses_remount/icon?v=abc';
    fetchMock.mockResolvedValueOnce(iconResponse());
    const first = render(<ShowPageAvatarContent iconUrl={url} letter="S" />);
    await waitFor(() => expect(first.container.querySelector('img')).not.toBeNull());
    first.unmount();

    const second = render(<ShowPageAvatarContent iconUrl={url} letter="S" />);
    expect(second.container.querySelector('img')?.getAttribute('src')).toBe('blob:icon-1');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('falls back to the letter after bounded retries of a failing icon', async () => {
    const url = '/api/show-pages/ses_missing/icon?v=abc';
    fetchMock.mockResolvedValue(new Response('', { status: 404 }));
    const { container } = render(<ShowPageAvatarContent iconUrl={url} letter="S" />);
    await waitFor(() => expect(container.textContent).toBe('S'));
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('downloads again when the held bytes cannot be decoded', async () => {
    const url = '/api/show-pages/ses_corrupt/icon?v=abc';
    fetchMock.mockImplementation(async () => iconResponse());
    const { container } = render(<ShowPageAvatarContent iconUrl={url} letter="S" />);
    await waitFor(() => expect(container.querySelector('img')?.getAttribute('src')).toBe('blob:icon-1'));

    fireEvent.error(container.querySelector('img')!);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:icon-1');
    await waitFor(() => expect(container.querySelector('img')?.getAttribute('src')).toBe('blob:icon-2'));
    expect(fetchMock).toHaveBeenCalledTimes(2);
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
