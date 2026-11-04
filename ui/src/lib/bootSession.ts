import { normalizeSessionInfo, type SessionInfo } from './sessionInfo';

// The route guard's last successful outcome, so a cold start can render the
// shell at once and re-validate in the background instead of waiting on
// ``/api/session`` (and, for owners, ``/api/config``) first
// (docs/plans/web-ui-local-cache.md section 4.5).
//
// It is only ever an outcome the server confirmed in full: a signed-in reader
// whose authorization was current and, when setup applies, an instance whose
// setup is complete. It is used only until the session itself expires (the
// server reports ``session_expires_at``; without it, 24 hours), and any other
// answer from the server deletes it, as do sign-out, revocation, and a
// login-required signal.
//
// localStorage, not IndexedDB: the guard reads it in its first render, so the
// read must be synchronous. It is per origin, so per instance.

const BOOT_SESSION_KEY = 'avibe.boot-session.v1';
// Shared by every tab of the origin and advanced by every forget. A guard check
// captures it when it starts; if anything voided the record meanwhile, in this
// tab or another (sign-out, revocation, login required, any gate outcome), the
// check's answer predates that and is not recorded.
const BOOT_SESSION_GENERATION_KEY = 'avibe.boot-session.generation.v1';

/** How long an outcome is trusted when the server did not say when the session
 *  expires (local loopback, or an instance from before ``session_expires_at``):
 *  the 24 hours remote sessions used to last. */
export const BOOT_SESSION_MAX_AGE_MS = 24 * 60 * 60 * 1000;
/** Never trust an outcome longer than the longest a remote session can live
 *  (docs/plans/remote-session-lifetime.md: a 90-day absolute cap), whatever an
 *  old record claims. */
export const BOOT_SESSION_HARD_MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000;

type BootSessionRecord = { version: 1; savedAt: number; session: unknown };

// Set when sign-out begins. A guard check that started before it may still
// answer ``ready`` while the logout request is out; it must not re-record the
// reader who is leaving. Sign-out ends in a reload, which resets this.
let recordingBlocked = false;

function storage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    return null;
  }
}

const isReadyOutcome = (session: SessionInfo): boolean =>
  !session.remote || (session.authenticated && session.authorization_state === 'current');

/** The last confirmed outcome, or null when there is none, it is too old, or it
 *  does not describe a reader the guard would let in. */
export function readBootSession(now = Date.now()): SessionInfo | null {
  const store = storage();
  if (!store) return null;
  try {
    const raw = store.getItem(BOOT_SESSION_KEY);
    if (!raw) return null;
    const record = JSON.parse(raw) as Partial<BootSessionRecord>;
    if (record.version !== 1 || typeof record.savedAt !== 'number') return null;
    const age = now - record.savedAt;
    if (!(age >= 0 && age < BOOT_SESSION_HARD_MAX_AGE_MS)) return null;
    const session = normalizeSessionInfo(record.session);
    if (!isReadyOutcome(session)) return null;
    // The session's own expiry when the server reported one, else the fallback.
    const expiresAt = session.remote && session.authenticated && session.authorization_state === 'current'
      && session.session_expires_at !== undefined
      ? session.session_expires_at * 1000
      : record.savedAt + BOOT_SESSION_MAX_AGE_MS;
    return now < expiresAt ? session : null;
  } catch {
    return null;
  }
}

/** The generation a guard check captures when it starts, so its answer can be
 *  refused if the record was voided anywhere meanwhile. */
export function bootSessionGeneration(): number {
  try {
    const raw = storage()?.getItem(BOOT_SESSION_GENERATION_KEY) ?? null;
    const value = raw === null ? 0 : Number(raw);
    return Number.isFinite(value) ? value : 0;
  } catch {
    return 0;
  }
}

/** Remember an outcome the server just confirmed. ``generation`` is the value
 *  of ``bootSessionGeneration()`` when the check that produced it started. */
export function recordBootSession(
  session: SessionInfo,
  now = Date.now(),
  generation = bootSessionGeneration(),
): void {
  const store = storage();
  if (!store || recordingBlocked || !isReadyOutcome(session)) return;
  if (generation !== bootSessionGeneration()) return;
  try {
    const record: BootSessionRecord = { version: 1, savedAt: now, session };
    store.setItem(BOOT_SESSION_KEY, JSON.stringify(record));
  } catch {
    // Best-effort: without it the next cold start simply waits for the server.
  }
}

/** Sign-out: forget the outcome and refuse to record another one in this
 *  document, whatever a check already in flight answers. */
export function blockBootSessionRecording(): void {
  recordingBlocked = true;
  forgetBootSession();
}

/** Sign-out has settled (whether or not the request succeeded): forget again.
 *  A check another tab started after the first forget but before the server
 *  dropped the cookie captured that generation, and must not record the
 *  leaving reader once it answers. */
export function settleSignOutBootSession(): void {
  forgetBootSession();
}

/** Forget it, and void every check in flight in any tab: the next cold start
 *  waits for the server again. */
export function forgetBootSession(): void {
  const store = storage();
  if (!store) return;
  try {
    store.setItem(BOOT_SESSION_GENERATION_KEY, String(bootSessionGeneration() + 1));
    store.removeItem(BOOT_SESSION_KEY);
  } catch {
    // Nothing more to do.
  }
}
