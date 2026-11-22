import { describe, expect, it } from 'vitest';

import { showPageWindowSource, showPageWindowStatusAfterRead, showPageWindowTitle } from './showPageWindowState';

describe('showPageWindowStatusAfterRead', () => {
  it('keeps a ready frame through a non-authoritative read failure', () => {
    expect(showPageWindowStatusAfterRead('ready', { status: 500, session: null })).toBe('ready');
    expect(showPageWindowStatusAfterRead('ready', { status: null, session: null })).toBe('ready');
  });

  it('treats not-found and archived sessions as authoritative missing states', () => {
    expect(showPageWindowStatusAfterRead('ready', { status: 404, session: null })).toBe('missing');
    expect(
      showPageWindowStatusAfterRead('loading', {
        status: 200,
        session: { id: 'ses_1', status: 'archived' },
      }),
    ).toBe('missing');
  });

  it('accepts an active session and rejects a failed initial read', () => {
    expect(
      showPageWindowStatusAfterRead('loading', {
        status: 200,
        session: { id: 'ses_1', status: 'active' },
      }),
    ).toBe('ready');
    expect(showPageWindowStatusAfterRead('loading', { status: 500, session: null })).toBe('missing');
  });
});

describe('showPageWindowSource', () => {
  it('optimistically frames an active session while its row loads', () => {
    expect(showPageWindowSource('ses_1', 'loading', false)).toBe('/show/ses_1/?vibe-embed=1');
    expect(showPageWindowSource('ses_1', 'ready', false)).toBe('/show/ses_1/?vibe-embed=1');
  });

  it('withdraws the frame and parent controls for missing or archived sessions', () => {
    expect(showPageWindowSource('ses_1', 'missing', false)).toBeNull();
    expect(showPageWindowSource('ses_1', 'ready', true)).toBeNull();
    expect(showPageWindowSource('', 'ready', false)).toBeNull();
  });
});

describe('showPageWindowTitle', () => {
  it('names the window after the Show as last read or renamed', () => {
    expect(showPageWindowTitle('Q4 漏斗看板', 'Launcher name', 'Untitled session')).toBe('Q4 漏斗看板');
    expect(showPageWindowTitle(' Renamed ', undefined, 'Untitled session')).toBe('Renamed');
  });

  it('falls back to the launcher until the Show is known, and to nothing for an entry that named nothing', () => {
    expect(showPageWindowTitle(undefined, 'Launcher name', 'Untitled session')).toBe('Launcher name');
    // A deep link or the /apps/show route names nothing: nothing to set until a read answers.
    expect(showPageWindowTitle(undefined, undefined, 'Untitled session')).toBeNull();
    expect(showPageWindowTitle(undefined, '  ', 'Untitled session')).toBeNull();
  });

  it('reads a known Show without a title as untitled', () => {
    expect(showPageWindowTitle(null, 'Launcher name', 'Untitled session')).toBe('Untitled session');
    expect(showPageWindowTitle('  ', undefined, 'Untitled session')).toBe('Untitled session');
  });
});
