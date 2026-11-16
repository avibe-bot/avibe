import type { ApiContextType, VaultRequest, VaultSecret } from '@/context/ApiContext';

import { bringDesktopAppForward, canBringDesktopAppForward } from './desktopShell';
import type { VaultBrowserStep } from './vaultBrowserHandoff';

/**
 * Bringing the desktop app back after a Vaults step it handed to the browser
 * (`openVaultsInBrowser`) has finished there.
 *
 * The app remembers, in memory, the steps this window handed off. Its own
 * Workbench connection reports Vault changes; when one finishes a remembered
 * step, the step is forgotten and, unless the user is already in the app, the
 * shell brings its window forward on whatever page it shows. A step counts only
 * once and only for {@link HANDOFF_RETURN_WINDOW_MS} after the hand-off; a
 * reload forgets them all.
 *
 * Which steps are remembered:
 * - a request (approve, deny, sign, provide): finished once the server no
 *   longer has it pending, or once it is past its expiry (expiry sends no event,
 *   so a timer checks it then);
 * - an Add: finished when a secret (of the requested name, if any) appears
 *   that was created after the hand-off.
 * An edit and a reveal are not: a secret's update time also moves when it is
 * used, so it cannot tell an edit apart, and a reveal changes nothing on the
 * server. A revealed value is also exactly what the user is reading in the
 * browser, so pulling the app over it would be wrong.
 */

/** Requests live 30 minutes on the server; a step not finished by then is not coming back. */
export const HANDOFF_RETURN_WINDOW_MS = 30 * 60_000;
/** Server and app share this machine's clock; this absorbs only rounding. */
const CLOCK_TOLERANCE_MS = 5_000;
/** A pending request is re-read this long after its expiry, once the server has swept it. */
const EXPIRY_GRACE_MS = 1_500;

type Handoff =
  | { kind: 'request'; requestId: string; at: number; expiresAt?: number }
  | { kind: 'add'; name?: string; at: number };

const handoffs = new Map<string, Handoff>();
const listeners = new Set<() => void>();

const keyOf = (handoff: Handoff) => (handoff.kind === 'request' ? `request:${handoff.requestId}` : `add:${handoff.name ?? ''}`);

/**
 * Remember a step this window is handing to the browser. Only a shell that can
 * bring its window forward remembers anything; elsewhere this does nothing.
 */
export function noteVaultHandoff(step: VaultBrowserStep, now = Date.now()): void {
  if (!canBringDesktopAppForward()) return;
  const handoff: Handoff | null =
    step.kind === 'request'
      ? { kind: 'request', requestId: step.requestId, at: now }
      : step.kind === 'add'
        ? { kind: 'add', name: step.name, at: now }
        : null;
  if (!handoff) return;
  handoffs.set(keyOf(handoff), handoff);
  for (const listener of listeners) listener();
}

/** Whether `request` no longer waits on the user: answered, failed, or past its expiry. */
export function vaultRequestHandoffFinished(request: Pick<VaultRequest, 'status' | 'expires_at'>, now: number): boolean {
  // `signing` is the browser still producing the signature it was just allowed.
  if (request.status === 'signing') return false;
  if (request.status !== 'pending') return true;
  const expiresAt = request.expires_at ? Date.parse(request.expires_at) : NaN;
  return !Number.isNaN(expiresAt) && expiresAt <= now;
}

/** The secret an Add handed off at `at` produced, if it exists yet. */
export function vaultAddHandoffFinished(
  secrets: Pick<VaultSecret, 'name' | 'created_at'>[],
  handoff: { name?: string; at: number },
): boolean {
  return secrets.some((secret) => {
    if (handoff.name && secret.name !== handoff.name) return false;
    const created = secret.created_at ? Date.parse(secret.created_at) : NaN;
    return !Number.isNaN(created) && created >= handoff.at - CLOCK_TOLERANCE_MS;
  });
}

type ReturnApi = Pick<ApiContextType, 'connectWorkbenchEvents' | 'getVaultRequest' | 'listVaultSecrets'>;

/**
 * Follow the remembered hand-offs until `stop` is called. `bringForward` runs
 * once per finished step while this document does not have focus.
 */
export function watchVaultHandoffs(
  api: ReturnApi,
  bringForward: () => void = () => void bringDesktopAppForward(),
  clock: () => number = Date.now,
): () => void {
  let stopped = false;
  const timers = new Map<string, number>();

  const finish = (key: string, handoff: Handoff) => {
    if (handoffs.get(key) !== handoff) return;
    handoffs.delete(key);
    window.clearTimeout(timers.get(key));
    timers.delete(key);
    if (!document.hasFocus()) bringForward();
  };

  // A step not finished within the window is forgotten, and its timer with it.
  const live = (key: string, handoff: Handoff) => {
    if (clock() - handoff.at <= HANDOFF_RETURN_WINDOW_MS) return true;
    if (handoffs.get(key) === handoff) handoffs.delete(key);
    window.clearTimeout(timers.get(key));
    timers.delete(key);
    return false;
  };

  const checkRequest = async (key: string, handoff: Extract<Handoff, { kind: 'request' }>) => {
    if (!live(key, handoff)) return;
    let request: VaultRequest | undefined;
    try {
      const reply = await api.getVaultRequest(handoff.requestId);
      request = reply?.ok === false ? undefined : reply?.request;
    } catch {
      return;
    }
    if (stopped || handoffs.get(key) !== handoff || !request || request.id !== handoff.requestId) return;
    if (vaultRequestHandoffFinished(request, clock())) {
      finish(key, handoff);
      return;
    }
    // Expiry sends no event: look again just after it, if that falls inside the window.
    const expiresAt = request.expires_at ? Date.parse(request.expires_at) : NaN;
    if (Number.isNaN(expiresAt) || timers.has(key) || expiresAt > handoff.at + HANDOFF_RETURN_WINDOW_MS) return;
    timers.set(key, window.setTimeout(() => {
      timers.delete(key);
      void checkRequest(key, handoff);
    }, Math.max(0, expiresAt - clock() + EXPIRY_GRACE_MS)));
  };

  const checkAdds = async () => {
    const adds = [...handoffs].filter((entry): entry is [string, Extract<Handoff, { kind: 'add' }>] => (
      entry[1].kind === 'add' && live(entry[0], entry[1])
    ));
    if (adds.length === 0) return;
    let secrets: VaultSecret[];
    try {
      secrets = (await api.listVaultSecrets({ cache: false })).secrets ?? [];
    } catch {
      return;
    }
    if (stopped) return;
    for (const [key, handoff] of adds) if (vaultAddHandoffFinished(secrets, handoff)) finish(key, handoff);
  };

  const checkAll = () => {
    for (const [key, handoff] of handoffs) if (handoff.kind === 'request') void checkRequest(key, handoff);
    void checkAdds();
  };

  // A new hand-off is read at once: that also learns its expiry.
  const onNoted = () => checkAll();
  listeners.add(onNoted);
  const disconnect = api.connectWorkbenchEvents({
    // Whatever a gap in the stream hid is read again here.
    onConnected: checkAll,
    onVaultsUpdated: (data) => {
      if (data.request_id) {
        const key = `request:${data.request_id}`;
        const handoff = handoffs.get(key);
        if (handoff?.kind === 'request') void checkRequest(key, handoff);
      }
      if (data.scope === 'secret' && data.secret_name) void checkAdds();
    },
  });
  if (handoffs.size > 0) checkAll();

  return () => {
    stopped = true;
    listeners.delete(onNoted);
    for (const timer of timers.values()) window.clearTimeout(timer);
    timers.clear();
    disconnect();
  };
}

