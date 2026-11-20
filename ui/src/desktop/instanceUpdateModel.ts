// What the update panel's section for the selected instance shows, from the state the shell keeps for it
// (`instance_update`: the instance's own `/api/version`, read by the shell with the app's sign-in, and the
// last action the shell sent). The same mapping every other surface uses turns the snapshot into the
// section's model; this only adds what the shell knows about its own request.
// ui/design: update/instance-and-app
import { modelFromDesktopRelay, modelFromRuntime, type UpdatePanelModel } from '../components/update/updatePanelModel';
import { installRunning, normalizeVersion } from '../components/update/versions';
import type { InstanceUpdateState } from './bridge';

export type InstanceSection = {
  /** None while the instance cannot be read. */
  model: UpdatePanelModel | null;
  unavailable?: 'unreachable' | 'unauthorized';
  /** The version an install would be asked for: the one on screen. */
  target: string | null;
  /** The account may install here, and no install is on its way or under way. */
  canUpdate: boolean;
  canSet: { prereleases: boolean; autoUpdate: boolean };
  /** Something sent from here is on its way, or an install is under way: the shell takes no other action for
   *  this instance until it ends, so the check and the switches wait. */
  busy: boolean;
};

const WAITING: UpdatePanelModel = {
  subject: 'runtime', current: null, latest: null, prereleases: null, checkedAt: null, status: { kind: 'checking' },
};

/** How long the shell follows an install it sent (`FOLLOW_INSTALL` in instance_update.rs). After that it takes
 *  other actions for the instance again, and an install that has still changed nothing did not take. */
const FOLLOW_INSTALL_MS = 30 * 60_000;

export function instanceSection(update: InstanceUpdateState, now: number): InstanceSection {
  const { snapshot, request } = update;
  const none = { target: null, canUpdate: false, canSet: { prereleases: false, autoUpdate: false }, busy: false };
  if (update.status === 'unauthorized') return { ...none, model: null, unavailable: 'unauthorized' };
  if (!snapshot) return { ...none, model: update.status === 'loading' ? WAITING : null };

  const sending = request?.phase === 'sending' ? request.action : null;
  const install = request?.action === 'install' ? request : null;
  const managed = snapshot.managed_by === 'desktop';
  const offered = snapshot.has_update && snapshot.latest ? snapshot.latest : null;
  const reachable = update.status !== 'unreachable';
  // The shell's own accepted install, while the shell still follows it.
  const followed = install?.phase === 'accepted' && now - Date.parse(install.updated_at) < FOLLOW_INSTALL_MS;
  // A desktop-managed host relays its own receipt of the install (`remote_request`): that receipt, not the
  // shell's, says whether it still runs, and when it failed, was declined or expired. While the host does
  // not answer, its last receipt stands only for an install this shell is following.
  const relayed = managed && installRunning(snapshot) && (reachable || followed);
  // A standalone Runtime restarts after an accepted install. While its last answer still offers what was
  // installed, the old version on screen is that restart: answering or not. Once it has answered with
  // anything else the install has settled, and a later outage is a lost connection like any other.
  const sameOffer = Boolean(install?.target && offered && normalizeVersion(offered) === normalizeVersion(install.target));
  const restarting = !managed && followed && sameOffer;
  // Accepted, no longer followed, and still the same offer on the same version: it did not take.
  const lapsed = !managed && install?.phase === 'accepted' && !followed && sameOffer;
  // An instance that does not answer, with nothing of ours under way, is a lost connection.
  if (!reachable && !restarting && !relayed && sending !== 'install') return { ...none, model: null };

  const can = update.can_manage;
  const takes = (action: 'prereleases' | 'auto_update' | 'install') => !managed || (snapshot.host_actions?.includes(action) ?? false);
  const model = managed
    ? modelFromDesktopRelay(snapshot, {
      requesting: sending === 'install',
      error: install?.phase === 'failed' ? install.error_code ?? 'request_failed' : null,
      timedOut: false,
      disconnected: !reachable,
    })
    : modelFromRuntime(snapshot, {
      checking: sending === 'check',
      installing: sending === 'install',
      restarting,
      // A standalone install is not pinned to the version clicked: the result names the one now running. The
      // shell keeps its receipt until the next action, so a release offered since then speaks instead of it.
      result: install?.phase === 'failed' || lapsed ? { ok: false, target: install?.target ?? null }
        : install?.phase === 'accepted' && !restarting && !offered ? { ok: true, target: snapshot.current } : null,
      prereleases: can && typeof snapshot.prereleases === 'boolean' ? snapshot.prereleases : null,
      autoUpdate: can && typeof snapshot.auto_update === 'boolean' ? snapshot.auto_update : null,
      checkedAt: update.fetched_at,
    });
  const busy = sending !== null || restarting || (managed ? relayed : installRunning(snapshot));
  return {
    model,
    target: offered,
    canUpdate: can && takes('install') && !busy,
    canSet: { prereleases: can && takes('prereleases'), autoUpdate: can && takes('auto_update') },
    busy,
  };
}
