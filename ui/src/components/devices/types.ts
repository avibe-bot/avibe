// The data the Devices views draw (ui/design: settings/devices/*, settings/devices-connection/*,
// setup/remote-access/*). Locale-neutral: names a person gave stay as given, times are ISO
// instants, and everything the product says is chosen by kind, so the copy lives in `src/i18n`.
// The containers map the APIs onto these shapes (`./wire.ts`); the views fetch nothing.
// Whatever an older or newer instance answers that this page does not know is carried as
// `unknown`, and said as unknown: never as success.
import type { RemoteAccessSettings, TunnelNetworkInterface } from '@/context/ApiContext';
import type { DeviceUsing } from '@/lib/remoteAccessDirect';

/** The paths a device reaches this Avibe on, by their shipped names (`remoteAccess.direct.using*`). */
export type PathKind = 'tunnel' | 'lan' | 'punched' | 'relay';
export type IpFamily = 'ipv4' | 'ipv6';

/** `other`: a kind this page does not know yet. It is still a device that can open this Avibe. */
export type DeviceKind = 'host' | 'iphone' | 'desktop' | 'browser' | 'other';
/** What runs on the device: the Avibe runtime itself, the Avibe App, Avibe Desktop, a browser tab,
 *  or a browser page added to the home screen. */
export type ClientKind = 'runtime' | 'app' | 'desktop' | 'browser' | 'homeScreen' | 'other';
export type DeviceClient = { kind: ClientKind; version?: string; platform?: string };

export type HealthData = 'steps' | 'activity' | 'workouts' | 'sleep';

/** What a device offers. `canChange`: whether this viewer may switch it (the instance decides;
 *  the page never infers it from the group or the kind). */
export type DeviceFunction =
  | { kind: 'notifications' | 'schedule'; on: boolean; canChange: boolean }
  | { kind: 'health'; on: boolean; canChange: boolean; data: HealthData[]; syncedAt?: string }
  /** The computer that runs Avibe only, and never changed from this page. */
  | { kind: 'computerUse'; status: 'ready' | 'needs_permission' | 'off' | 'unavailable' };
export type FunctionKind = DeviceFunction['kind'];

export type DeviceConnection = { path?: PathKind; family?: IpFamily; ms?: number };

/** Which list a device belongs to, as `/api/devices` groups it: the viewer's own devices, other
 *  people's (owner only), and push rows whose origin cannot be shown (owner only). */
export type DeviceGroup = 'mine' | 'others' | 'unattributed';

export type Device = {
  id: string;
  group: DeviceGroup;
  kind: DeviceKind;
  /** The device's own name as reported; null when it is not known (the page names its kind). */
  name: string | null;
  client: DeviceClient;
  /** The device this page is open on. */
  self?: boolean;
  /** A connection observed right now, and its path; absent without one. */
  online?: DeviceConnection;
  /** When it was last here, shown while no connection is observed. */
  lastActiveAt?: string;
  signedInAt?: string;
  functions: DeviceFunction[];
  /** The account it is signed in as, when the instance says. */
  account?: string;
  /** Someone else's device (`group: 'others'`): their account, as the row names it. */
  person?: string;
  /** No sign-in left on it, only a notification registration: signing it out stops those. */
  pushOnly?: boolean;
  /** Only on the computer that runs Avibe, and only the instance's outright answer about an Avibe
   *  that is not bound (`network_open` of its row). `true`: other devices on its network open it
   *  without signing in, through a listener Avibe itself can close. `false`: only this computer
   *  opens it. Absent when the instance does not say, says null (not known, or not Avibe's to
   *  close), or is bound: never filled in from a missing answer. */
  networkOpen?: boolean;
  recent?: DeviceActivity[];
};

export type ActivityEvent =
  | { kind: 'signedIn' }
  /** `actor`: the device it was signed out from, when this viewer may know it. */
  | { kind: 'signedOut'; actor?: string | null }
  | { kind: 'healthRead'; data: HealthData[]; fromDay: string; toDay: string; task?: string }
  /** `label` only for the owner of the device it was created on. */
  | { kind: 'alarmCreated' | 'reminderCreated'; confirmed: boolean; label?: string }
  | { kind: 'remoteAccessOn' | 'remoteAccessOff' };

