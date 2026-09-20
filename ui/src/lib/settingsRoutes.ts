export type LegacySettingsRedirect = {
  from: string;
  to: string;
};

export const legacySettingsRedirectTarget = (to: string, hash: string): string =>
  hash && !to.includes('#') ? `${to}${hash}` : to;

// Keep every retired route as a client-side redirect so bookmarks, PWA state,
// and links from older releases all arrive at the canonical Settings surface.
export const LEGACY_SETTINGS_REDIRECTS: LegacySettingsRedirect[] = [
  { from: '/admin', to: '/settings' },
  { from: '/admin/dashboard', to: '/settings/service' },
  { from: '/admin/permissions', to: '/settings/access' },
  { from: '/admin/remote-access', to: '/settings/remote-access' },
  { from: '/admin/groups', to: '/settings/platforms/groups' },
  { from: '/admin/users', to: '/settings/platforms/users' },
  { from: '/admin/show-pages', to: '/apps/library?view=pages' },
  { from: '/admin/logs', to: '/settings/diagnostics/logs' },
  { from: '/admin/settings/service', to: '/settings/service' },
  { from: '/admin/settings/platforms', to: '/settings/platforms' },
  { from: '/admin/settings/backends', to: '/settings/backends' },
  { from: '/admin/settings/backends/opencode', to: '/settings/backends/opencode' },
  { from: '/admin/settings/backends/claude', to: '/settings/backends/claude' },
  { from: '/admin/settings/backends/codex', to: '/settings/backends/codex' },
  { from: '/admin/settings/models', to: '/settings/models' },
  { from: '/admin/settings/dependencies', to: '/settings/dependencies' },
  { from: '/admin/settings/memory', to: '/settings/memory' },
  { from: '/admin/settings/messaging', to: '/settings/replies' },
  { from: '/admin/settings/diagnostics', to: '/settings/diagnostics' },
  { from: '/admin/settings/logs', to: '/settings/diagnostics/logs' },
  { from: '/dashboard', to: '/settings/service' },
  { from: '/groups', to: '/settings/platforms/groups' },
  { from: '/channels', to: '/settings/platforms/groups' },
  { from: '/users', to: '/settings/platforms/users' },
  { from: '/logs', to: '/settings/diagnostics/logs' },
  { from: '/settings/messaging', to: '/settings/replies' },
  { from: '/settings/logs', to: '/settings/diagnostics/logs' },
  { from: '/remote-access', to: '/settings/remote-access' },
  { from: '/doctor', to: '/settings/diagnostics' },
  { from: '/doctor/logs', to: '/settings/diagnostics/logs' },
];

const LEGACY_SETTINGS_ENTRY_PATHS = new Set(
  LEGACY_SETTINGS_REDIRECTS
    .filter(({ to }) => to === '/settings' || to.startsWith('/settings/'))
    .map(({ from }) => from),
);

export const isLegacySettingsEntryPath = (pathname: string): boolean =>
  LEGACY_SETTINGS_ENTRY_PATHS.has(pathname);
