/* @vitest-environment jsdom */

// The lightbox pages through the session's gallery. One caller — the queue strip
// — shows files that are NOT in that gallery and must not start paging through
// it when they arrive there, which happens on every flush. So "on its own" is a
// property of the open call, and these tests are what make it one.

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import { ImageViewerProvider } from './image-viewer';
import { useImageViewer, type ImageViewerOpenOptions } from './image-viewer-context';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const GALLERY = ['/api/media/med_1', '/api/media/med_2', '/api/media/med_3'];

const Opener: React.FC<{ src: string; options?: ImageViewerOpenOptions }> = ({ src, options }) => {
  const viewer = useImageViewer();
  return (
    <button type="button" onClick={() => viewer?.open(src, options)}>
      open
    </button>
  );
};

const mount = (options?: ImageViewerOpenOptions, images: string[] = GALLERY) =>
  render(
    <I18nextProvider i18n={i18n}>
      <ImageViewerProvider images={images}>
        <Opener src={GALLERY[0]} options={options} />
      </ImageViewerProvider>
    </I18nextProvider>,
  );

const paging = () => ({
  next: screen.queryByLabelText('Next'),
  previous: screen.queryByLabelText('Previous'),
  counter: screen.queryByText('1 / 3'),
});

// The zoom stage observes its own box; jsdom has no layout to report.
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('ImageViewerProvider — paging belongs to the gallery, not to every image', () => {
  it('pages through the gallery for an ordinary chat image', () => {
    mount();
    fireEvent.click(screen.getByText('open'));

    expect(paging().next).not.toBeNull();
    expect(paging().previous).not.toBeNull();
    expect(paging().counter).not.toBeNull();
  });

  // The same src, in the same gallery: only the open call differs.
  it('shows an isolated image on its own even when it is in the gallery', () => {
    mount({ isolated: true });
    fireEvent.click(screen.getByText('open'));

    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(paging().next).toBeNull();
    expect(paging().previous).toBeNull();
    expect(paging().counter).toBeNull();
  });

  // What actually happens to a queued file: the queue flushes while the viewer
  // is open and the transcript — hence the gallery — grows to include it. An
  // isolated view must not acquire controls it was opened without.
  it('stays on its own when the gallery grows underneath it', () => {
    const view = mount({ isolated: true }, []);
    fireEvent.click(screen.getByText('open'));

    view.rerender(
      <I18nextProvider i18n={i18n}>
        <ImageViewerProvider images={GALLERY}>
          <Opener src={GALLERY[0]} options={{ isolated: true }} />
        </ImageViewerProvider>
      </I18nextProvider>,
    );

    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(paging().next).toBeNull();
    expect(paging().counter).toBeNull();
  });

  // Arrow keys are the same faculty as the buttons, so they answer to the same
  // rule rather than to a second one that could drift from it.
  it('ignores the arrow keys for an isolated image', () => {
    mount({ isolated: true });
    fireEvent.click(screen.getByText('open'));

    fireEvent.keyDown(window, { key: 'ArrowRight' });

    expect(document.querySelector('img[src="/api/media/med_1"]')).not.toBeNull();
    expect(document.querySelector('img[src="/api/media/med_2"]')).toBeNull();
  });

  it('closes on Escape either way', () => {
    mount({ isolated: true });
    fireEvent.click(screen.getByText('open'));
    fireEvent.keyDown(window, { key: 'Escape' });

    expect(screen.queryByRole('dialog')).toBeNull();
  });
});
