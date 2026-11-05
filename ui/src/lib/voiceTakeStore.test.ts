// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { afterEach, describe, expect, it, vi } from 'vitest';

import { clearLocalSnapshots, forgetLocalSnapshots, setLocalSnapshotPrincipal } from './localSnapshots';
import { bumpSignOutGeneration, SIGN_OUT_GENERATION_KEY } from './signOutGeneration';
import { VoiceTakeKeeper } from './voiceTakeKeeper';
import { retryKeptVoiceTake } from './voiceTakeRetry';
import {
  deleteVoiceTake,
  heldVoiceTakes,
  isVoiceTakeLive,
  landVoiceTake,
  readVoiceTakeAudio,
  readVoiceTakes,
  saveVoiceTake,
  saveVoiceTakeAudio,
  subscribeVoiceTakes,
  voiceTakeDurationMs,
  type VoiceTake,
} from './voiceTakeStore';

// Recognition, at its module boundary: what a recovery sends, and what comes back.
const finalize = vi.hoisted(() => vi.fn());
vi.mock('./voiceTranscription', async (importOriginal) => ({
  ...await importOriginal<typeof import('./voiceTranscription')>(),
  finalizeVoiceDictation: finalize,
}));
/** The segments a recovery sent, as what they are: sequence, final, and whether audio went. */
const sent = (call = 0) => {
  const [segments] = finalize.mock.calls[call] as [{ sequence: number; final: boolean; blob: Blob | null }[]];
  return segments.map(({ sequence, final, blob }) => ({ sequence, final, blob: blob !== null }));
};

// The store is what keeps a take's speech through a reload, and what keeps one reader's
// recordings away from the next person to sign in on the device.

const idle = () => new Promise((resolve) => setTimeout(resolve, 30));
const wav = (label: string) => new Blob([label], { type: 'audio/wav' });

const take = (id: string, patch: Partial<VoiceTake> = {}): VoiceTake => ({
  id,
  target: 'ses_a',
  intent: 'send',
  createdAt: Date.now(),
  updatedAt: Date.now(),
  status: 'pending',
  segments: [],
  ...patch,
});

/** A take recorded through the keeper: two minutes and a tail, then left without text. */
const keepTake = async (id: string, target = 'ses_a') => {
  const keeper = new VoiceTakeKeeper(id, { target, intent: 'send' });
  keeper.begin();
  keeper.segment({ sequence: 0, final: false, durationMs: 60_000 }, wav('minute one'));
  keeper.segment({ sequence: 1, final: false, durationMs: 60_000, overlapMs: 500 }, wav('minute two'));
  keeper.segment({ sequence: 2, final: true, durationMs: 23_000, overlapMs: 500 }, wav('tail'));
  keeper.pending();
  await keeper.written();
  return keeper;
};

/** The browser's Web Locks for one origin: how its documents see which takes a surface holds. */
const installWebLocks = () => {
  const held = new Map<string, Promise<void>>();
  const locks = {
    async request(name: string, ...args: unknown[]) {
      const [options, callback] = (args.length === 1 ? [{}, args[0]] : args) as [
        { ifAvailable?: boolean; signal?: AbortSignal },
        (lock: unknown) => Promise<unknown>,
      ];
      while (held.has(name)) {
        if (options.ifAvailable) return callback(null);
        await Promise.race([held.get(name), new Promise((_resolve, reject) => {
          options.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        })]);
      }
      let free!: () => void;
      held.set(name, new Promise<void>((resolve) => {
        free = resolve;
      }));
      try {
        return await callback({ name, mode: 'exclusive' });
      } finally {
        held.delete(name);
        free();
      }
    },
    async query() {
      return { held: [...held.keys()].map((name) => ({ name, mode: 'exclusive' })), pending: [] };
    },
  };
  Object.defineProperty(navigator, 'locks', { configurable: true, value: locks });
  return () => Reflect.deleteProperty(navigator, 'locks');
};

