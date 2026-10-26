import type { HostedSearchServiceVendor, HostedToolsStatus, HostedWebSearchToolStatus, Source, SourceState } from './types';

export const KEYLESS_EXA_SOURCE_ID = 'src_exakeyless';
export const KEYLESS_EXA_VENDOR_ID = 'exa-mcp';

export const keylessExaSource = (candidate?: HostedWebSearchToolStatus['executor_candidates'][number], vendor?: HostedSearchServiceVendor): Source => ({
  id: KEYLESS_EXA_SOURCE_ID,
  kind: 'api_key',
  vendor: KEYLESS_EXA_VENDOR_ID,
  display_name: vendor?.name ?? 'Exa (free)',
  protocol: 'search',
  supply_channel: 'hub',
  billing: 'metered',
  state: (candidate?.kind === 'service' ? candidate.state : undefined) ?? ({ status: 'active' } satisfies SourceState),
  last_discovered_at: null,
  models: [],
  base_url: null,
  credential_ref: null,
  masked_credential: null,
});

/** One read signal across Tools, Sources and detail; S2b-2 activates service reads and execution together. */
export const searchServicesActive = (tool: HostedWebSearchToolStatus | null | undefined): boolean =>
  tool?.service_vendors !== undefined;

export const webSearchTool = (status: HostedToolsStatus | null | undefined): HostedWebSearchToolStatus | undefined =>
  status?.tools.find((tool): tool is HostedWebSearchToolStatus => tool.name === 'web_search');

/** Membership comes only from the read, never from a vendor name or a relay URL in the browser. */
export const serviceVendorForSource = (tool: HostedWebSearchToolStatus | undefined, sourceId: string) =>
  searchServicesActive(tool) ? tool?.service_vendors?.find((vendor) => vendor.source_ids.includes(sourceId)) : undefined;

export const serviceSettings = (tool: HostedWebSearchToolStatus, vendor: HostedSearchServiceVendor, sourceId: string) => {
  const candidate = tool.executor_candidates.find((row) => row.source_id === sourceId && row.kind === 'service');
  if (candidate?.kind === 'service') return candidate.settings;
  // A search-capable Source can retain its model-executor role while search is off.
  // The catalog facts and saved overrides still describe its editable settings.
  return {
    enabled: tool.services[sourceId]?.enabled ?? (vendor.facts.tool_only || vendor.facts.auto_enroll),
    results: tool.services[sourceId]?.results ?? Math.min(5, vendor.results_max),
  };
};
