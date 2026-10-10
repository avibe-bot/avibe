import { hasConfiguredPlatformCredentials } from './platforms';

/**
 * Whether first-run setup is complete, from a `GET /api/config` payload. The
 * one rule `AuthGuard` and the desktop pet both apply: the server's
 * `setup_state.needs_setup` when present, otherwise configured platform
 * credentials, and always a chosen mode.
 */
type SetupConfig = { mode?: unknown; setup_state?: { needs_setup?: unknown } | null } | null | undefined;

export const isSetupComplete = (config: SetupConfig): boolean => {
  if (!config || !config.mode) return false;
  const setupState = config.setup_state;
  return typeof setupState?.needs_setup === 'boolean'
    ? setupState.needs_setup === false
    : hasConfiguredPlatformCredentials(config);
};