export type DeviceActivity = {
  id: string;
  /** The device it happened on, by the list's own id. */
  deviceId?: string;
  at: string;
  /** The device's name; null when it is no longer in the list. */
  device: string | null;
  event: ActivityEvent;
};

/** A Health connection no listed device can be proven to own. */
export type UnlinkedHealth = { id: string; device: string; data: HealthData[]; lastSeenAt?: string };

/** What avibe.bot did about a signed-out device's account sign-in (`status/reason` of the
 *  contract); anything this page does not know is `unknown`. */
export type AccountResult =
  | 'queued' | 'retrying' | 'accepted'
  | 'localOnly' | 'sharedSignIn' | 'keeperUnknown'
  | 'refusedCredential' | 'refused' | 'unconfirmed' | 'unknown';

/** A device signed out lately: it is out of this Avibe; the account side is its own story. */
export type SignedOutDevice = {
  id: string;
  deviceId?: string;
  device: string | null;
  kind: DeviceKind | null;
  /** Whose device it was, where that is known without guessing; it decides which advice applies. */
  owner: 'mine' | 'others' | 'unknown';
  results: AccountResult[];
};

/** The answer of a sign-out just made. `healthCleanupPending`: a conditional clean-up of synced
 *  Health data is not confirmed yet (it does not say the device had any). */
export type SignOutNotice = {
  device: string | null;
  owner: 'mine' | 'others' | 'unknown';
  results: AccountResult[];
  healthCleanupPending: boolean;
  /** The signed-out row showed Health on this page. */
  hadHealth: boolean;
  /** The row listed what the device does and Health was not among it: nothing is said about
   *  Health's clean-up, which cannot concern it. */
  withoutHealth?: boolean;
};

/** Why a claim (turning remote access on) did not go through: the local outcome names of
 *  `personal-installation-claims-runtime.md`, and `unknown` with the raw code for the rest. */
export type ClaimProblem =
  | { code: 'link_required' | 'organization' | 'invalid_return' | 'expired' | 'declined' | 'unreachable'
      | 'pairing_recovery_required' | 'fresh_claim_required' | 'name_taken' | 'binding_unavailable'
      | 'paired_installation_loopback_required' | 'active_pairing_recent' | 'local_binding_changed' }
  | { code: 'claim_in_progress'; claimId?: string }
  /** Refused at avibe.bot by another account: `email` is the account that confirmed it, never the Owner. */
  | { code: 'other_account'; email: string }
  | { code: 'rate_limited'; seconds?: number }
  | { code: 'unknown'; raw: string };

/** How this Avibe is reached. */
export type Reach =
  /** Not bound, in a browser: one action, the name is chosen at avibe.bot. */
  | { state: 'off'; surface: 'browser'; busy?: boolean; problem?: ClaimProblem }
  /** The Desktop App on this computer: one action, which asks the shell for its claim dialog (the
   *  App's own sign-in if needed, then the name). `busy`: that request was just sent. */
  | { state: 'off'; surface: 'desktop'; busy?: boolean }
  /** Turning on. `waiting`: the request is open and not approved at avibe.bot yet; it can be
   *  cancelled. `retrying`: open, and the instance is trying avibe.bot again after it could not be
   *  reached or asked to wait. Otherwise avibe.bot approved and this computer is binding. */
  | { state: 'starting'; waiting?: boolean; retrying?: boolean; busy?: boolean }
  /** Bound, and this browser stopped being this computer the moment it was: it opened Avibe at a
   *  private-network address, which now needs a sign-in. `host` only when the local API said it. */
  | { state: 'moved'; host?: string }
  | { state: 'on'; host: string; account?: string; tunnel: TunnelStatus['state']; ms?: number; direct?: boolean; busy?: boolean }
  /** Bound, switched off: the domain is kept. */
  | { state: 'paused'; host: string; account?: string; busy?: boolean }
  /** The state is being read for the first time. */
  | { state: 'loading' }
  /** The state could not be read. */
  | { state: 'unknown' };


