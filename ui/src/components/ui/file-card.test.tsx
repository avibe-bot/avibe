/* @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { FileCard } from './file-card';

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
