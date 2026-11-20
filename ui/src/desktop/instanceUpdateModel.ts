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
  /** The shell would send this instance's updater a check now: it answers, the account may manage it, and its
   *  updater takes one. Otherwise the panel's check only reads the instance again. */
  canCheck: boolean;
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
/** A Runtime that answers, on the version and offer it had before an accepted install, is given this long to
 *  start its restart before the install is taken not to have finished. A Runtime that reports its own install
 *  (`install`) needs no such guess; this is for one that predates the report, or that installed and did not
 *  restart. */
const RESTART_GRACE_MS = 5 * 60_000;

export function instanceSection(update: InstanceUpdateState, now: number): InstanceSection {
  const { snapshot, request } = update;
  const sending = request?.phase === 'sending' ? request.action : null;
  // Nothing to offer; still waiting only for something of ours that is on its way.
  const none = { target: null, canUpdate: false, canSet: { prereleases: false, autoUpdate: false }, canCheck: false, busy: sending !== null };
  if (update.status === 'unauthorized') return { ...none, model: null, unavailable: 'unauthorized' };
  if (!snapshot) return { ...none, model: update.status === 'loading' ? WAITING : null };

  const install = request?.action === 'install' ? request : null;
  const managed = snapshot.managed_by === 'desktop';
  const offered = snapshot.has_update && snapshot.latest ? snapshot.latest : null;
  const reachable = update.status !== 'unreachable';
  // The instance's last answer still offers exactly what the install was sent for: nothing has changed yet.
  const sameOffer = Boolean(install?.target && offered && normalizeVersion(offered) === normalizeVersion(install.target));
  // The shell's own accepted install, and how long ago it was accepted.
  const accepted = install?.phase === 'accepted';
  const age = install ? now - Date.parse(install.updated_at) : 0;
  const followed = accepted && age < FOLLOW_INSTALL_MS;
  // What the instance itself reports is running: a standalone Runtime its own install and restart (`install`),
  // a desktop-managed host its receipt of the request (`remote_request`), which also says when that failed,
  // was declined or expired. While the instance does not answer (it is restarting), that last report stands
  // for as long as an install is followed: this shell's, or the Runtime's own by the time it started.
  const ownInstall = snapshot.install ? now - Date.parse(snapshot.install.started_at) < FOLLOW_INSTALL_MS : false;
  const reported = installRunning(snapshot) && (reachable || followed || ownInstall);
  // The host's receipt appears in the first read made after the shell's own was accepted; until that read
  // the last answer on screen predates the request, and the request is still on its way. The same holds
  // while the host has shown no receipt at all and still offers what was asked for.
  const unread = managed && followed && sameOffer
    && (!update.fetched_at || Date.parse(update.fetched_at) <= Date.parse(install!.updated_at) || !snapshot.remote_request);
  // A standalone Runtime restarts after an accepted install, and reports that itself when it can (`install`).
  // Without that report, its last answer still offering what was installed is that restart: for as long as
  // the shell follows it while it does not answer, and for a few minutes while it does. Once it has answered
  // with anything else the install has settled, and a later outage is a lost connection like any other.
  const pending = !managed && accepted && sameOffer && !snapshot.install;
  const restarting = pending && (reachable ? age < RESTART_GRACE_MS : followed);
  // Accepted, and nothing changed in that time: it did not finish.
  const lapsed = pending && !restarting;
  // The shell itself still follows that install until its own limit, and refuses every other action for the
  // instance meanwhile: the section says it did not finish, and offers nothing the shell would refuse.
  const held = lapsed && followed;
  // An instance that does not answer, with nothing of ours under way, is a lost connection.
  if (!reachable && !restarting && !reported && sending !== 'install') return { ...none, model: null };

  const can = update.can_manage;
  const takes = (action: 'prereleases' | 'auto_update' | 'install') => !managed || (snapshot.host_actions?.includes(action) ?? false);
  const model = managed
    // An account that cannot manage the host is shown no switches, as on a standalone Runtime.
    ? modelFromDesktopRelay(can ? snapshot : { ...snapshot, prereleases: null, auto_update: null }, {
      requesting: sending === 'install' || unread,
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
        : accepted && !restarting && !offered ? { ok: true, target: snapshot.current } : null,
      prereleases: can && typeof snapshot.prereleases === 'boolean' ? snapshot.prereleases : null,
      autoUpdate: can && typeof snapshot.auto_update === 'boolean' ? snapshot.auto_update : null,
      checkedAt: update.fetched_at,
    });
  const busy = sending !== null || restarting || unread || reported;
  return {
    model,
    target: offered,
    canUpdate: can && takes('install') && !busy && !held && offered !== null,
    canSet: { prereleases: can && !held && takes('prereleases'), autoUpdate: can && !held && takes('auto_update') },
    canCheck: reachable && update.status === 'ready' && can && !held && (!managed || (snapshot.host_actions?.includes('check') ?? false)),
    busy,
  };
}
