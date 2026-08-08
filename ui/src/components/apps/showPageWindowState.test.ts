import { describe, expect, it } from 'vitest';

import { showPageWindowSource, showPageWindowStatusAfterRead } from './showPageWindowState';

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