export type AddKind = 'iphone' | 'android' | 'computer';
/** `remove`: sign the open device out. `health-off`: turn the open device's Health off.
 *  `remove-all-remote`: at the computer itself, every remote device. */
export type DevicesConfirm = 'remove' | 'health-off' | 'turn-off' | 'remove-others' | 'remove-all-remote' | 'this-computer-only' | { unlinkedHealth: string };

/** Addresses an operator's own reverse proxy serves, whose visitors this Avibe lets in without
 *  signing in (`unauthenticated_proxy_access` of `/api/devices`, the Owner only). `range`: the
 *  trusted proxy is declared as more than one address. */
export type ProxyAccess = { origins: string[]; peerScope: 'single_address' | 'range' };

/** The instance's direct state (`GET /api/devices/connection`). */
export type DirectState = 'ready' | 'off' | 'awaiting-permission' | 'denied' | 'awaiting-certificate' | 'not-running' | 'unknown';

/** This device's own path choice, in the Avibe App or a Desktop window of another computer's
 *  instance (`direct.preference()`, capability `directPreference`). */
export type DirectPreference = {
  mode: 'automatic' | 'tunnel' | 'direct';
  /** The shell put `from` back to `automatic` by itself because it could not connect. */
  reverted: null | { at: string; reason: 'unreachable'; from: 'tunnel' | 'direct' };
  /** The App takes the choice only after it is opened again. */
  pendingRelaunch?: boolean;
};

export type TunnelSetting = 'protocol' | 'ipFamily' | 'outbound';
export type ConnectionSetting = TunnelSetting | 'direct' | 'relay' | 'autoRecovery' | 'profile';

/** The latest change of a tunnel setting, as `operation` / `outcome` report it, or an answer that
 *  never arrived. `from`: the value it was, as the instance stores it. */
export type TunnelChange = { /** The value it was changed to, as the instance stores it, when it says. */ to?: string } & (
  | { setting: TunnelSetting; state: 'applying' | 'verifying' }
  | { setting: TunnelSetting; state: 'restoring'; from: string }
  /** `saved`: stored while no tunnel was running, so it was not tried: saved, not yet in effect. */
  | { setting: TunnelSetting; state: 'applied'; saved?: boolean }
  | { setting: TunnelSetting; state: 'superseded' }
  | { setting: TunnelSetting; state: 'restored'; reason: 'verification_failed' | 'verification_unavailable' | 'unknown'; from: string }
  | {
    setting: TunnelSetting; state: 'failed'; from: string;
    reason: 'saved_restored_runtime_pending' | 'storage_restore_failed' | 'candidate_not_ready' | 'connection_change_busy' | 'settings_not_applied' | 'unknown';
  }
  /** The answer was lost: the result is read again, the change is never sent twice. */
  | { setting: TunnelSetting; state: 'unknown' }
);

export type RelayState = {
  enabled: boolean;
  connected: boolean;
  /** Why it cannot be turned on now; the switch is then not offered. */
  unavailable?: 'direct_permission_required' | 'relay_transport_unavailable' | 'unknown';
};

export type ConnectionState = {
  settings: RemoteAccessSettings;
  interfaces: TunnelNetworkInterface[];
  direct: DirectState;
  /** Absent unless this instance is enrolled in the relay trial. */
  relay?: RelayState;
  change?: TunnelChange;
  /** Fixed values that could not connect for three minutes went back to automatic: exactly these. */
  returnedToAutomatic?: { at: string; changes: Array<{ setting: TunnelSetting; from: string }> };
  /** The tunnel runs on another combination for now while the saved settings are unchanged. */
  temporaryRoute?: { protocol: 'quic' | 'http2' | null; differences: TunnelSetting[] };
  /** Direct or the relay was asked to change and the instance has not written it yet (`202`:
   *  accepted, not applied). The row waits; the state says how it ended. */
  switching?: { setting: SwitchSetting; id?: string };
  /** How the last change of direct or the relay ended, by the instance's own record. */
  switched?: { setting: SwitchSetting; id?: string; failed: boolean; reason: string };
};

/** The two settings the instance changes after it has answered. */
export type SwitchSetting = 'direct' | 'relay';

