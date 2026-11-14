import type { DesktopUpdateState } from '../lib/desktopShell';

export type Account = { id: string; email: string };
export type Instance = {
  id: string; name: string; host: string; url: string;
  relationship: 'owner' | 'shared'; owner_email: string | null; available: boolean;
};
export type ClientTarget =
  | { kind: 'local'; name: string; instance_id: string | null }
  | { kind: 'remote'; name: string; instance_id: string; canonical_origin: string };
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
  local: { name: string; remote_access: 'off' | 'starting' | 'on' | 'unavailable' };
  desktop: { version: string; update: DesktopUpdateState };
  consent: null | { instance_id: string; instance_name: string; host: string; scope: string };
  remote_auxiliary: 'local' | 'disabled_incomplete';
};
/** Workbench receives presentation only; authentication/enrollment details stay bundled. */
export type DesktopClientProjection = Pick<DesktopClientState,
  'version' | 'revision' | 'generation' | 'connection' | 'local' | 'desktop' | 'remote_auxiliary'> & {
    account: Pick<DesktopClientState['account'], 'status' | 'identity'>;
  };
export type DesktopClientAction =
  | { type: 'get_state' }
  | { type: 'request_otp'; email: string }
  | { type: 'verify_otp'; code: string }
  | { type: 'verify_second_factor'; method: 'totp' | 'recovery'; code: string }
  | { type: 'verify_passkey' }
  | { type: 'begin_totp_enrollment' }
  | { type: 'finish_totp_enrollment'; code: string }
  | { type: 'refresh_instances' }
  | { type: 'retry_enrollment' }
  | { type: 'retry_local_remote_access' }
  | { type: 'select_instance'; instance_id: string }
  | { type: 'decide_consent'; approve: boolean }
  | { type: 'cancel' }
  | { type: 'return_local' }
  | { type: 'sign_out' }
  // This computer's desktop updater, from the trusted update view (ui/design: update/panel/trusted-view).
  | { type: 'update_check' }
  | { type: 'update_set_prereleases'; enabled: boolean }
  | { type: 'update_set_auto_update'; enabled: boolean }
  | { type: 'update_install'; target_version: string };
export type DesktopClientRequest = {
  version: 1; request_id: string; expected_generation: number; action: DesktopClientAction;
};
export type DesktopClientDirectResult =
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
/** The caller owns the navigation guard. This URL only presents a trusted page. A native request
 *  carries its `presentationToken` back unchanged, so native can match the approval to that request. */
export function presentDesktopClient(view: DesktopClientView | 'updates', presentationToken?: string): boolean {
  if (!hasDesktopClient() || (view !== 'updates' && !isDesktopClientView(view))) return false;
  window.location.assign(presentationToken !== undefined
    ? `avibe://desktop/${view}?native_request=${encodeURIComponent(presentationToken)}`
    : view === 'updates' ? 'avibe://updates' : `avibe://desktop/${view}`);
  return true;
}
