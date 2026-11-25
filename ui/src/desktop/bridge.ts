import type { DesktopUpdateState } from '../lib/desktopShell';
import type { UpdateSnapshot } from '../components/update/types';

export type Account = { id: string; email: string };
export type Instance = {
  id: string; name: string; host: string; url: string;
  relationship: 'owner' | 'shared'; owner_email: string | null; available: boolean;
};
export type ClientTarget =
  | { kind: 'local'; name: string; instance_id: string | null }
  | { kind: 'remote'; name: string; instance_id: string; canonical_origin: string };
/** A standalone Runtime's install in progress (its `/api/version` `install`); absent from older instances. */
export type InstanceInstallProgress = {
  phase: 'installing' | 'restarting';
  target: string | null;
  started_at: string;
};
/** The committed remote instance's update state, read and acted on by the shell itself
 *  (docs/plans/2026-11-20-update-panel-instance-and-app.md, B1). Display data only. */
export type InstanceUpdateState = {
  instance_id: string;
  status: 'loading' | 'ready' | 'unreachable' | 'unauthorized';
  /** The instance's `/api/version`, allow-listed. Kept from the last valid read while `unreachable`. */
  snapshot: (UpdateSnapshot & { install?: InstanceInstallProgress | null }) | null;
  /** The instance's `/api/session` `capabilities.can_manage_instance`. */
  can_manage: boolean;
  /** The last action this shell sent to the instance. */
  request: null | {
    action: 'install' | 'check' | 'prereleases' | 'auto_update';
    phase: 'sending' | 'accepted' | 'failed';
    target: string | null;
    error_code: string | null;
    updated_at: string;
    /** The shell would refuse another action for this instance now because of this request
     *  (`update_in_progress`): it is still on its way, or still being followed. Offer the
     *  instance's Update, check and switches exactly when this is false. */
    following: boolean;
    /** An install only: the version the instance ran when the install was accepted. A version,
     *  validated like every other; null for other actions, and when the shell does not know it. */
    installed_from: string | null;
  };
  fetched_at: string | null;
};
export type DesktopClientState = {
  version: 1; revision: number; generation: number;
  account: {
    status: 'signed_out' | 'verifying' | 'second_factor_required' | 'signed_in';
    identity: Account | null; email: string | null;
    gate: 'challenge' | 'enroll' | null; methods: ('passkey' | 'totp')[];
  };
  connection: {
    status: 'local' | 'switching' | 'remote_connected' | 'remote_unreachable';
    committed: ClientTarget; pending: ClientTarget | null;
  };
  enrollment: {
    status: 'unknown' | 'unpaired' | 'registering' | 'paired_this_account' | 'conflict' | 'failed' | 'paired_tunnel_unavailable';
    instance_id: string | null; error_code: string | null;
  };
  catalog: { status: 'idle' | 'loading' | 'ready' | 'error'; instances: Instance[] };
  /** Display proof for the committed remote; never used to authorize a switch. */
  current_instance?: Instance | null;
  local: { name: string; instance_id?: string | null; slug?: string | null; remote_access_url?: string | null; remote_access: 'off' | 'starting' | 'on' | 'unavailable';
    /** Avibe on this computer is too old to say what it is bound to: 升级并重试 on its row. */
    runtime_too_old?: boolean };
  desktop: { version: string; update: DesktopUpdateState };
  /** Null while the committed connection is local. Never in the Workbench projection. */
  instance_update: InstanceUpdateState | null;
  consent: null | { instance_id: string; instance_name: string; host: string; scope: string };
  remote_auxiliary: 'local' | 'disabled_incomplete';
  /** avibe.bot's public sign-in switches as native last read them; absent from older shells. */
  sign_in?: { providers: ('google' | 'github' | 'apple')[]; passkey: boolean };
};
/** Workbench receives presentation only; authentication/enrollment details stay bundled. */
export type DesktopClientProjection = Pick<DesktopClientState,
  'version' | 'revision' | 'generation' | 'connection' | 'local' | 'desktop' | 'remote_auxiliary'> & {
    account: Pick<DesktopClientState['account'], 'status' | 'identity'>;
  };
export type DesktopClientAction =
  | { type: 'get_state' }
  | { type: 'request_otp'; email: string }
  | { type: 'sign_in_password'; email: string; password: string }
  | { type: 'verify_otp'; code: string }
  | { type: 'verify_second_factor'; method: 'totp' | 'recovery'; code: string }
  | { type: 'verify_passkey' }
  // Google, GitHub or Apple in the default browser, back through loopback PKCE.
  | { type: 'sign_in_with_provider'; provider: 'google' | 'github' | 'apple'; locale?: 'en' | 'zh' }
  // Passkey sign-in with no email first (not the second-factor passkey).
  | { type: 'sign_in_with_passkey' }
  // Ends an unfinished sign-in attempt without leaving the account window.
  | { type: 'abort_sign_in' }
  // Reads avibe.bot's public sign-in switches into `sign_in`.
  | { type: 'refresh_sign_in_methods' }
  | { type: 'begin_totp_enrollment' }
  | { type: 'finish_totp_enrollment'; code: string }
  | { type: 'refresh_instances' }
  | { type: 'retry_enrollment' }
  | { type: 'retry_local_remote_access' }
  /** 升级并重试 on this computer's row: what 版本与更新 does for Avibe on this computer. */
  | { type: 'upgrade_local_runtime' }
  | { type: 'select_instance'; instance_id: string }
  | { type: 'decide_consent'; approve: boolean }
  | { type: 'cancel' }
  | { type: 'return_local' }
  | { type: 'sign_out'; intent_id?: string }
  // This computer's desktop updater, from the trusted update view (ui/design: update/panel/trusted-view).
  | { type: 'update_check' }
  | { type: 'update_set_prereleases'; enabled: boolean }
  | { type: 'update_set_auto_update'; enabled: boolean }
  | { type: 'update_install'; target_version: string }
  // The selected remote instance's updater, from the same trusted views; the shell sends them with its own sign-in.
  | { type: 'instance_update_refresh' }
  | { type: 'instance_update_check' }
  | { type: 'instance_update_set_prereleases'; enabled: boolean }
  | { type: 'instance_update_set_auto_update'; enabled: boolean }
  | { type: 'instance_update_install'; target_version: string };