/** 「优化线路」: evaluating now (`busy`), not pressable for a moment (`disabled`), what came of the
 *  last press (`problem`: why it did not start), and when it can be or will be tried again. */
export type OptimizeState = {
  busy?: boolean;
  disabled?: boolean;
  started?: boolean;
  problem?: string;
  wait?: { kind: 'manual' | 'automatic'; time: string } | { kind: 'due' };
};

/** A switch refused before anything changed. */
export type SwitchRefusal = { row: 'direct' | 'relay' | 'autoRecovery' | 'profile'; reason: 'tunnel_unhealthy' | 'connection_change_busy' | 'unknown' };

/**
 * The tunnel as the status reports it: its round trip, and the protocol in use. `down` only when
 * there is in fact no connection (not running, no connection registered, or requests through it
 * failing). A connected tunnel that is slow is `slow`; one short of its connections, with errors,
 * or still settling after a recovery is `unsteady`; one being evaluated for a better route is
 * `optimizing`, connected like the others. What cannot be read is `unknown`.
 */
export type TunnelStatus = {
  /** `connecting`: started, and nothing read from it yet that says either way. `connected`: at least
   *  one connection, its round trip not measured yet. */
  state: 'up' | 'slow' | 'unsteady' | 'optimizing' | 'connected' | 'connecting' | 'down' | 'unknown';
  ms?: number;
  active?: 'quic' | 'http2' | null;
};

// ── Changing the domain (docs/plans/instance-address-change.md) ────────────────────────────────

export type RenameDeviceKind = DeviceKind | 'homeScreen';
/** A device in the list shown before and after a rename. */
export type RenameDevice = {
  id: string;
  name: string | null;
  kind: RenameDeviceKind;
  self?: boolean;
  pushOnly?: boolean;
  /** After the rename only. */
  cleanup?: 'cleared' | 'unconfirmed';
  lastActiveAt?: string;
  /** The account it was signed in as, when the instance says. */
  account?: string;
  /** Someone else's device: their account, where the page can tell without guessing. */
  person?: string;
};

/** A name this Avibe gave up and still cleans up after: this account can take it back until
 *  `releaseAt` (the real day; absent when avibe.bot gave none). */
export type RetiredName = { hostname: string; releaseAt?: string; devices: RenameDevice[] };

export type RenameRefusal =
  | { code: 'taken' | 'invalid_prefix' | 'same_address' | 'confirmation_expired' | 'operation_in_progress'
      | 'address_changed' | 'pairing_invalid' | 'remote_access_configuration_mismatch' | 'rebind_required' | 'unavailable'
      | 'owner_required' | 'rename_ineligible' }
  /** The account holds as many names as it may: the instance passes the numbers through. */
  | { code: 'name_holding_limit_reached'; held?: number; earliestReleaseAt?: string }
  | { code: 'unknown'; raw: string };

export type RenameStep =
  /** The new name, what happens, the devices. `earlier`: the refusal is of a change found
   *  unfinished when the page was opened again, not of something just pressed. */
  | { step: 'form'; name: string; busy?: boolean; refusal?: RenameRefusal; earlier?: boolean }
  /** The second question; its button waits `countdown` seconds. */
  | {
    step: 'confirm'; name: string; hostname: string; countdown: number; busy?: boolean; refusal?: RenameRefusal;
    isReturn: boolean; releaseAt?: string; holdDays?: number; healthConnected: boolean; devices: RenameDevice[];
  }
  /** avibe.bot has not answered yet: the change is still running. */
  | { step: 'pending'; hostname: string }
  /** Afterwards, at this computer: the list for the name just given up (`previous`). */
  | { step: 'done'; hostname: string; previous: string; retired: RetiredName | null }
  /** Later: the same list for a name given up earlier. */
  | { step: 'cleanup'; retired: RetiredName };

