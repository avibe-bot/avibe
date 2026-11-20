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

export function instanceSection(update: InstanceUpdateState): InstanceSection {
  const { snapshot, request } = update;
  const none = { target: null, canUpdate: false, canSet: { prereleases: false, autoUpdate: false }, busy: false };
  if (update.status === 'unauthorized') return { ...none, model: null, unavailable: 'unauthorized' };
  if (!snapshot) return { ...none, model: update.status === 'loading' ? WAITING : null };

  const sending = request?.phase === 'sending' ? request.action : null;
  const install = request?.action === 'install' ? request : null;
  const managed = snapshot.managed_by === 'desktop';
  const offered = snapshot.has_update && snapshot.latest ? snapshot.latest : null;
  // After an accepted install the instance restarts. Until it answers with something other than the offer
  // that was installed (or does not answer at all), the old version on screen is that restart, not a refusal.
  const sameOffer = Boolean(install?.target && offered && normalizeVersion(offered) === normalizeVersion(install.target));
  const restarting = install?.phase === 'accepted' && (update.status === 'unreachable' || sameOffer);
  // An instance that does not answer, with nothing of ours under way, is a lost connection.
  if (update.status === 'unreachable' && !restarting && sending !== 'install') return { ...none, model: null };

  const can = update.can_manage;
  const takes = (action: 'prereleases' | 'auto_update' | 'install') => !managed || (snapshot.host_actions?.includes(action) ?? false);
  const model = managed
    ? modelFromDesktopRelay(snapshot, {
      requesting: sending === 'install',
      error: install?.phase === 'failed' ? install.error_code ?? 'request_failed' : null,
      timedOut: false,
      disconnected: update.status === 'unreachable',
    })
    : modelFromRuntime(snapshot, {
      checking: sending === 'check',
      installing: sending === 'install',
      restarting,
      // A standalone install is not pinned to the version clicked: the result names the one now running.
      result: install?.phase === 'failed' ? { ok: false, target: install.target }
        : install?.phase === 'accepted' && !restarting ? { ok: true, target: snapshot.current } : null,
      prereleases: can && typeof snapshot.prereleases === 'boolean' ? snapshot.prereleases : null,
      autoUpdate: can && typeof snapshot.auto_update === 'boolean' ? snapshot.auto_update : null,
      checkedAt: update.fetched_at,
    });
  const busy = sending !== null || restarting || installRunning(snapshot);
  return {
    model,
    target: offered,
    canUpdate: can && takes('install') && !busy,
    canSet: { prereleases: can && takes('prereleases'), autoUpdate: can && takes('auto_update') },
    busy,
  };
}
