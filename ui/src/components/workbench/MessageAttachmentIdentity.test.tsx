/* @vitest-environment jsdom */

import { cleanup, fireEvent, render as mount, screen } from '@testing-library/react';
import { createInstance } from 'i18next';
import type { ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

// ``ChatPage`` reaches the composer's mention editor at import time, which reads
// ``matchMedia`` on the module's first evaluation — before any test body runs.
vi.hoisted(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: false }),
  });
});

import en from '../../i18n/en.json';
import type { WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import { ToastProvider } from '../../context/ToastProvider';
import { FileViewerContext } from '../ui/file-viewer-context';
import { ImageViewerContext } from '../ui/image-viewer-context';
import { MessageRow, QueueRow } from './ChatPage';

// One message, two surfaces. A queued message and the delivered message it turns
// into carry the SAME ``content`` — the delivery projection passes it through
// verbatim — so anything either surface derives on its own is a place the two can
// disagree. They read through one boundary now, and these are the properties that
// boundary is there to hold.

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

afterEach(cleanup);

const render = (ui: ReactElement) =>
  renderToStaticMarkup(
    <I18nextProvider i18n={i18n}>
      <ToastProvider>
        <MemoryRouter>{ui}</MemoryRouter>
      </ToastProvider>
    </I18nextProvider>,
  );

const session = (): WorkbenchSession =>
  ({
    id: 'ses_01J8XK5M8T',
    scope_id: 'scope-1',
    project_id: 'proj-1',
    title: 'Model Hub',
    agent_id: 'agt-1',
    agent_name: 'claude',
    agent_backend: 'claude_code',
    status: 'active',
    agent_status: 'idle',
    created_at: '2026-07-27T04:00:00Z',
    updated_at: '2026-07-27T04:00:00Z',
    metadata: {},
  }) as WorkbenchSession;

// Exactly what ``core/handlers/message_handler`` records for a screenshot and a
// log pasted into an IM channel: a media token and a mimetype, no URL.
const IM_CONTENT = {
  attachments: [
    { token: 'med_im1', name: 'feishu-screenshot.png', mimetype: 'image/png', size: 4096 },
    { token: 'med_im2', name: 'trace.log', mimetype: 'text/plain', size: 12 },
  ],
} as WorkbenchMessage['content'];

const message = (type: 'queued' | 'message'): WorkbenchMessage =>
  ({
    id: 'msg_01J8XK5M8T',
    type,
    author: 'user',
    source: 'user',
    text: '',
    content: IM_CONTENT,
    metadata: {},
    created_at: '2026-07-27T04:04:00Z',
  }) as WorkbenchMessage;

describe('attachment identity survives delivery', () => {
  it('shows the same token-shaped image and file before and after the flush', () => {
    const queued = render(
      <QueueRow item={message('queued')} onRemove={() => undefined} onRecall={() => undefined} />,
    );
    const delivered = render(
      <MessageRow message={message('message')} session={session()} messageFontSize={13} />,
    );

    // The same media object, named the same way on both sides. Before this
    // boundary existed the queue drew a nameless chip and the transcript dropped
    // the row entirely, because both read ``url`` and an IM inbound writes none.
    for (const html of [queued, delivered]) {
      expect(html).toContain('/api/media/med_im1');
      expect(html).toContain('feishu-screenshot.png');
      expect(html).toContain('trace.log');
    }

    // And each is still the right KIND of thing: the image is fetched as an
    // <img>, the log is a click-through, on both surfaces.
    expect(queued).toContain('data-queue-attachment="image"');
    expect(queued).toContain('data-queue-attachment="file"');
    expect(delivered).toContain('<img src="/api/media/med_im1"');
    expect(delivered).not.toContain('<img src="/api/media/med_im2"');
    expect(delivered).toContain('href="/api/media/med_im2"');
  });

  // The queue opens a file rather than linking to it, so its URL is in the click
  // and not in the markup. Same URL, asked for the same way.
  it('opens the queued log at the URL the delivered one links to', () => {
    const openFile = vi.fn();
    mount(
      <I18nextProvider i18n={i18n}>
        <ImageViewerContext.Provider value={{ open: vi.fn() }}>
          <FileViewerContext.Provider value={{ open: openFile }}>
            <QueueRow item={message('queued')} onRemove={() => undefined} onRecall={() => undefined} />
          </FileViewerContext.Provider>
        </ImageViewerContext.Provider>
      </I18nextProvider>,
    );

    fireEvent.click(screen.getByLabelText('trace.log · LOG'));
    expect(openFile).toHaveBeenCalledWith({ url: '/api/media/med_im2', name: 'trace.log' });
  });

  it('keeps the uploader-supplied box on the delivered image', () => {
    // Reserving the box is what stops the transcript jumping when the bytes
    // land, so the dimensions have to cross the boundary with the URL.
    const delivered = render(
      <MessageRow
        message={
          {
            ...message('message'),
            content: {
              attachments: [
                { url: '/api/media/med_1', name: 'shot.png', mime: 'image/png', kind: 'image', width: 800, height: 600 },
              ],
            } as WorkbenchMessage['content'],
          } as WorkbenchMessage
        }
        session={session()}
        messageFontSize={13}
      />,
    );
    expect(delivered).toMatch(/aspect-ratio:\s*800\s*\/\s*600/);
  });

  it('does not inline-render a third-party image on either surface', () => {
    const remote = {
      attachments: [{ url: 'https://files.example.com/a.png?sig=abc', name: 'a.png', mime: 'image/png' }],
    } as WorkbenchMessage['content'];

    const queued = render(
      <QueueRow
        item={{ ...message('queued'), content: remote } as WorkbenchMessage}
        onRemove={() => undefined}
        onRecall={() => undefined}
      />,
    );
    const delivered = render(
      <MessageRow
        message={{ ...message('message'), content: remote } as WorkbenchMessage}
        session={session()}
        messageFontSize={13}
      />,
    );

    // Neither surface may paint an <img> at a host that is not ours: the fetch
    // would happen the moment the row renders, with nobody asking for it.
    for (const html of [queued, delivered]) {
      expect(html).not.toContain('<img src="https://files.example.com');
      expect(html).toContain('a.png');
    }
  });
});