export type AddressState = {
  hostname: string;
  eligible: boolean;
  ineligibleReason?: string;
  /** avibe.bot holds more than one binding record for this computer and cannot tell which to use:
   *  the name's state cannot be read until this computer is bound again. */
  stateError?: 'rebind_required';
  holdDays?: number;
  /** The real day the current name would be released if it were changed now. */
  releaseAtIfChangedNow?: string;
  /** A change left unfinished or failed, found when the page is opened again. */
  operation?: { state: 'pending' | 'applying' | 'failed'; hostname: string; error?: RenameRefusal };
  /** The address was changed somewhere else, with this computer's device key. */
  unrecognizedChange?: { previousHostname: string; hostname: string };
  retired: RetiredName[];
  healthConnected: boolean;
};

// ── Binding remote access again (docs/plans/local-remote-access-rebind.md) ──────────────────────

export type RebindRefusal =
  | { code: 'active_pairing_recent' | 'binding_unavailable' | 'issuer_replay_refused' | 'expired' | 'declined'
      | 'unreachable' | 'local_binding_changed' | 'operation_in_progress' | 'claim_in_progress' | 'confirmation_expired'
      | 'invalid_return' }
  | { code: 'rate_limited'; seconds?: number }
  | { code: 'unknown'; raw: string };

/** What this page can say about binding again. */
export type RebindState =
  /** Not this computer (or not its owner at it): said, never offered. */
  | { state: 'remote' }
  | { state: 'unpaired' }
  /** Bound: the quiet row. `rejected`: avibe.bot has refused this binding at least twice. */
  | { state: 'ready'; rejected?: boolean; tunnelRunning?: boolean; replayRefused?: boolean; refusal?: RebindRefusal; busy?: boolean }
  /** An earlier attempt may have completed at avibe.bot: it continues, no second one starts. */
  | { state: 'recovering' }
  /** A change of binding or domain is running. */
  | { state: 'busy' }
  /** avibe.bot refused in one of the two ways that allow binding as a new computer.
   *  `hint`: whose Avibe it is, masked, when it is another account's. */
  | { state: 'asNewOffered'; reason: 'predecessor_unrecoverable' | 'other_account'; hint?: string; busy?: boolean; refusal?: RebindRefusal }
  /** A reset was started and has not finished: Avibe continues it. */
  | { state: 'resetting' }
  /** Something this page cannot handle: the terminal command can. */
  | { state: 'needsTerminal' }
  | { state: 'unknown' };

/** Open dialogs of the bind-again row. */
export type RebindDialog =
  | { kind: 'confirm' }
  /** Binding as a new computer, clearing this computer only. `afterRefusal`: avibe.bot did not
   *  accept deleting the old binding (its key no longer works), so only this computer is cleared. */
  | { kind: 'asNew'; healthConnected: boolean; afterRefusal?: boolean }
  /** Binding as a new computer by first deleting the old binding at avibe.bot (the instance's new
   *  preview). `releaseAt` / `holdDays`: how long the name stays the original account's. */
  | { kind: 'asNewCloud'; healthConnected: boolean; releaseAt?: string; holdDays?: number }
  /** The reset finished: this computer is no longer bound. `oldHost`: the name it had. */
  | { kind: 'resetDone'; oldHost?: string }
  /** Bound again. */
  | { kind: 'bound'; disposition: 'existing' | 'new' | 'unknown'; host?: string };

// ── 解除绑定 (docs: the instance's unbind states) ───────────────────────────────────────────────

/** What 连接方式 says about unbinding this computer. `ready`: the entry is offered. `unbinding`: the
 *  instance is doing it and continues by itself. `notChanged`: avibe.bot did not accept it and
 *  remote access is as it was before. `needsRepair`: only this computer can sort it out. */
export type UnbindState =
  | { state: 'ready' }
  | { state: 'unbinding' }
  | { state: 'notChanged'; reason: 'ineligible' | 'refused' }
  | { state: 'needsRepair' };

/** The confirmation, open. `remote`: pressed from a device away from the computer, which loses this
 *  page at once. `releaseAt` / `holdDays`: how long the name stays the account's, as the instance
 *  says. */
export type UnbindConfirmState = {
  remote: boolean;
  /** The computer's name as the Devices page lists it. Pressed from another device, the sentences
   *  name the computer rather than say 「这台电脑」 / 「那台电脑」. */
  computer?: string;
  healthConnected: boolean;
  releaseAt?: string;
  holdDays?: number;
  busy?: boolean;
};

export type { DeviceUsing };
