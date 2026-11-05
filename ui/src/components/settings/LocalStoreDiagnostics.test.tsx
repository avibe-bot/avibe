/* @vitest-environment jsdom */
import 'fake-indexeddb/auto';

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/lib/platform', () => ({ isStandalonePwa: () => false, isIosDevice: () => false }));

import type { WorkbenchMessage } from '@/context/ApiContext';
import { clearLocalSnapshots, setLocalSnapshotPrincipal } from '@/lib/localSnapshots';
import { beginLocalStoreRead, recordLocalStorePage } from '@/lib/localStore';
import { LocalStoreDiagnostics } from './LocalStoreDiagnostics';
import { offlineHistoryPersistence } from './offlineHistoryPersistence';

// The local message store's line on the diagnostics page (docs/plans/
// local-sync-store.md, Phase 1): whether the browser keeps it, and its size.

afterEach(async () => {
  cleanup();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('offline history diagnostics', () => {
  it('names what the browser answered, and a browser tab as never asked', () => {
    expect([
      offlineHistoryPersistence(true, true),
      offlineHistoryPersistence(false, true),
      offlineHistoryPersistence(null, true),
      offlineHistoryPersistence(null, false),
    ]).toEqual(['granted', 'denied', 'unknown', 'not-requested']);
  });

  it("shows a browser tab's store and its size once its reader is named, even after the page opened", async () => {
    setLocalSnapshotPrincipal('remote:alice');
    const row = { id: 'msg_1', session_id: 'ses_a', type: 'result', author: 'agent', text: 'x'.repeat(4000), created_at: '2026-11-05T00:00:01.000000Z', updated_at: '2026-11-05T00:00:01.000000Z' } as WorkbenchMessage;
    await recordLocalStorePage(beginLocalStoreRead('ses_a'), { kind: 'tail' }, { messages: [row], next_before_id: null });
    // A page loaded straight here: the reader is named only after it opened.
    setLocalSnapshotPrincipal(null);
    const { container } = render(<LocalStoreDiagnostics />);
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(container.textContent).toBe('');

    act(() => setLocalSnapshotPrincipal('remote:alice'));
    expect(await screen.findByText('settings.offlineHistory.persistenceNotRequested')).toBeTruthy();
    expect(screen.getByText(/^4\.\d KB$/)).toBeTruthy();
  });
});
