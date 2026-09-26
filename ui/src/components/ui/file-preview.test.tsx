/* @vitest-environment jsdom */

import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  EDITOR_FONT_DEFAULT,
  adjustEditorFontSize,
  resetEditorFontSize,
} from '@/lib/editorFontSize';
import { FilePreview } from './file-preview';

beforeEach(resetEditorFontSize);

afterEach(() => {
  cleanup();
  resetEditorFontSize();
});

describe('FilePreview media', () => {
  it.each([
    ['voice.wav', 'audio'],
    ['clip.mp4', 'video'],
  ])('plays %s with a native <%s> pointed at the content URL', (name, tag) => {
    const { container } = render(<FilePreview source={{ name, url: '/api/media/tok' }} />);
    const el = container.querySelector<HTMLMediaElement>(tag);
    expect(el?.getAttribute('src')).toBe('/api/media/tok');
    expect(el?.hasAttribute('controls')).toBe(true);
  });

  it('falls back to the failure message when the browser cannot load the media', () => {
    const { container } = render(<FilePreview source={{ name: 'voice.wav', url: '/api/media/tok' }} />);
    act(() => {
      container.querySelector('audio')?.dispatchEvent(new Event('error'));
    });
    expect(container.querySelector('audio')).toBeNull();
    expect(container.textContent).toContain('preview.failed');
  });
});

describe('FilePreview font size', () => {
  it('tracks the Editor font-size preference for Markdown previews', () => {
    const { container } = render(
      <FilePreview source={{ name: 'notes.md', text: '# Preview' }} />,
    );
    const preview = container.querySelector<HTMLElement>('.vr-fileview-text');

    expect(preview?.style.getPropertyValue('--vr-fileview-font-size')).toBe(`${EDITOR_FONT_DEFAULT}px`);

    act(() => adjustEditorFontSize(3));

    expect(preview?.style.getPropertyValue('--vr-fileview-font-size')).toBe(`${EDITOR_FONT_DEFAULT + 3}px`);
  });
});
