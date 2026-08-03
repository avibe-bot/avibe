export const isAdvancedSettingsPath = (pathname: string): boolean =>
  pathname.startsWith('/admin/settings') &&
  !pathname.startsWith('/admin/settings/platforms') &&
  !pathname.startsWith('/admin/settings/backends') &&
  !pathname.startsWith('/admin/settings/models') &&
  !pathname.startsWith('/admin/settings/memory');
