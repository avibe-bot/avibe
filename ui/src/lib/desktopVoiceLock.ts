/** Read-only native lock lifetime. No marker means ordinary Web/iOS behavior.
 * A macOS document waits for its first native state before admitting voice. */
type VoiceLockState = { revision: number; locked: boolean };
type VoiceLockWindow = Window & { __AVIBE_VOICE_LOCK__?: VoiceLockState };
const listeners = new Set<() => void>();
const unlockListeners = new Set<() => void>();
const read = (): VoiceLockState => (typeof window === 'undefined'
  ? undefined : (window as VoiceLockWindow).__AVIBE_VOICE_LOCK__) ?? { revision: 0, locked: false };
let observed = read();

if (typeof window !== 'undefined') window.addEventListener('avibe:voice-lock', () => {
  const next = read();
  // A late native snapshot cannot revive an older document lifetime.
  if (next.revision < observed.revision) {
    (window as VoiceLockWindow).__AVIBE_VOICE_LOCK__ = observed;
    return;
  }
  const invalidate = next.locked || (!observed.locked && next.revision > observed.revision + 1);
  const changed = next.revision !== observed.revision || next.locked !== observed.locked;
  observed = next;
  if (changed && invalidate) [...listeners].forEach((listener) => listener());
  if (changed && !next.locked) [...unlockListeners].forEach((listener) => listener());
});

export const desktopVoiceLock = {
  allowed: (): boolean => !read().locked,
  token: (): number => read().revision,
  current: (token: number): boolean => !read().locked && (read().revision === token || (token === -1 && read().revision === 0)),
  onUnlock: (showRetained: () => void): (() => void) => {
    unlockListeners.add(showRetained);
    return () => { unlockListeners.delete(showRetained); };
  },
  onInvalidate: (cancel: () => void): (() => void) => {
    listeners.add(cancel);
    return () => { listeners.delete(cancel); };
  },
};
