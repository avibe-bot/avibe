import { describe, expect, it, vi } from 'vitest';
import type { Location } from 'react-router-dom';

import {
  closeSettingsOverlay,
  settingsOverlayHistoryDelta,
  type SettingsOverlayOrigin,
} from './settingsOverlay';

const origin = (historyIndex: number | null = 2): SettingsOverlayOrigin => ({
  historyIndex,
  location: {
    pathname: '/chat/ses_1',
    search: '?message=m1',
    hash: '#tail',
    state: { source: 'search' },
    key: 'chat-origin',
  } satisfies Location,
});

describe('Settings overlay history', () => {
  it('unwinds every entry added after the opening route', () => {
    expect(settingsOverlayHistoryDelta(origin(), { idx: 5 })).toBe(-3);
    expect(settingsOverlayHistoryDelta(origin(), { idx: 2 })).toBeNull();
    expect(settingsOverlayHistoryDelta(origin(null), { idx: 5 })).toBeNull();

    const navigate = vi.fn();
    closeSettingsOverlay(navigate, origin(), { idx: 5 });
    expect(navigate).toHaveBeenCalledWith(-3);
  });

  it('falls back to replacing the exact origin when no history index exists', () => {
    const navigate = vi.fn();
    closeSettingsOverlay(navigate, origin(null), null);

    expect(navigate).toHaveBeenCalledWith('/chat/ses_1?message=m1#tail', {
      replace: true,
      state: { source: 'search' },
    });
  });
});
