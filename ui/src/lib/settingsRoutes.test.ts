import { describe, expect, it } from 'vitest';

import { APPLICATION_ROUTE_PATHS } from './applicationRoutes';
import { legacySettingsRedirectTarget, LEGACY_SETTINGS_REDIRECTS } from './settingsRoutes';

describe('legacy Settings redirects', () => {
  it('translates every retired admin route exactly once', () => {
    const adminRoutes = APPLICATION_ROUTE_PATHS.filter((path) => path.startsWith('/admin'));
    const redirectedAdminRoutes = LEGACY_SETTINGS_REDIRECTS
      .map((redirect) => redirect.from)
      .filter((path) => path.startsWith('/admin'));

    expect(new Set(redirectedAdminRoutes)).toEqual(new Set(adminRoutes));
    expect(redirectedAdminRoutes).toHaveLength(new Set(redirectedAdminRoutes).size);
  });

  it('targets a declared application route while preserving optional query state', () => {
    const applicationRoutes = new Set<string>(APPLICATION_ROUTE_PATHS);
    expect(
      LEGACY_SETTINGS_REDIRECTS.every(({ to }) => applicationRoutes.has(to.split(/[?#]/, 1)[0]!)),
    ).toBe(true);
  });

  it('preserves retired anchors for destination-level compatibility handling', () => {
    expect(legacySettingsRedirectTarget('/settings/service', '#remote-access')).toBe(
      '/settings/service#remote-access',
    );
  });
});
