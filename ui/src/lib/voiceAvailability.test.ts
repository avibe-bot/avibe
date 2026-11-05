import 'fake-indexeddb/auto';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The last known transcription status belongs to the reader it was read for:
// it is remembered per reader across loads, and a different reader or a cleared
// store starts from nothing.

type Voice = typeof import('./voiceAvailability');
type Store = typeof import('./localSnapshots');
let voice: Voice;
let store: Store;

beforeEach(async () => {
  vi.resetModules();
  [voice, store] = await Promise.all([import('./voiceAvailability'), import('./localSnapshots')]);
});

afterEach(async () => {
  store.setLocalSnapshotPrincipal(null);
  await store.clearLocalSnapshots();
});

describe('the last known voice status', () => {
  it('is remembered for the reader it was read for, across loads', async () => {
    store.setLocalSnapshotPrincipal('local');
    voice.beginVoiceStatusRead().remember({ available: true, maxFileBytes: 100 });
    expect(voice.knownVoiceStatus()).toEqual({ available: true, maxFileBytes: 100 });
    await new Promise((resolve) => setTimeout(resolve, 20));

    // A new document for the same reader reads it back.
    vi.resetModules();
    const [nextVoice, nextStore] = await Promise.all([import('./voiceAvailability'), import('./localSnapshots')]);
    expect(nextVoice.knownVoiceStatus()).toBeNull();
    nextStore.setLocalSnapshotPrincipal('local');
    await nextVoice.loadKnownVoiceStatus();
    expect(nextVoice.knownVoiceStatus()).toEqual({ available: true, maxFileBytes: 100 });
    nextStore.setLocalSnapshotPrincipal(null);
  });

  it('starts from nothing for another reader or after a clear', async () => {
    store.setLocalSnapshotPrincipal('local');
    voice.beginVoiceStatusRead().remember({ available: true, maxFileBytes: null });
    store.setLocalSnapshotPrincipal('remote:bob');
    expect(voice.knownVoiceStatus()).toBeNull();
    await voice.loadKnownVoiceStatus();
    expect(voice.knownVoiceStatus()).toBeNull();

    voice.beginVoiceStatusRead().remember({ available: true, maxFileBytes: null });
    await store.clearLocalSnapshots();
    expect(voice.knownVoiceStatus()).toBeNull();
  });

  it('refuses an answer read for a reader who is no longer the one signed in', () => {
    store.setLocalSnapshotPrincipal('local');
    const read = voice.beginVoiceStatusRead();
    store.setLocalSnapshotPrincipal('remote:bob');
    read.remember({ available: true, maxFileBytes: null });
    expect(voice.knownVoiceStatus()).toBeNull();
  });
});
