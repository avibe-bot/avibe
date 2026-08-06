// Whether a migration scan may run at all. Gated on the Model Hub capability so a
// disabled instance issues no scan request; separated from MigrationBanner so the
// gate is testable without rendering the strip.
import { modelsApi } from './modelsApi';

export const scanMigrationWhenEnabled = async (
  enabled: boolean,
  scan: typeof modelsApi.scanMigration = modelsApi.scanMigration,
) => (enabled ? scan() : null);
