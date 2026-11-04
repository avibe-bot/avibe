// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { afterEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchSession } from '../context/ApiContext';

// A conversation is read back only by the UI build that stored it: the page's
// entry module, whose content-hashed name changes with every build.

type ChatSnapshotsModule = typeof import('./chatSnapshots');
type LocalSnapshotsModule = typeof import('./localSnapshots');

const loadBuild = async (entry: string): Promise<[ChatSnapshotsModule, LocalSnapshotsModule]> => {
  document.head.innerHTML = `<script type="module" crossorigin src="${entry}"></script>`;
  vi.resetModules();
  return Promise.all([import('./chatSnapshots'), import('./localSnapshots')]);
};

afterEach(() => {
  document.head.innerHTML = '';
});

describe('the UI build of a conversation snapshot', () => {
  it('is the page entry module: the next build ignores what the last one stored', async () => {
    const [before, beforeStore] = await loadBuild('/assets/index-BUILD1.js');
    beforeStore.setLocalSnapshotPrincipal('local');
    await before.writeChatSnapshot(before.chatSnapshotOwner('ses_a')!, {
      session: { id: 'ses_a', title: 'stored by build 1' } as WorkbenchSession,
      messages: [],
      next_before_id: null,
      agents: [],
      default_agent_name: null,
      can_chat: true,
    });
    expect((await before.readChatSnapshot('ses_a'))?.session.title).toBe('stored by build 1');

    const [sameBuild, sameStore] = await loadBuild('/assets/index-BUILD1.js');
    sameStore.setLocalSnapshotPrincipal('local');
    expect((await sameBuild.readChatSnapshot('ses_a'))?.session.title).toBe('stored by build 1');

    const [after, afterStore] = await loadBuild('/assets/index-BUILD2.js');
    afterStore.setLocalSnapshotPrincipal('local');
    expect(await after.readChatSnapshot('ses_a')).toBeNull();
    await afterStore.clearLocalSnapshots();
  });
});
