/* @vitest-environment jsdom */
import 'fake-indexeddb/auto';

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchMessage, WorkbenchSession } from '@/context/ApiContext';
import { clearLocalSnapshots, setLocalSnapshotPrincipal } from '@/lib/localSnapshots';
import { beginLocalStoreRead, readLocalStoreMediaMeta, recordLocalStoreMediaMeta, recordLocalStorePage } from '@/lib/localStore';
import { FileCard } from './file-card';
import { MediaSessionContext } from './media-session-context';

const mock = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock('@/lib/apiFetch', async (importOriginal) => ({ ...await importOriginal<typeof import('@/lib/apiFetch')>(), apiFetch: mock.apiFetch }));

function serveMeta(meta: Record<string, unknown>) {
  mock.apiFetch.mockResolvedValue(new Response(JSON.stringify(meta), { status: 200, headers: { 'Content-Type': 'application/json' } }));
}

afterEach(() => {
  cleanup();
  mock.apiFetch.mockReset();
});

describe('FileCard media', () => {
  it('plays an audio attachment inline instead of offering the preview overlay', async () => {
    serveMeta({ name: 'take-2.wav', ext: 'wav', content_type: 'audio/x-wav', size: 2048 });
    const { container } = render(<FileCard href="/api/media/tok">Voice memo</FileCard>);

    await waitFor(() => expect(screen.getByText(/WAV/)).toBeTruthy());
    const audio = container.querySelector('audio');
    expect(audio?.getAttribute('src')).toBe('/api/media/tok');
    expect(audio?.hasAttribute('controls')).toBe(true);
    expect(screen.queryByLabelText('chat.media.preview')).toBeNull();
    expect(screen.getByLabelText('chat.media.download')).toBeTruthy();
  });

  it('never mounts a player on the label alone — only once /meta confirms audio', async () => {
    let resolve!: (r: Response) => void;
    mock.apiFetch.mockReturnValue(new Promise<Response>((r) => { resolve = r; }));
    const { container } = render(<FileCard href="/api/media/tok">report.mp3</FileCard>);

    expect(container.querySelector('audio')).toBeNull();
    resolve(new Response(JSON.stringify({ name: 'report.pdf', ext: 'pdf', content_type: 'application/pdf', size: 1024 }), { status: 200 }));
    await waitFor(() => expect(screen.getByText(/PDF/)).toBeTruthy());
    expect(container.querySelector('audio')).toBeNull();
    expect(screen.getByLabelText('chat.media.preview')).toBeTruthy();
  });

  it('drops the previous attachment\'s audio kind when a reused card switches to a new URL', async () => {
    serveMeta({ name: 'take-2.wav', ext: 'wav', content_type: 'audio/x-wav', size: 2048 });
    const { container, rerender } = render(<FileCard href="/api/media/one">Attachment</FileCard>);
    await waitFor(() => expect(container.querySelector('audio')).toBeTruthy());

    mock.apiFetch.mockReturnValue(new Promise<Response>(() => {}));
    rerender(<FileCard href="/api/media/two">Attachment</FileCard>);

    expect(container.querySelector('audio')).toBeNull();
  });

  it('keeps video behind the preview control rather than autoloading it in the card', async () => {
    serveMeta({ name: 'demo.mp4', ext: 'mp4', content_type: 'video/mp4', size: 4096 });
    const { container } = render(<FileCard href="/api/media/tok">Demo</FileCard>);

    await waitFor(() => expect(screen.getByLabelText('chat.media.preview')).toBeTruthy());
    expect(container.querySelector('audio, video')).toBeNull();
  });

  it('never mounts a player for a non-proxy URL', () => {
    const { container } = render(<FileCard href="https://example.com/a.mp3">a.mp3</FileCard>);

    expect(mock.apiFetch).not.toHaveBeenCalled();
    expect(container.querySelector('audio')).toBeNull();
    expect(screen.getByLabelText('chat.media.preview')).toBeTruthy();
  });
});

describe('FileCard in a conversation the local message store keeps', () => {
  // docs/plans/local-sync-store.md section 4.6: a card's metadata is kept with
  // its conversation (never the file), so it shows offline.
  const url = '/api/media/tok_report';
  const stored = { name: 'report.pdf', ext: 'pdf', content_type: 'application/pdf', size: 2048 };
  const row = { id: 'msg_1', session_id: 'ses_a', type: 'result', author: 'agent', text: 'here', created_at: '2026-11-05T00:00:01.000000Z', updated_at: '2026-11-05T00:00:01.000000Z' } as WorkbenchMessage;
  const session = { id: 'ses_a', title: 'a', status: 'active', last_active_at: '2026-11-05T00:00:01.000000Z' } as WorkbenchSession;
  const inConversation = (href: string) => (
    <MediaSessionContext.Provider value="ses_a"><FileCard href={href}>report.pdf</FileCard></MediaSessionContext.Provider>
  );

  beforeEach(async () => {
    setLocalSnapshotPrincipal('remote:alice');
    await recordLocalStorePage(beginLocalStoreRead('ses_a'), { kind: 'tail' }, { messages: [row], next_before_id: null }, session);
  });
  afterEach(async () => {
    setLocalSnapshotPrincipal(null);
    await clearLocalSnapshots();
  });

  it('shows what it stored for its conversation when /meta cannot be reached', async () => {
    await recordLocalStoreMediaMeta(beginLocalStoreRead('ses_a'), url, stored);
    mock.apiFetch.mockRejectedValue(new TypeError('Failed to fetch'));
    render(inConversation(url));
    await waitFor(() => expect(screen.getByText('PDF · 2.0 KB')).toBeTruthy());
  });

  it("keeps the server's answer for its conversation, and lets it win over what was stored", async () => {
    await recordLocalStoreMediaMeta(beginLocalStoreRead('ses_a'), url, { ...stored, size: 1024 });
    serveMeta(stored);
    render(inConversation(url));
    await waitFor(() => expect(screen.getByText('PDF · 2.0 KB')).toBeTruthy());
    await waitFor(async () => expect(await readLocalStoreMediaMeta('ses_a', url)).toEqual(stored));
    // A stored read that lands after the answer does not take its place.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.getByText('PDF · 2.0 KB')).toBeTruthy();
  });

  it('stores nothing for a card shown outside a conversation', async () => {
    serveMeta(stored);
    render(<FileCard href={url}>report.pdf</FileCard>);
    await waitFor(() => expect(screen.getByText('PDF · 2.0 KB')).toBeTruthy());
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(await readLocalStoreMediaMeta('ses_a', url)).toBeNull();
  });
});
