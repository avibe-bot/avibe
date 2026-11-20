// The data the Devices views draw (ui/design: settings/devices/*, settings/devices-connection/*,
// setup/remote-access/*). Locale-neutral: names a person gave stay as given, times are ISO
// instants, and everything the product says is chosen by kind, so the copy lives in `src/i18n`.
// The containers map the APIs onto these shapes; the views fetch nothing.
import type { RemoteAccessSettings, TunnelNetworkInterface } from '@/context/ApiContext';
import type { DeviceUsing } from '@/lib/remoteAccessDirect';

/** The paths a device reaches this Avibe on, by their shipped names (`remoteAccess.direct.using*`). */
export type PathKind = 'tunnel' | 'lan' | 'punched' | 'relay';
export type IpFamily = 'ipv4' | 'ipv6';

export type DeviceKind = 'host' | 'iphone' | 'desktop' | 'browser';
/** What runs on the device: the Avibe runtime itself, the Avibe App, Avibe Desktop, a browser tab,
 *  or a browser page added to the home screen. */
export type ClientKind = 'runtime' | 'app' | 'desktop' | 'browser' | 'homeScreen';
export type DeviceClient = { kind: ClientKind; version?: string; platform?: string };

export type HealthData = 'steps' | 'activity' | 'workouts' | 'sleep';

export type DeviceCapability =
  | { kind: 'notifications'; on: boolean }
  | { kind: 'health'; on: boolean; data: HealthData[]; syncedAt?: string }
  | { kind: 'schedule'; on: boolean }
  /** `fixed`: seen from another device, so it is changed only on that computer. */
  | { kind: 'computerUse'; on: boolean; fixed?: boolean };
export type CapabilityKind = DeviceCapability['kind'];

export type DeviceConnection = { path?: PathKind; family?: IpFamily; ms?: number };

export type Device = {
  id: string;
  kind: DeviceKind;
  /** The device's own name, as reported. */
  name: string;
  client: DeviceClient;
  /** The device this page is open on. */
  self?: boolean;
  /** Connected now, and on which path; absent when it is not connected. */
  online?: DeviceConnection;
  /** When it was last here, shown while it is not connected. */
  lastActiveAt?: string;
  signedInAt?: string;
  capabilities: DeviceCapability[];
  /** Someone else's device on a shared instance: their account. */
  person?: string;
  recent?: DeviceActivity[];
};

export type ActivityEvent =
  | { kind: 'signedIn'; device: string }
  | { kind: 'signedOut'; device: string }
  | { kind: 'healthRead'; days: number; data: HealthData[]; task?: string }
  | { kind: 'healthConnected'; device: string; data: HealthData[] }
  | { kind: 'alarmCreated'; label: string; confirmedOn?: string }
  | { kind: 'directOn' }
  | { kind: 'remoteAccessOn'; host: string };

export type DeviceActivity = {
  id: string;
  at: string;
  event: ActivityEvent;
  /** Where it happened, as the list's last column says it. */
  source?: string;
};

/** How this Avibe is reached. */
export type Reach =
  /** A plain browser on the computer that runs a standalone Runtime: the Runtime knows no account. */
  | { state: 'off'; surface: 'browser'; error?: 'unreachable' }
  /** The Desktop App on this computer: its own account sign-in is the sign-in. */
  | { state: 'off'; surface: 'desktop'; account: string | null; name: string; nameState?: NameState }
  /** Back from avibe.bot (or after the App's sign-in): turning on. */
  | { state: 'starting'; host: string; account: string }
  | { state: 'on'; host: string; account: string; tunnel: 'good' | 'down'; ms?: number; direct?: boolean }
  /** Bound to the account, switched off: the domain is kept. */
  | { state: 'paused'; host: string; account: string }
  /** This computer is bound to another account than the one that just confirmed. */
  | { state: 'conflict'; host: string; owner: string; account: string };

/** Whether the name typed for the domain is free. `taken` never suggests another name. */
export type NameState = 'free' | 'taken';

export type AddKind = 'iphone' | 'android' | 'computer';
export type DevicesConfirm = 'remove' | 'turn-off' | 'remove-others';

/** The instance's direct state, as `vibe remote direct status` reports it. */
export type DirectState = 'ready' | 'off' | 'awaiting-permission' | 'denied' | 'awaiting-certificate' | 'not-running';

/** The Avibe App's own path choice (`direct.preference()`, capability `directPreference` 1). */
export type DirectPreference = {
  mode: 'automatic' | 'tunnel';
  /** The App put `tunnel` back to `automatic` by itself because it could not connect. */
  reverted: null | { at: string; reason: 'unreachable' };
};

export type TunnelState = {
  /** Round trip through the tunnel. */
  ms?: number;
  settings: RemoteAccessSettings;
  /** The protocol in use when `transport_protocol` is automatic. */
  active?: 'quic' | 'http2' | null;
  interfaces: TunnelNetworkInterface[];
};

/** A connection setting changed remotely, on trial or put back. */
export type ConnectionChange =
  | { setting: 'protocol'; to: RemoteAccessSettings['transport_protocol']; from: RemoteAccessSettings['transport_protocol'] }
  | { setting: 'ipFamily'; to: RemoteAccessSettings['edge_ip_version']; from: RemoteAccessSettings['edge_ip_version'] }
  | { setting: 'profile'; to: RemoteAccessSettings['optimization_profile']; from: RemoteAccessSettings['optimization_profile'] }
  | { setting: 'outbound'; to: string; from: string }
  | { setting: 'autoRecovery' | 'direct' | 'relay'; to: boolean; from: boolean };

/** A remote change is kept only if the device that made it comes back within this many seconds. */
export const TRIAL_WINDOW_SECONDS = 30;

/** Changing the domain (owner, 2026-11-20: a plain rename, the consequences are the person's):
 *  `form` takes the new name and says what will happen, `confirm` asks a second time (its button
 *  waits `countdown` seconds), `done` is the page afterwards. `voidDays`: how long the old name is
 *  held before someone else may claim it. */
export type Rename =
  | { step: 'form'; name: string; nameState?: NameState; voidDays: number }
  | { step: 'confirm'; name: string; countdown: number }
  | { step: 'done'; from: string };

export type { DeviceUsing };