afterEach(async () => {
  finalize.mockReset();
  setLocalSnapshotPrincipal('cleanup');
  for (const kept of await readVoiceTakes()) await deleteVoiceTake(kept.id);
  setLocalSnapshotPrincipal(null);
  window.localStorage.clear();
});

describe('voice take store', () => {
  it('keeps a take and its audio, as fresh blobs, until its words land', async () => {
    setLocalSnapshotPrincipal('local');
    const keeper = await keepTake('take-1');

    const [kept] = await readVoiceTakes('ses_a');
    expect(kept).toMatchObject({ id: 'take-1', status: 'pending', intent: 'send' });
    expect(voiceTakeDurationMs(kept!)).toBe(142_000);
    const audio = await readVoiceTakeAudio('take-1');
    expect(await audio.get(0)?.text()).toBe('minute one');
    expect(await audio.get(2)?.text()).toBe('tail');
    expect(isVoiceTakeLive('take-1')).toBe(true);

    keeper.transcribed('everything that was said');
    await keeper.written();
    expect((await readVoiceTakes('ses_a'))[0]).toMatchObject({ status: 'transcribed', text: 'everything that was said' });

    keeper.forget();
    await keeper.written();
    expect(await readVoiceTakes()).toEqual([]);
    expect((await readVoiceTakeAudio('take-1')).size).toBe(0);
    expect(isVoiceTakeLive('take-1')).toBe(false);
  });

  it('returns takes only to the reader who spoke them, and drops them when another reader signs in', async () => {
    setLocalSnapshotPrincipal('remote:alice');
    await keepTake('alice-take');
    expect(await readVoiceTakes()).toHaveLength(1);

    setLocalSnapshotPrincipal('remote:bob');
    await idle();
    expect(await readVoiceTakes()).toEqual([]);
    setLocalSnapshotPrincipal('remote:alice');
    await idle();
    expect(await readVoiceTakes()).toEqual([]);
  });

  it('drops every take on sign-out, and on a sign-out in another tab', async () => {
    setLocalSnapshotPrincipal('local');
    await keepTake('before-sign-out');
    await forgetLocalSnapshots();
    await idle();
    setLocalSnapshotPrincipal('local');
    expect(await readVoiceTakes()).toEqual([]);

    await keepTake('before-other-tab');
    expect(await readVoiceTakes()).toHaveLength(1);
    // Another tab signs out: the shared generation moves on.
    bumpSignOutGeneration();
    expect(await readVoiceTakes()).toEqual([]);
  });

  it('keeps the reader\'s own speech through an authorization change, which clears only server reads', async () => {
    setLocalSnapshotPrincipal('local');
    await keepTake('through-a-change');
    await clearLocalSnapshots();
    await idle();
    expect(await readVoiceTakes()).toHaveLength(1);
  });

  it('lets no take still running here bring back speech another tab just signed out', async () => {
    setLocalSnapshotPrincipal('local');
    const keeper = new VoiceTakeKeeper('running', { target: 'ses_a', intent: 'send' });
    keeper.begin();
    keeper.segment({ sequence: 0, final: false, durationMs: 60_000 }, wav('minute one'));
    await idle();
    expect(await readVoiceTakes()).toHaveLength(1);

    // Another tab signs out: its sign-out generation reaches this one through storage.
    bumpSignOutGeneration();
    window.dispatchEvent(new StorageEvent('storage', { key: SIGN_OUT_GENERATION_KEY }));
    await idle();
    keeper.segment({ sequence: 1, final: true, durationMs: 9_000 }, wav('tail'));
    keeper.transcribed('words after the sign-out');
    await idle();

    // Even once this tab names the same reader again, nothing of it is back.
    setLocalSnapshotPrincipal(null);
    setLocalSnapshotPrincipal('local');
    await idle();
    expect(await readVoiceTakes()).toEqual([]);
  });

  it('tells its readers when the reader becomes known, even with nothing to delete', async () => {
    setLocalSnapshotPrincipal('local');
    await saveVoiceTake(take('mine'));
    setLocalSnapshotPrincipal(null);
    let heard = 0;
    const stop = subscribeVoiceTakes(() => { heard += 1; });
    setLocalSnapshotPrincipal('local');
    await idle();
    stop();
    expect(heard).toBeGreaterThan(0);
    expect(await readVoiceTakes()).toHaveLength(1);
  });

  it('tells other documents of this origin about every change', async () => {
    setLocalSnapshotPrincipal('local');
    const other = new BroadcastChannel('avibe-voice-takes');
    const heard = new Promise((resolve) => { other.onmessage = resolve; });
    await saveVoiceTake(take('from-the-pet'));
    await expect(heard).resolves.toBeTruthy();
    other.close();
  });

  it('keeps at most a few takes, letting the oldest go first', async () => {
    setLocalSnapshotPrincipal('local');
    // Three are kept; a fourth lets the oldest go.
    for (let index = 0; index < 4; index += 1) {
      await saveVoiceTake(take(`take-${index}`, { createdAt: 1_000 + index }));
    }
    expect((await readVoiceTakes()).map((kept) => kept.id)).toEqual(['take-1', 'take-2', 'take-3']);
  });

  it('stores nothing while the reader is unknown', async () => {
    setLocalSnapshotPrincipal(null);
    expect(await saveVoiceTake(take('nobody'))).toBe(false);
    setLocalSnapshotPrincipal('local');
    expect(await readVoiceTakes()).toEqual([]);
  });

  it('transcribes a kept take from its stored audio after a reload, and keeps the text until it lands', async () => {
    setLocalSnapshotPrincipal('local');
    const keeper = await keepTake('reloaded');
    keeper.release();
    const [kept] = await readVoiceTakes('ses_a');
    finalize.mockResolvedValue({ text: 'all of it, recovered', cleanup: 'success' });

    await expect(retryKeptVoiceTake(kept!)).resolves.toBe('all of it, recovered');
    expect(sent()).toEqual([
      { sequence: 0, final: false, blob: true },
      { sequence: 1, final: false, blob: true },
      { sequence: 2, final: true, blob: true },
    ]);
    expect((await readVoiceTakes('ses_a'))[0]).toMatchObject({ status: 'transcribed', text: 'all of it, recovered' });
  });

  it('keeps a kept take pending when it still cannot be transcribed, and does not bring back one discarded meanwhile', async () => {
    setLocalSnapshotPrincipal('local');
    (await keepTake('still-down')).release();
    const [kept] = await readVoiceTakes();
    finalize.mockRejectedValueOnce(new Error('Load failed'));
    await expect(retryKeptVoiceTake(kept!)).rejects.toThrow('Load failed');
    expect((await readVoiceTakes())[0]).toMatchObject({ status: 'pending' });

    let release!: () => void;
    finalize.mockImplementationOnce(() => new Promise((resolve) => {
      release = () => resolve({ text: 'too late', cleanup: 'success' });
    }));
    const running = retryKeptVoiceTake(kept!);
    await idle();
    await deleteVoiceTake(kept!.id);
    release();
    await running;
    expect(await readVoiceTakes()).toEqual([]);
  });

  it('leaves a take a surface still holds to it, in any document, and recovers it once that surface is gone', async () => {
    const uninstall = installWebLocks();
    try {
      setLocalSnapshotPrincipal('local');
      // A tab still recording: its first minute is kept, the next is not out yet.
      const keeper = new VoiceTakeKeeper('held', { target: 'ses_a', intent: 'send' });
      keeper.begin();
      keeper.segment({ sequence: 0, final: false, durationMs: 60_000 }, wav('minute one'));
      await keeper.written();
      const [kept] = await readVoiceTakes();
      finalize.mockResolvedValue({ text: 'the first minute', cleanup: 'success' });

      expect(await heldVoiceTakes()).toContain('held');
      await expect(retryKeptVoiceTake(kept!)).resolves.toBe('');
      expect(finalize).not.toHaveBeenCalled();
      expect((await readVoiceTakes())[0]).toMatchObject({ status: 'recording' });

      // Its surface let go of it (or its page went away): the take is free to recover, with a
      // finalize-only request for the end the recording never reached.
      keeper.release();
      await keeper.written();
      await vi.waitFor(async () => expect(await heldVoiceTakes()).not.toContain('held'));
      const [left] = await readVoiceTakes();
      await expect(retryKeptVoiceTake(left!)).resolves.toBe('the first minute');
      expect(sent()).toEqual([
        { sequence: 0, final: false, blob: true },
        { sequence: 1, final: true, blob: false },
      ]);
      expect((await readVoiceTakes())[0]).toMatchObject({ status: 'transcribed', text: 'the first minute', truncated: false });
    } finally {
      uninstall();
    }
  });

  it('lands a kept take\'s words once across documents, and never one already gone or held elsewhere', async () => {
    const uninstall = installWebLocks();
    try {
      setLocalSnapshotPrincipal('local');
      await saveVoiceTake(take('twice', { status: 'transcribed', text: 'Ship it' }));
      // The same take, shown in two tabs, sent from both at once.
      let sent!: (value: boolean) => void;
      const land = vi.fn(() => new Promise<boolean>((resolve) => {
        sent = resolve;
      }));
      const first = landVoiceTake('twice', land);
      const second = landVoiceTake('twice', land);
      await expect(second).resolves.toBe(false);
      await vi.waitFor(() => expect(land).toHaveBeenCalledTimes(1));
      sent(true);
      await expect(first).resolves.toBe(true);
      expect(await readVoiceTakes()).toEqual([]);
      // A stale item for a take another tab already sent.
      await expect(landVoiceTake('twice', land)).resolves.toBe(false);
      expect(land).toHaveBeenCalledTimes(1);

      // Nor the previous reader's words, once the reader changed while the take was checked.
      await saveVoiceTake(take('switched', { status: 'transcribed', text: 'Alice only' }));
      const landAlice = vi.fn(() => true);
      const landing = landVoiceTake('switched', landAlice);
      await vi.waitFor(async () => expect(await heldVoiceTakes()).toContain('switched'));
      setLocalSnapshotPrincipal('bob');
      await landing;
      expect(landAlice).not.toHaveBeenCalled();
      setLocalSnapshotPrincipal('local');

      // One a surface still holds (recording, or showing it in its card) is that surface's.
      const keeper = new VoiceTakeKeeper('held', { target: 'ses_a', intent: 'send' });
      keeper.begin();
      keeper.transcribed('words');
      await keeper.written();
      await expect(landVoiceTake('held', land)).resolves.toBe(false);
      expect(land).toHaveBeenCalledTimes(1);
      expect(await readVoiceTakes()).toHaveLength(1);
    } finally {
      uninstall();
    }
  });

  it('returns nothing from a read the reader changed under', async () => {
    setLocalSnapshotPrincipal('alice');
    (await keepTake('private')).release();
    const takes = readVoiceTakes();
    const audio = readVoiceTakeAudio('private');
    setLocalSnapshotPrincipal('bob');
    expect(await takes).toEqual([]);
    expect((await audio).size).toBe(0);

    setLocalSnapshotPrincipal('carol');
    (await keepTake('signed-out')).release();
    const afterSignOut = readVoiceTakes();
    bumpSignOutGeneration();
    expect(await afterSignOut).toEqual([]);
  });

  it('never sets back a take its keeper advanced while another surface retried it', async () => {
    // Where the browser has no Web Locks, a document cannot see another's surface: the revision
    // check is what keeps a recovery from setting the take back.
    setLocalSnapshotPrincipal('local');
    // Another tab is still recording it: one minute is kept so far.
    const keeper = new VoiceTakeKeeper('still-recording', { target: 'ses_a', intent: 'send' });
    keeper.begin();
    keeper.segment({ sequence: 0, final: false, durationMs: 60_000 }, wav('minute one'));
    await keeper.written();
    // Another surface retries the take as it reads it now, and the keeper writes before it ends.
    const retryWhileItRecords = async (outcome: (resolve: (value: unknown) => void, reject: (error: Error) => void) => void) => {
      const [kept] = await readVoiceTakes();
      let settle!: () => void;
      finalize.mockImplementationOnce(() => new Promise((resolve, reject) => {
        settle = () => outcome(resolve, reject);
      }));
      const running = retryKeptVoiceTake(kept!);
      return { running, settle: () => settle() };
    };

    const succeeds = await retryWhileItRecords((resolve) => resolve({ text: 'the first minute only', cleanup: 'success' }));
    await idle();
    keeper.segment({ sequence: 1, final: false, durationMs: 60_000, overlapMs: 500 }, wav('minute two'));
    await keeper.written();
    succeeds.settle();
    await succeeds.running;
    expect((await readVoiceTakes())[0]).toMatchObject({ status: 'recording', segments: [{ sequence: 0 }, { sequence: 1 }] });
    expect((await readVoiceTakes())[0]?.text).toBeUndefined();

    keeper.transcribed('both minutes');
    await keeper.written();
    const fails = await retryWhileItRecords((_resolve, reject) => reject(new Error('Load failed')));
    await idle();
    keeper.segment({ sequence: 2, final: true, durationMs: 9_000, overlapMs: 500 }, wav('tail'));
    keeper.transcribed('both minutes and the tail');
    await keeper.written();
    fails.settle();
    await expect(fails.running).rejects.toThrow('Load failed');
    expect((await readVoiceTakes())[0]).toMatchObject({ status: 'transcribed', text: 'both minutes and the tail' });
    expect((await readVoiceTakes())[0]?.segments).toHaveLength(3);
  });

  it('never takes audio that did not reach the device for the end of the take, and says the rest is cut', async () => {
    setLocalSnapshotPrincipal('local');
    await saveVoiceTake(take('cut', {
      segments: [
        { sequence: 0, final: false, durationMs: 60_000 },
        { sequence: 1, final: false, durationMs: 60_000 },
        { sequence: 2, final: true, durationMs: 20_000 },
      ],
    }));
    // The page died between the last segment's details and its bytes.
    await saveVoiceTakeAudio('cut', 0, wav('minute one'));
    await saveVoiceTakeAudio('cut', 1, wav('minute two'));
    finalize.mockResolvedValue({ text: 'two minutes', cleanup: 'success' });

    await expect(retryKeptVoiceTake((await readVoiceTakes())[0]!)).resolves.toBe('two minutes');
    expect(sent()).toEqual([
      { sequence: 0, final: false, blob: true },
      { sequence: 1, final: false, blob: true },
      { sequence: 2, final: true, blob: false },
    ]);
    expect((await readVoiceTakes())[0]).toMatchObject({ status: 'transcribed', text: 'two minutes', truncated: true });
  });

  it('keeps a take another tab discarded discarded, while its recording here goes on', async () => {
    setLocalSnapshotPrincipal('local');
    const keeper = new VoiceTakeKeeper('discarded-elsewhere', { target: 'ses_a', intent: 'send' });
    keeper.begin();
    keeper.segment({ sequence: 0, final: false, durationMs: 60_000 }, wav('minute one'));
    await idle();
    // Another tab's pending item discards it.
    await deleteVoiceTake('discarded-elsewhere');
    keeper.segment({ sequence: 1, final: true, durationMs: 9_000 }, wav('tail'));
    keeper.transcribed('words');
    await idle();
    expect(await readVoiceTakes()).toEqual([]);
  });
});
