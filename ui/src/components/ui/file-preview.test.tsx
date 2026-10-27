/* @vitest-environment jsdom */

import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  EDITOR_FONT_DEFAULT,
  adjustEditorFontSize,
  resetEditorFontSize,
} from '@/lib/editorFontSize';
import { FilePreview } from './file-preview';

// The page reader pulls in pdf.js; these tests only decide which reader a PDF gets.
vi.mock('@/components/ui/pdf-pages', () => ({
  default: ({ url }: { url: string }) => <div data-testid="pdf-pages" data-url={url} />,
}));

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

describe('FilePreview PDF', () => {
  const setViewer = (value: boolean | undefined) =>
    Object.defineProperty(window.navigator, 'pdfViewerEnabled', { configurable: true, get: () => value });

  afterEach(() => {
    Reflect.deleteProperty(window.navigator, 'pdfViewerEnabled');
  });

  it('frames the browser viewer where it is a real reader', () => {
    setViewer(true);
    const { container } = render(<FilePreview source={{ name: 'report.pdf', url: '/api/media/tok' }} />);
    expect(container.querySelector('iframe')?.getAttribute('src')).toBe('/api/media/tok');
  });

  it('draws the pages itself where the browser has no viewer', async () => {
    setViewer(false);
    const { container, findByTestId } = render(<FilePreview source={{ name: 'report.pdf', url: '/api/media/tok' }} />);
    expect((await findByTestId('pdf-pages')).getAttribute('data-url')).toBe('/api/media/tok');
    expect(container.querySelector('iframe')).toBeNull();
  });
});
