// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { afterEach, describe, expect, it } from 'vitest';

import type { WorkbenchSession } from '../context/ApiContext';
import {
  clearLocalSnapshots,
  readLocalSnapshot,
  readLocalSnapshotsWithPrefix,
  setLocalSnapshotPrincipal,
  writeLocalSnapshot,
} from './localSnapshots';
import {
  cachedProjectSessions,
  cachedSessionProjects,
  forgetProjectSessions,
  hydrateProjectSessionSnapshots,
  rememberProjectSessions,
} from './projectSessionSnapshots';

const row = (id: string) => ({ id, title: id, agent_status: 'idle', last_active_at: '2026-11-01T00:00:00Z' }) as WorkbenchSession;
const page = (id: string) => ({ sessions: [row(id)], next_before_id: null });
const stored = (projectId: string) => readLocalSnapshot(`projects:sessions:${projectId}`, 1, (value) => value);
const flush = () => new Promise((resolve) => setTimeout(resolve, 50));

afterEach(async () => {
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('project session snapshots', () => {
  it('keep the 40 most recently written projects, evicting the oldest first', async () => {
    setLocalSnapshotPrincipal('local');
    for (let index = 0; index < 41; index += 1) {
      rememberProjectSessions(`p${index}`, page(`s${index}`));
      await flush();
    }
    // At the cap the eviction goes 8 below it, so the oldest 9 are gone.
    expect(await stored('p0')).toBeNull();
    expect(await stored('p8')).toBeNull();
    expect(await stored('p9')).not.toBeNull();
    expect(await stored('p40')).not.toBeNull();

    // Writing the evicted projects again (memory still had them) keeps the cap.
    for (let index = 0; index < 9; index += 1) {
      rememberProjectSessions(`p${index}`, page(`s${index}b`));
      await flush();
    }
    const kept = await readLocalSnapshotsWithPrefix('projects:sessions:', 1, (value) => value);
    expect(kept.size).toBeGreaterThan(0);
    expect(kept.size).toBeLessThanOrEqual(40);
    expect(cachedSessionProjects().length).toBeLessThanOrEqual(40);
  });

  it('load stored pages for the reader, without putting back one forgotten while they load', async () => {
    setLocalSnapshotPrincipal('local');
    await writeLocalSnapshot('projects:sessions:kept', 1, page('k1'));
    await writeLocalSnapshot('projects:sessions:gone', 1, page('g1'));
    setLocalSnapshotPrincipal(null);
    setLocalSnapshotPrincipal('local');
    expect(cachedProjectSessions('kept')).toBeNull();

    const loading = hydrateProjectSessionSnapshots();
    forgetProjectSessions('gone');
    await loading;
    expect(cachedProjectSessions('kept')?.sessions.map((each) => each.id)).toEqual(['k1']);
    expect(cachedProjectSessions('gone')).toBeNull();

    // Another reader starts with nothing in memory.
    setLocalSnapshotPrincipal('remote:bob');
    expect(cachedProjectSessions('kept')).toBeNull();
  });
});
