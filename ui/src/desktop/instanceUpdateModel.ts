// What the update panel's section for the selected instance shows, from the state the shell keeps for it
// (`instance_update`: the instance's own `/api/version`, read by the shell with the app's sign-in, and the
// last action the shell sent, with the shell's own word on whether it still follows it). The same mapping
// every other surface uses turns the snapshot into the section's model; this only adds what the shell says
// about its own request.
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

/** The Runtime's own report of an install stands for this long while it does not answer, when the install was
 *  not sent from this shell (which then has no receipt to follow). */
const OWN_INSTALL_MS = 30 * 60_000;

export function instanceSection(update: InstanceUpdateState, now: number): InstanceSection {
  const { snapshot, request } = update;
  const sending = request?.phase === 'sending' ? request.action : null;
  // The shell's own word: it would refuse another action for this instance now, because what it sent is still
  // on its way or still being followed.
  const following = request?.following === true;
  // Nothing to offer; still waiting only for something of ours.
  const none = { target: null, canUpdate: false, canSet: { prereleases: false, autoUpdate: false }, canCheck: false, busy: following };
  if (update.status === 'unauthorized') return { ...none, model: null, unavailable: 'unauthorized' };
  if (!snapshot) return { ...none, model: update.status === 'loading' ? WAITING : null };

  const install = request?.action === 'install' ? request : null;
  const managed = snapshot.managed_by === 'desktop';
  const offered = snapshot.has_update && snapshot.latest ? snapshot.latest : null;
  const reachable = update.status !== 'unreachable';
  const accepted = install?.phase === 'accepted';
  // An accepted install the shell still follows: under way, whatever the instance's last answer shows.
  const followed = accepted && following;
  // Whether the instance now runs another version than when the install was accepted. Where the shell could
  // not say what it ran then, an unchanged offer of the same version stands in for "nothing changed".
  const changed = accepted && (install.installed_from !== null
    ? normalizeVersion(snapshot.current) !== normalizeVersion(install.installed_from)
    : !(install.target && offered && normalizeVersion(offered) === normalizeVersion(install.target)));
  // What the instance itself reports as running: a standalone Runtime its own install and restart (`install`),
  // a desktop-managed host its receipt of the request (`remote_request`). While it does not answer, that last
  // report stands for an install this shell follows, or for the Runtime's own for a bounded time.
  const ownInstall = snapshot.install ? now - Date.parse(snapshot.install.started_at) < OWN_INSTALL_MS : false;
  const reported = installRunning(snapshot) && (reachable || followed || ownInstall);
  // An instance that does not answer, with nothing of ours under way, is a lost connection.
  if (!reachable && !followed && !reported && sending !== 'install') return { ...none, model: null };

  const can = update.can_manage;
  const takes = (action: 'prereleases' | 'auto_update' | 'install' | 'check') => !managed || (snapshot.host_actions?.includes(action) ?? false);
  const model = managed
    // An account that cannot manage the host is shown no switches, as on a standalone Runtime.
    ? modelFromDesktopRelay(can ? snapshot : { ...snapshot, prereleases: null, auto_update: null }, {
      // On its way until the host's own receipt of it shows in an answer.
      requesting: sending === 'install' || (followed && !installRunning(snapshot)),
      error: install?.phase === 'failed' ? install.error_code ?? 'request_failed' : null,
      timedOut: false,
      disconnected: !reachable,
    })
    : modelFromRuntime(snapshot, {
      checking: sending === 'check',
      installing: sending === 'install',
      // Followed, and the Runtime has not reported an install of its own: it is restarting into it.
      restarting: followed && !snapshot.install,
      // No longer followed: it runs another version (the result names that one, since a standalone install
      // is not pinned to the version clicked; a release offered since speaks instead), or nothing changed
      // and it did not finish. The shell takes a retry from then on.
      result: install?.phase === 'failed' || (accepted && !followed && !changed) ? { ok: false, target: install?.target ?? null }
        : accepted && !followed && !offered ? { ok: true, target: snapshot.current } : null,
      prereleases: can && typeof snapshot.prereleases === 'boolean' ? snapshot.prereleases : null,
      autoUpdate: can && typeof snapshot.auto_update === 'boolean' ? snapshot.auto_update : null,
      checkedAt: update.fetched_at,
    });
  // The shell refuses while it follows; an install the instance reports from elsewhere is not offered again.
  const busy = following || reported;
  return {
    model,
    target: offered,
    canUpdate: can && takes('install') && !busy && offered !== null,
    canSet: { prereleases: can && takes('prereleases'), autoUpdate: can && takes('auto_update') },
    canCheck: update.status === 'ready' && can && !following && takes('check'),
    busy,
  };
}
