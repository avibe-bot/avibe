// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { afterEach, describe, expect, it, vi } from 'vitest';

import { clearLocalSnapshots, forgetLocalSnapshots, setLocalSnapshotPrincipal } from './localSnapshots';
import { bumpSignOutGeneration, SIGN_OUT_GENERATION_KEY } from './signOutGeneration';
import { VoiceTakeKeeper } from './voiceTakeKeeper';
import { keptVoiceSegments, retryKeptVoiceTake } from './voiceTakeRetry';
import {
  deleteVoiceTake,
  isVoiceTakeLive,
  readVoiceTakeAudio,
  readVoiceTakes,
  saveVoiceTake,
  subscribeVoiceTakes,
  VOICE_TAKE_LIMIT,
  voiceTakeDurationMs,
  type VoiceTake,
} from './voiceTakeStore';

// The store is what keeps a take's speech through a reload, and what keeps one reader's
// recordings away from the next person to sign in on the device.

const idle = () => new Promise((resolve) => setTimeout(resolve, 30));
const wav = (label: string) => new Blob([label], { type: 'audio/wav' });

const take = (id: string, patch: Partial<VoiceTake> = {}): VoiceTake => ({
  id,
  target: 'ses_a',
  intent: 'send',
  label: 'To Vibey',
  createdAt: Date.now(),
  updatedAt: Date.now(),
  status: 'pending',
  segments: [],
  ...patch,
});

/** A take recorded through the keeper: two minutes and a tail, then left without text. */
const keepTake = async (id: string, target = 'ses_a') => {
  const keeper = new VoiceTakeKeeper(id, { target, intent: 'send', label: 'To Vibey' });
  keeper.begin();
  keeper.segment({ sequence: 0, final: false, durationMs: 60_000 }, wav('minute one'));
  keeper.segment({ sequence: 1, final: false, durationMs: 60_000, overlapMs: 500 }, wav('minute two'));
  keeper.segment({ sequence: 2, final: true, durationMs: 23_000, overlapMs: 500 }, wav('tail'));
  keeper.pending();
  await idle();
  return keeper;
};

afterEach(async () => {
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
    expect(kept).toMatchObject({ id: 'take-1', status: 'pending', intent: 'send', label: 'To Vibey' });
    expect(voiceTakeDurationMs(kept!)).toBe(142_000);
    const audio = await readVoiceTakeAudio('take-1');
    expect(await audio.get(0)?.text()).toBe('minute one');
    expect(await audio.get(2)?.text()).toBe('tail');
    expect(isVoiceTakeLive('take-1')).toBe(true);

    keeper.transcribed('everything that was said');
    await idle();
    expect((await readVoiceTakes('ses_a'))[0]).toMatchObject({ status: 'transcribed', text: 'everything that was said' });

    keeper.forget();
    await idle();
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
    for (let index = 0; index <= VOICE_TAKE_LIMIT; index += 1) {
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
    const finalize = vi.fn(async (segments: unknown[]) => {
      expect(segments).toHaveLength(3);
      return { text: 'all of it, recovered', cleanup: 'success' as const };
    });

    await expect(retryKeptVoiceTake(kept!, { finalize: finalize as never })).resolves.toBe('all of it, recovered');
    expect((await readVoiceTakes('ses_a'))[0]).toMatchObject({ status: 'transcribed', text: 'all of it, recovered' });
  });

  it('keeps a kept take pending when it still cannot be transcribed, and does not bring back one discarded meanwhile', async () => {
    setLocalSnapshotPrincipal('local');
    (await keepTake('still-down')).release();
    const [kept] = await readVoiceTakes();
    await expect(retryKeptVoiceTake(kept!, {
      finalize: (async () => { throw new Error('Load failed'); }) as never,
    })).rejects.toThrow('Load failed');
    expect((await readVoiceTakes())[0]).toMatchObject({ status: 'pending' });

    let release!: () => void;
    const running = retryKeptVoiceTake(kept!, {
      finalize: (() => new Promise((resolve) => {
        release = () => resolve({ text: 'too late', cleanup: 'success' });
      })) as never,
    });
    await idle();
    await deleteVoiceTake(kept!.id);
    release();
    await running;
    expect(await readVoiceTakes()).toEqual([]);
  });

  it('finalizes a take the page lost while it recorded with a finalize-only request', () => {
    const segments = keptVoiceSegments(
      take('crashed', { status: 'recording', segments: [{ sequence: 0, final: false, durationMs: 60_000 }] }),
      new Map([[0, wav('minute one')]]),
    );
    expect(segments.map(({ sequence, final, blob }) => ({ sequence, final, blob: blob !== null }))).toEqual([
      { sequence: 0, final: false, blob: true },
      { sequence: 1, final: true, blob: false },
    ]);
  });
});
