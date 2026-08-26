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
