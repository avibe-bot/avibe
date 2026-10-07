// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { getBackendUiMeta } from '@/lib/agentBackends';
import { backendVisual } from '../settings/models/vendorMeta';
import { BACKEND_BRAND_MARKS } from './backendBrandMarks';
import { BackendAvatar, BackendIcon } from './BackendIcon';

const markPath = (container: HTMLElement) => container.querySelector('path')?.getAttribute('d');

// Vibey's face is one entry in the backend visuals table: every surface that
// draws a backend icon or an Agent avatar takes it from there.
describe('Vibey mark', () => {
  afterEach(cleanup);

  it('is the built-in backend icon, avatar and brand mark', () => {
    const vibey = BACKEND_BRAND_MARKS.avibe.path;
    const { Icon, Avatar } = getBackendUiMeta('avibe');
    expect(markPath(render(<Icon size={16} />).container)).toBe(vibey);
    expect(markPath(render(<Avatar size={16} />).container)).toBe(vibey);
    expect(markPath(render(<BackendAvatar backend="avibe" />).container)).toBe(vibey);
    expect(markPath(render(<BackendIcon backend="avibe" variant="brand" size={28} />).container)).toBe(vibey);
    // The Models route card reads the same table.
    expect(backendVisual('avibe').Icon).toBe(Icon);
  });

  it('follows the text color so it reads on dark and light themes', () => {
    const { container } = render(<BackendAvatar backend="avibe" className="size-3.5" />);
    const svg = container.querySelector('svg')!;
    expect(svg.getAttribute('class')).toBe('size-3.5');
    expect(container.querySelector('path')!.getAttribute('fill')).toBe('currentColor');
  });

  it('leaves native Agents on the generic Agent glyph', () => {
    for (const backend of ['claude', 'codex', 'opencode']) {
      const { container, unmount } = render(<BackendAvatar backend={backend} />);
      expect(container.querySelector('svg')!.getAttribute('class')).toContain('lucide-bot');
      unmount();
    }
  });
});
