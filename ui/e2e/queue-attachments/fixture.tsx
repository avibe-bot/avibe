import { useState } from 'react';
import { createRoot } from 'react-dom/client';

import { QueueStrip } from '../../src/components/workbench/ChatPage';
import { FileViewerProvider } from '../../src/components/ui/file-viewer';
import { ImageViewerProvider } from '../../src/components/ui/image-viewer';
import type { WorkbenchMessage } from '../../src/context/ApiContext';
import { ThemeProvider } from '../../src/context/ThemeProvider';
import '../../src/i18n';
import '../../src/index.css';

// The real queue strip with real attachment data, in a column the width of the
// chat one. Nothing here reaches an Avibe service: every `/api/media/...` request
// is answered by the spec's own route handler, which is also how a broken image
// is produced without inventing a second code path for failure.

const noop = () => undefined;

const media = (id: string, name: string, over: Record<string, unknown> = {}) => ({
  url: `/api/media/${id}`,
  name,
  kind: 'image',
  mime: 'image/png',
  ...over,
});

const queued = (id: string, text: string, attachments?: unknown[]): WorkbenchMessage =>
  ({
    id,
    type: 'queued',
    author: 'user',
    source: 'user',
    text,
    content: attachments ? { attachments } : {},
    metadata: {},
    created_at: '2026-09-19T00:00:00Z',
  }) as WorkbenchMessage;

const FIVE = [
  media('med_1', 'one.png'),
  media('med_2', 'two.png'),
  media('med_3', 'three.png'),
  { url: '/api/media/med_4', name: 'release-notes-2026-09.pdf', mime: 'application/pdf' },
  { url: '/api/media/med_5', name: 'console-log.txt', mime: 'text/plain' },
];

const QUEUE: WorkbenchMessage[] = [
  queued('q-text', 'A queued message with no files at all'),
  queued('q-image', '', [media('med_1', 'annotation-region.png')]),
  queued('q-mixed', 'Compare these against the spec', FIVE),
  queued('q-broken', '', [media('med_broken', 'console-log.png')]),
  queued('q-file', '', [{ url: '/api/media/med_5', name: 'console-log.txt', mime: 'text/plain' }]),
];

// The transcript gallery deliberately CONTAINS the queued image, so the spec can
// show that a queued preview opens on its own because it was asked to — not
// because its URL happened to be absent.
const GALLERY = ['/api/media/med_1', '/api/media/med_2'];

export function Fixture() {
  const [queue, setQueue] = useState(QUEUE);
  const [sent, setSent] = useState(0);
  return (
    // ThemeProvider is what the app puts above the chat, and the file viewer's
    // text renderer reads the theme — so a fixture without it would fail on a
    // file type the product handles perfectly well.
    <ThemeProvider>
      <ImageViewerProvider images={GALLERY}>
        <FileViewerProvider>
          <div className="flex h-dvh flex-col bg-background text-foreground">
            <div className="min-h-0 flex-1 overflow-y-auto p-4 text-[12px] text-muted">
              Transcript stand-in — the queue strip below is the surface under test.
            </div>
            <div data-testid="queue-fixture" data-sent={sent}>
              <QueueStrip
                queue={queue}
                onRemove={(id) => setQueue((rows) => rows.filter((row) => row.id !== id))}
                onRecall={noop}
                onSendNow={() => setSent((n) => n + 1)}
              />
            </div>
            {/* The composer's place in the layout: the strip must never push it
                off-screen, which is the reason the queue body is capped at all. */}
            <div data-testid="composer" className="shrink-0 px-4 py-3 md:px-8">
              <div className="mx-auto h-11 w-full max-w-[1080px] rounded-xl border border-border bg-surface-1" />
            </div>
          </div>
        </FileViewerProvider>
      </ImageViewerProvider>
    </ThemeProvider>
  );
}

createRoot(document.getElementById('root')!).render(<Fixture />);
