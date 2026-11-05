// The last known answer of ``/api/asr/status`` for the current reader, so a
// composer can render its voice control from the first frame instead of popping
// it in when the read answers (docs/plans/web-ui-local-cache.md section 4.2). It
// is kept in memory for this document and, per reader, in the local snapshot
// store across loads; the live read still decides, and may only narrow what was
// shown (the composer keeps the control's place, disabled).

import {
  getLocalSnapshotEpoch,
  getLocalSnapshotPrincipal,
  readLocalSnapshot,
  subscribeLocalSnapshotEpoch,
  subscribeLocalSnapshotPrincipal,
  writeLocalSnapshot,
} from './localSnapshots';

export type VoiceStatus = { available: boolean; maxFileBytes: number | null };

const KEY = 'voice:asr-status';
const SCHEMA = 1;

let known: VoiceStatus | null = null;
let loading: { principal: string; promise: Promise<void> } | null = null;

const parse = (value: unknown): VoiceStatus | null => {
  if (typeof value !== 'object' || value === null) return null;
  const { available, maxFileBytes } = value as Partial<VoiceStatus>;
  if (typeof available !== 'boolean') return null;
  if (maxFileBytes !== null && typeof maxFileBytes !== 'number') return null;
  return { available, maxFileBytes };
};

// What was known belongs to the reader it was read for and to the store it came
// from: a new reader or a cleared store starts unknown.
const forget = () => {
  known = null;
  loading = null;
};
subscribeLocalSnapshotPrincipal((next) => {
  forget();
  if (next) void loadKnownVoiceStatus();
});
subscribeLocalSnapshotEpoch(forget);

/** The last known status for the current reader, or null when none is known. */
export function knownVoiceStatus(): VoiceStatus | null {
  return known;
}

/** Load the current reader's stored status into memory; resolves once known (or
 *  known to be missing). Safe to call repeatedly. */
export function loadKnownVoiceStatus(): Promise<void> {
  const principal = getLocalSnapshotPrincipal();
  if (!principal) return Promise.resolve();
  if (loading?.principal === principal) return loading.promise;
  const epoch = getLocalSnapshotEpoch();
  const promise = readLocalSnapshot(KEY, SCHEMA, parse).then((stored) => {
    if (!stored || known !== null) return;
    if (getLocalSnapshotPrincipal() !== principal || getLocalSnapshotEpoch() !== epoch) return;
    known = stored;
  });
  loading = { principal, promise };
  return promise;
}

/** Note the store's state as a status read starts; ``remember`` refuses an
 *  answer that no longer belongs to the current reader and store. */
export function beginVoiceStatusRead(): { remember: (status: VoiceStatus) => void } {
  const principal = getLocalSnapshotPrincipal();
  const epoch = getLocalSnapshotEpoch();
  return {
    remember: (status) => {
      if (!principal || getLocalSnapshotPrincipal() !== principal || getLocalSnapshotEpoch() !== epoch) return;
      known = status;
      void writeLocalSnapshot(KEY, SCHEMA, status);
    },
  };
}
