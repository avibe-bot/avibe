/** @vitest-environment jsdom */

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import i18n from '../i18n';
import { setLiveStreamDirect } from '../lib/liveStreamVia';
import { DirectTag } from './DirectTag';

// ui/design: mobile-app/direct-tag
afterEach(() => {
  cleanup();
  act(() => setLiveStreamDirect(false));
});

describe('DirectTag', () => {
  it('shows nothing unless the live stream came through the direct ingress', async () => {
    await i18n.changeLanguage('zh');
    const { container } = render(<DirectTag />);
    expect(container.innerHTML).toBe('');
    act(() => setLiveStreamDirect(true));
    expect(screen.getByText('直连')).toBeTruthy();
    act(() => setLiveStreamDirect(false));
    expect(container.innerHTML).toBe('');
  });
});
