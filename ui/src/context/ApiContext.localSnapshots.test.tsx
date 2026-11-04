/* @vitest-environment jsdom */
import 'fake-indexeddb/auto';

import { useEffect } from 'react';
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());

vi.mock('../lib/apiFetch', () => ({ apiFetch }));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import { ApiProvider, useApi } from './ApiContext';
import {
  clearLocalSnapshots,
  readLocalSnapshot,
  setLocalSnapshotPrincipal,
  writeLocalSnapshot,
} from '../lib/localSnapshots';

// Sign-out is the moment a reader's cached Inbox must leave the device. The
// Inbox writes its snapshot on a timer and when the page is hidden, so a write
// can land while the logout request is still out; it must not put the
// snapshot back.

afterEach(async () => {
  cleanup();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('signing out', () => {
  it('removes the reader\'s snapshots and keeps a write scheduled before it from restoring them', async () => {
    let api: ReturnType<typeof useApi> | null = null;
    const Capture = () => {
      const value = useApi();
      useEffect(() => { api = value; }, [value]);
      return null;
    };
    apiFetch.mockImplementation(async (path: string) => (
      path === '/auth/logout' ? new Promise<Response>(() => {}) : Response.json({})
    ));
    render(<ApiProvider><Capture /></ApiProvider>);
    await waitFor(() => expect(api).not.toBeNull());

    setLocalSnapshotPrincipal('remote:alice');
    await writeLocalSnapshot('inbox:first-page', 1, ['alice row']);

    void api!.signOut();
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/auth/logout', expect.anything()));
    // The Inbox's debounce fires while the logout request is still out.
    await writeLocalSnapshot('inbox:first-page', 1, ['alice row']);

    setLocalSnapshotPrincipal('remote:alice');
    expect(await readLocalSnapshot('inbox:first-page', 1, (value) => value)).toBeNull();
  });

  it('removes the reader\'s snapshots when /api/session reports the reader revoked', async () => {
    let api: ReturnType<typeof useApi> | null = null;
    const Capture = () => {
      const value = useApi();
      useEffect(() => { api = value; }, [value]);
      return null;
    };
    apiFetch.mockImplementation(async (path: string) => (
      path === '/api/session'
        ? Response.json({ remote: true, authenticated: true, email: 'a@example.com', authorization_state: 'revoked' })
        : Response.json({})
    ));
    render(<ApiProvider><Capture /></ApiProvider>);
    await waitFor(() => expect(api).not.toBeNull());

    setLocalSnapshotPrincipal('remote:alice');
    await writeLocalSnapshot('inbox:first-page', 1, ['alice row']);
    await api!.getAuthSession();
    await waitFor(async () => {
      setLocalSnapshotPrincipal('remote:alice');
      expect(await readLocalSnapshot('inbox:first-page', 1, (value) => value)).toBeNull();
    });
  });
});
