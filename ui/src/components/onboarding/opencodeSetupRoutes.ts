import type { ApiContextType } from '@/context/ApiContext';
import { modelsApi } from '../settings/models/modelsApi';
import { modelHubEnabledFromConfig } from '../settings/models/featureFlags';
import { catalogModelIds } from '../settings/models/backendCatalog';

/** Routing eligibility is separate from backend credential/application readiness. */
export async function readOpencodeSetupRoutes(api: ApiContextType) {
  const config = await api.getConfig();
  if (modelHubEnabledFromConfig(config)) {
    // Unlike a best-effort picker read, an unreadable active supply cannot
    // silently become Direct mode during explicit setup completion.
    const supply = await modelsApi.getAgentSources('opencode');
    if (supply.mode === 'hub') {
      const catalog = new Set(catalogModelIds(supply));
      const supplied = new Set(supply.model_supply?.filter((row) => row.has_runnable_hop).map((row) => row.model_id));
      return { mode: 'hub' as const, accepts: (model: string | null) => Boolean(model && catalog.has(model) && supplied.has(model)) };
    }
  }
  const result = await api.getOpencodeProviders();
  if (!result.ok) throw new Error(result.message);
  const connected = new Set(result.providers?.filter((provider) => provider.active_auth_type === 'api' || provider.active_auth_type === 'oauth').map((provider) => provider.id));
  return {
    mode: 'direct' as const,
    accepts: (model: string | null) => {
      if (!model) return false;
      const slash = model.indexOf('/');
      const provider = slash >= 0 ? model.slice(0, slash) : result.default_provider;
      return Boolean(provider && connected.has(provider) && (slash < 0 || model.slice(slash + 1)));
    },
  };
}
