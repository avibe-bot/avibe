/* @vitest-environment jsdom */

// The lightbox pages through the session's gallery. One caller — the queue strip
// — shows files that are NOT in that gallery and must not start paging through
// it when they arrive there, which happens on every flush. So "on its own" is a
// property of the open call, and these tests are what make it one.

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RouteSurfaceActivityBoundary } from '../RouteSurfaceActivityBoundary';
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

const transformScale = (element: Element | null): number => {
  if (!(element instanceof HTMLElement)) return 1;
  const inline = element.getAttribute('style') ?? '';
  const inlineScale = inline.match(/scale\(([-+]?\d*\.?\d+)\)/)?.[1];
  if (inlineScale) return Number(inlineScale);
  const transform = window.getComputedStyle(element).transform;
  const matrix = transform.match(/^matrix\(([^)]+)\)$/)?.[1]?.split(',').map(Number);
  return matrix && matrix.length >= 2 ? Math.hypot(matrix[0], matrix[1]) : 1;
};

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

// The other caller outside the transcript: the composer stages images that are
// not in the session gallery yet, and previewing one before sending must page
// through those and only those.
describe('ImageViewerProvider — a caller-named gallery is the one it pages', () => {
  const STAGED = ['/api/media/staged_1', '/api/media/staged_2'];

  const mountStaged = (images: string[] = GALLERY) =>
    render(
      <I18nextProvider i18n={i18n}>
        <ImageViewerProvider images={images}>
          <Opener src={STAGED[0]} options={{ gallery: STAGED }} />
        </ImageViewerProvider>
      </I18nextProvider>,
    );

  const shown = () => document.querySelector('[role="dialog"] img')?.getAttribute('src');

  it('pages within the named set instead of the session gallery', () => {
    mountStaged();
    fireEvent.click(screen.getByText('open'));

    expect(screen.getByText('1 / 2')).toBeTruthy();
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    expect(shown()).toBe(STAGED[1]);
    // Wrapping stays in the named set rather than continuing into the gallery.
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    expect(shown()).toBe(STAGED[0]);
  });

  it('keeps its set when the session gallery changes underneath it', () => {
    const view = mountStaged([]);
    fireEvent.click(screen.getByText('open'));

    view.rerender(
      <I18nextProvider i18n={i18n}>
        <ImageViewerProvider images={GALLERY}>
          <Opener src={STAGED[0]} options={{ gallery: STAGED }} />
        </ImageViewerProvider>
      </I18nextProvider>,
    );

    expect(screen.getByText('1 / 2')).toBeTruthy();
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    expect(shown()).toBe(STAGED[1]);
  });

  it('shows a single staged image on its own', () => {
    render(
      <I18nextProvider i18n={i18n}>
        <ImageViewerProvider images={GALLERY}>
          <Opener src={STAGED[0]} options={{ gallery: [STAGED[0]] }} />
        </ImageViewerProvider>
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByText('open'));

    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(screen.queryByLabelText('Next')).toBeNull();
    expect(screen.queryByLabelText('Previous')).toBeNull();
  });
});

describe('ImageViewerProvider — retained route activity owns global keys', () => {
  const retained = (active: boolean, options?: ImageViewerOpenOptions) => (
    <I18nextProvider i18n={i18n}>
      <MemoryRouter>
        <RouteSurfaceActivityBoundary active={active}>
          <div hidden={!active} inert={!active || undefined} aria-hidden={!active || undefined}>
            <ImageViewerProvider images={GALLERY}>
              <Opener src={GALLERY[0]} options={options} />
            </ImageViewerProvider>
          </div>
        </RouteSurfaceActivityBoundary>
      </MemoryRouter>
    </I18nextProvider>
  );

  it.each(['Escape', 'ArrowLeft', 'ArrowRight'] as const)(
    'passes hidden %s to the foreground while retaining the open image',
    (key) => {
    const view = render(retained(true));
    fireEvent.click(screen.getByText('open'));
    const image = document.querySelector('img');
    expect(image?.getAttribute('src')).toBe(GALLERY[0]);

    view.rerender(retained(false));
    const foreground = vi.fn();
    window.addEventListener('keydown', foreground);
    try {
      fireEvent.keyDown(window, { key });
      expect(foreground).toHaveBeenCalledTimes(1);
      expect(document.querySelector('img')).toBe(image);
      expect(document.querySelector('img')?.getAttribute('src')).toBe(GALLERY[0]);
      expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    } finally {
      window.removeEventListener('keydown', foreground);
    }
    },
  );

  it('restores foreground capture after repeated suspension without replacing the viewer instance', () => {
    const view = render(retained(true));
    fireEvent.click(screen.getByText('open'));
    const image = document.querySelector('img');
    expect(image).not.toBeNull();

    for (let cycle = 0; cycle < 2; cycle += 1) {
      view.rerender(retained(false));
      view.rerender(retained(true));
    }
    expect(document.querySelector('img')).toBe(image);
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    expect(document.querySelector('img')?.getAttribute('src')).toBe(GALLERY[1]);

    const recordingAbort = vi.fn();
    window.addEventListener('keydown', recordingAbort);
    try {
      fireEvent.keyDown(window, { key: 'Escape' });
      expect(recordingAbort).not.toHaveBeenCalled();
      expect(screen.queryByRole('dialog')).toBeNull();
    } finally {
      window.removeEventListener('keydown', recordingAbort);
    }
  });

  it('retains the zoom transform across suspension and resume', async () => {
    const view = render(retained(true));
    fireEvent.click(screen.getByText('open'));
    const image = document.querySelector('img');
    const transform = document.querySelector('.react-transform-component');
    expect(image).not.toBeNull();
    expect(transform).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    await waitFor(() => expect(transformScale(transform)).toBeGreaterThan(1));
    const zoomedScale = transformScale(transform);

    view.rerender(retained(false));
    view.rerender(retained(true));
    expect(document.querySelector('img')).toBe(image);
    expect(transformScale(document.querySelector('.react-transform-component'))).toBeCloseTo(zoomedScale);
  });

  it('keeps isolated image semantics after returning to the foreground', () => {
    const view = render(retained(true, { isolated: true }));
    fireEvent.click(screen.getByText('open'));
    view.rerender(retained(false, { isolated: true }));
    view.rerender(retained(true, { isolated: true }));

    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(screen.queryByLabelText('Next')).toBeNull();
    expect(screen.queryByLabelText('Previous')).toBeNull();
    expect(document.querySelector('img')?.getAttribute('src')).toBe(GALLERY[0]);
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    expect(document.querySelector('img')?.getAttribute('src')).toBe(GALLERY[0]);
  });
});
