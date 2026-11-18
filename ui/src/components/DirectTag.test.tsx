/** @vitest-environment jsdom */

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import i18n from '../i18n';
import { setLiveStreamVia } from '../lib/liveStreamVia';
import { DirectTag } from './DirectTag';

// ui/design: mobile-app/direct-tag
afterEach(() => {
  cleanup();
  act(() => setLiveStreamVia(null));
});

describe('DirectTag', () => {
  it('shows nothing unless the live stream came through the direct ingress', async () => {
    await i18n.changeLanguage('zh');
    const { container } = render(<DirectTag />);
    expect(container.innerHTML).toBe('');
    act(() => setLiveStreamVia('direct'));
    expect(screen.getByText('直连')).toBeTruthy();
    act(() => setLiveStreamVia('other'));
    expect(container.innerHTML).toBe('');
  });
});
