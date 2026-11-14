// The update snapshot every surface reads, kept free of imports so the desktop app's
// bundled views can share the update panel without the Workbench's API client.

export type DesktopRemoteUpdatePhase =
  | 'requested'
  | 'acknowledged'
  | 'checking'
  | 'awaiting_host'
  | 'downloading'
  | 'installing'
  | 'restarting'
  | 'completed'
  | 'failed'
  | 'declined'
  | 'expired';

export type DesktopRemoteUpdateRequest = {
  request_id: string;
  target: string;
  phase: DesktopRemoteUpdatePhase;
  progress: number | null;
  error_code: string | null;
  updated_at: string;
};

export type UpdateHostAction = 'check' | 'prereleases' | 'auto_update' | 'install';

/** The receipt of a check or a setting change asked of the host's updater. */
export type UpdateControlRequest = {
  request_id: string;
  action: 'check' | 'prereleases' | 'auto_update';
  enabled: boolean | null;
  phase: 'checking' | 'completed' | 'failed' | 'expired';
  error_code: string | null;
  updated_at: string;
};

/** What `/api/version` reports (vibe/api.py `get_version_info`). */
export type UpdateSnapshot = {
  current: string;
  latest: string | null;
  has_update: boolean;
  error: string | null;
  managed_by?: 'desktop';
  /** Desktop-managed hosts: the desktop app's last completed check, relayed (vibe/desktop_update_status.py);
   *  `checking` while a check asked for from here runs. */
  phase?: 'current' | 'available' | 'disabled' | 'error' | 'checking' | null;
  desktop_version?: string | null;
  prereleases?: boolean | null;
  auto_update?: boolean | null;
  checked_at?: string | null;
  /** What this host's updater accepts from here; absent from a desktop app too old to accept any. */
  host_actions?: UpdateHostAction[];
  control_request?: UpdateControlRequest | null;
  remote_install_allowed?: boolean;
  remote_request?: DesktopRemoteUpdateRequest | null;
  build?: {
    kind: 'package' | 'source';
    revision?: string;
    dirty?: boolean;
  };
};
