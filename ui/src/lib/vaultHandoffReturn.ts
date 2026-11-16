import type { ApiContextType, VaultRequest, VaultSecret } from '@/context/ApiContext';

import { bringDesktopAppForward, canBringDesktopAppForward } from './desktopShell';
import type { VaultBrowserStep } from './vaultBrowserHandoff';

/**
 * Bringing the desktop app back after a Vaults step it handed to the browser
 * (`openVaultsInBrowser`) has been completed there by the user.
 *
 * The app remembers, in memory, the steps this window handed off. Its own
 * Workbench connection reports Vault changes; when one shows the user completed
 * a remembered step, the step is forgotten and, unless the user is already in
 * the app, the shell brings its window forward on whatever page it shows. A step
 * counts only once and only for {@link HANDOFF_RETURN_WINDOW_MS} after the
 * hand-off; a reload forgets them all.
 *
 * Which steps are remembered:
 * - a request (approve, deny, sign, provide): the app comes back for an outcome
 *   the user produced (approved, denied, fulfilled, completed, failed). A request
 *   that expired or was withdrawn is only forgotten: the user did nothing that
 *   calls for the app, and the browser tab already shows that state;
 * - an Add for a name an agent asked for: the app comes back when a secret of
 *   that name appears that was created after the hand-off.
 * Not remembered: an Add with no name (any new secret, an Agent's generated one
 * included, would look the same), an edit (a secret's update time also moves
 * when it is used), and a reveal (it changes nothing on the server, and the
 * revealed value is what the user is reading in the browser). The browser tab
 * still says when those are done.
 */

/** Requests live 30 minutes on the server; a step not finished by then is not coming back. */
export const HANDOFF_RETURN_WINDOW_MS = 30 * 60_000;
/** Server and app share this machine's clock; this absorbs only rounding. */
const CLOCK_TOLERANCE_MS = 5_000;

/** Request states the user's own answer in the browser produces. */
const USER_OUTCOMES = new Set(['approved', 'denied', 'fulfilled', 'completed', 'failed']);

type Handoff =
  | { kind: 'request'; requestId: string; at: number }
  | { kind: 'add'; name: string; at: number };

const handoffs = new Map<string, Handoff>();
const listeners = new Set<() => void>();

const keyOf = (handoff: Handoff) => (handoff.kind === 'request' ? `request:${handoff.requestId}` : `add:${handoff.name}`);

/**
 * Remember a step this window is handing to the browser. Only a shell that can
 * bring its window forward remembers anything; elsewhere this does nothing.
 */
export function noteVaultHandoff(step: VaultBrowserStep, now = Date.now()): void {
  if (!canBringDesktopAppForward()) return;
  const handoff: Handoff | null =
    step.kind === 'request'
      ? { kind: 'request', requestId: step.requestId, at: now }
      : step.kind === 'add' && step.name
        ? { kind: 'add', name: step.name, at: now }
        : null;
  if (!handoff) return;
  handoffs.set(keyOf(handoff), handoff);
  for (const listener of listeners) listener();
}

/**
 * What a handed-off request's current state means for the app: `return` for an
 * outcome the user produced, `forget` for one they did not (expired, withdrawn),
 * `wait` while it is still the browser's turn (pending, or `signing`, the
 * signature the user just allowed being made).
 */
export function vaultRequestHandoffState(request: Pick<VaultRequest, 'status'>): 'return' | 'forget' | 'wait' {
  if (request.status === 'pending' || request.status === 'signing') return 'wait';
  return USER_OUTCOMES.has(request.status) ? 'return' : 'forget';
}

/** Whether the secret a named Add handed off at `at` exists yet. */
export function vaultAddHandoffFinished(
  secrets: Pick<VaultSecret, 'name' | 'created_at'>[],
  handoff: { name: string; at: number },
): boolean {
  return secrets.some((secret) => {
    if (secret.name !== handoff.name) return false;
    const created = secret.created_at ? Date.parse(secret.created_at) : NaN;
    return !Number.isNaN(created) && created >= handoff.at - CLOCK_TOLERANCE_MS;
  });
}

type ReturnApi = Pick<ApiContextType, 'connectWorkbenchEvents' | 'getVaultRequest' | 'listVaultSecrets'>;

/**
 * Follow the remembered hand-offs until `stop` is called. `bringForward` runs
 * once per completed step while this document does not have focus.
 */
export function watchVaultHandoffs(
  api: ReturnApi,
  bringForward: () => void = () => void bringDesktopAppForward(),
  clock: () => number = Date.now,
): () => void {
  let stopped = false;

  const forget = (key: string, handoff: Handoff) => {
    if (handoffs.get(key) !== handoff) return false;
    handoffs.delete(key);
    return true;
  };

  const finish = (key: string, handoff: Handoff) => {
    if (forget(key, handoff) && !document.hasFocus()) bringForward();
  };

  // A step not completed within the window is forgotten.
  const live = (key: string, handoff: Handoff) => {
    if (clock() - handoff.at <= HANDOFF_RETURN_WINDOW_MS) return true;
    forget(key, handoff);
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
    if (stopped || !request || request.id !== handoff.requestId) return;
    const state = vaultRequestHandoffState(request);
    if (state === 'return') finish(key, handoff);
    else if (state === 'forget') forget(key, handoff);
  };

  const checkRequests = () => {
    for (const [key, handoff] of handoffs) if (handoff.kind === 'request') void checkRequest(key, handoff);
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
    checkRequests();
    void checkAdds();
  };

  const onNoted = () => checkAll();
  listeners.add(onNoted);
  const disconnect = api.connectWorkbenchEvents({
    // Whatever a gap in the stream hid is read again here.
    onConnected: checkAll,
    onVaultsUpdated: (data) => {
      // Any request change is read for every remembered request: a provision answered with an
      // existing secret is announced by the secret, with a status but without the request's id.
      if (data.request_id || data.request_status || data.scope === 'request') checkRequests();
      if (data.scope === 'secret' && data.secret_name) void checkAdds();
    },
  });
  if (handoffs.size > 0) checkAll();

  return () => {
    stopped = true;
    listeners.delete(onNoted);
    disconnect();
  };
}
