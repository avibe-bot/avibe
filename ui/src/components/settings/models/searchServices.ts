import type { HostedExecutorCandidate, HostedSearchServiceVendor, HostedToolsStatus, HostedWebSearchToolStatus, Source, SourceState } from './types';

/** Legacy identity: servers that predate catalog `default_enabled` / `endpoint` facts only ship keyless Exa. */
export const KEYLESS_EXA_SOURCE_ID = 'src_exakeyless';
export const KEYLESS_EXA_VENDOR_ID = 'exa-mcp';
// Joined, not a literal: a dotted string would read as an i18n key.
const LEGACY_EXA_ENDPOINT = ['mcp', 'exa', 'ai'].join('.');

/** A keyless built-in Source row: identity comes from the catalog vendor, never from a source id. */
export const keylessSearchSource = (
  sourceId: string,
  vendor: { id: string; name: string },
  candidate?: HostedWebSearchToolStatus['executor_candidates'][number],
): Source => ({
  id: sourceId,
  kind: 'api_key',
  vendor: vendor.id,
  display_name: vendor.name,
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

export const keylessExaSource = (candidate?: HostedWebSearchToolStatus['executor_candidates'][number], vendor?: HostedSearchServiceVendor): Source =>
  keylessSearchSource(KEYLESS_EXA_SOURCE_ID, { id: KEYLESS_EXA_VENDOR_ID, name: vendor?.name ?? 'Exa (free)' }, candidate);

/** The catalog's built-in (keyless, no setup) vendor behind a Source, by read membership only. */
export const builtinVendorForSource = (tool: HostedWebSearchToolStatus | undefined, sourceId: string): HostedSearchServiceVendor | undefined => {
  const vendor = serviceVendorForSource(tool, sourceId);
  return vendor?.facts.built_in ? vendor : undefined;
};

/** Catalog-owned keyless identities use the existing service settings panel; an old read without the catalog still knows Exa. */
export const builtinSearchSource = (sourceId: string | null, tool?: HostedWebSearchToolStatus): Source | null => {
  if (sourceId === null) return null;
  const vendor = builtinVendorForSource(tool, sourceId);
  if (vendor) return keylessSearchSource(sourceId, vendor);
  return sourceId === KEYLESS_EXA_SOURCE_ID ? keylessExaSource() : null;
};

/** Where a keyless service receives queries: the catalog's endpoint; an old read only ever had Exa's. */
export const keylessEndpoint = (vendor: HostedSearchServiceVendor | undefined, sourceId: string): string | null =>
  vendor?.endpoint ?? (sourceId === KEYLESS_EXA_SOURCE_ID || vendor?.id === KEYLESS_EXA_VENDOR_ID ? LEGACY_EXA_ENDPOINT : null);

/** Whether a service starts on before the user chooses; an old read falls back to its enrollment facts. */
export const serviceDefaultEnabled = (vendor: HostedSearchServiceVendor): boolean =>
  vendor.facts.default_enabled ?? (vendor.facts.tool_only || vendor.facts.auto_enroll);

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
    enabled: tool.services[sourceId]?.enabled ?? serviceDefaultEnabled(vendor),
    results: tool.services[sourceId]?.results ?? Math.min(5, vendor.results_max),
  };
};

/** Whether the chain may try this candidate: a service's switch, or a web-search model executor's; absent is on. */
export const candidateEnabled = (candidate: HostedExecutorCandidate): boolean =>
  candidate.kind === 'service' ? candidate.settings.enabled : candidate.settings?.enabled ?? true;
