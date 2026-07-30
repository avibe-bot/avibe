import { describe, expect, it } from 'vitest';

import { showPageWindowSource } from './showPageWindowState';

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
