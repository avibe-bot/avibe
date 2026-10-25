import type { HostedWebFetchToolStatus, HostedToolsStatus } from './types';

export const LOCAL_FETCH_SOURCE_ID = 'src_localfetch';
export const KEYLESS_JINA_SOURCE_ID = 'src_jinakeyless';

/** The server omits this entire row while WEB_FETCH_ACTIVE is false. No client flag. */
export const webFetchTool = (status: HostedToolsStatus | null | undefined): HostedWebFetchToolStatus | undefined =>
  status?.tools.find((tool): tool is HostedWebFetchToolStatus => tool.name === 'web_fetch');

export const fetchVendorForSource = (tool: HostedWebFetchToolStatus | undefined, sourceId: string) =>
  tool?.service_vendors.find((vendor) => vendor.source_ids.includes(sourceId));