export type DesktopClientRequest = {
  version: 1; request_id: string; expected_generation: number; action: DesktopClientAction;
};
export type SignOutIntent = { intent_id: string; generation: number };
export type DesktopClientDirectResult =
  | ({ type: 'sign_out_intent' } & SignOutIntent)
  | { type: 'totp_enrollment'; uri: string; qrSvg: string; secret: string }
  | { type: 'totp_enrolled'; method: {
    id: string; kind: 'totp'; name: string; createdAt: string; lastUsedAt: string | null;
  }; recoveryCodes?: string[] };
export type DesktopClientReply =
  | { version: 1; request_id: string; ok: true; state: DesktopClientState; result?: DesktopClientDirectResult }
  | { version: 1; request_id: string; ok: false; state: DesktopClientState; error: { code: string; retry_after_seconds?: number } };

export const DESKTOP_CLIENT_STATE_EVENT = 'avibe:desktop-client-state';
export const DESKTOP_PRESENT_EVENT = 'avibe:desktop-present';
export const DESKTOP_CLIENT_VIEWS = ['account', 'instances', 'setup-complete', 'sign-out', 'updates'] as const;
export type DesktopClientView = typeof DESKTOP_CLIENT_VIEWS[number];
/** The claim view: only this computer's own Workbench asks for it (「开启远程访问」), never native
 *  with a token. It carries nothing but the locale; the shell decides what it shows. */
export const DESKTOP_CLAIM_VIEW = 'claim';
declare global {
  interface Window {
    readonly __AVIBE_DESKTOP_CLIENT_VERSION__?: number;
    readonly __AVIBE_DESKTOP_CLIENT_STATE__?: DesktopClientProjection | DesktopClientState;
  }
}
export function hasDesktopClient(): boolean {
  return typeof window !== 'undefined' && window.top === window && window.__AVIBE_DESKTOP_CLIENT_VERSION__ === 1;
}
export function isDesktopClientView(view: unknown): view is DesktopClientView {
  return typeof view === 'string' && DESKTOP_CLIENT_VIEWS.some((candidate) => candidate === view);
}
export function isClientProjection(value: unknown): value is DesktopClientProjection {
  if (!value || typeof value !== 'object') return false;
  const state = value as DesktopClientProjection;
  return state.version === 1 && Number.isSafeInteger(state.generation) && Number.isSafeInteger(state.revision)
    && !!state.account && !!state.connection?.committed && !!state.local && !!state.desktop?.update;
}
export function isClientState(value: unknown): value is DesktopClientState {
  return isClientProjection(value) && 'catalog' in value && 'enrollment' in value && 'consent' in value;
}
export function isNewerClientState(next: DesktopClientProjection, previous: DesktopClientProjection | null): boolean {
  return !previous || next.generation > previous.generation
    || (next.generation === previous.generation && next.revision >= previous.revision);
}
/** Asks the shell to show the claim dialog in its own document, in front of this Workbench. */
export function presentDesktopClaim(locale: 'en' | 'zh'): boolean {
  if (!hasDesktopClient()) return false;
  window.location.assign(`avibe://desktop/${DESKTOP_CLAIM_VIEW}?locale=${locale}`);
  return true;
}
/** The caller owns the navigation guard. Sign-out admits the A4 native intent; other views present trusted UI. A native request
 *  carries its `presentationToken` back unchanged, so native can match the approval to that request. */
export function presentDesktopClient(view: DesktopClientView | 'updates', presentationToken?: string, locale?: 'en' | 'zh'): boolean {
  if (!hasDesktopClient() || (view !== 'updates' && !isDesktopClientView(view))) return false;
  const query = [
    ...(presentationToken !== undefined ? [`native_request=${encodeURIComponent(presentationToken)}`] : []),
    ...(locale ? [`locale=${locale}`] : []),
  ].join('&');
  window.location.assign(query ? `avibe://desktop/${view}?${query}`
    : view === 'updates' ? 'avibe://updates' : `avibe://desktop/${view}`);
  return true;
}

/** Normalize absent metadata from older shells without inferring identity or ownership. */
export function withLocalIdentity<T extends DesktopClientProjection>(state: T): T {
  return { ...state,
    ...(isClientState(state) ? { current_instance: state.current_instance ?? null, instance_update: state.instance_update ?? null } : {}),
    local: { ...state.local,
    instance_id: typeof state.local.instance_id === 'string' ? state.local.instance_id : null,
    slug: typeof state.local.slug === 'string' ? state.local.slug : null,
    remote_access_url: typeof state.local.remote_access_url === 'string' ? state.local.remote_access_url : null,
  } };
}
export function desktopLocale(language: string | undefined): 'en' | 'zh' {
  return language?.toLowerCase().split(/[-_]/)[0] === 'zh' ? 'zh' : 'en';
}
