// Adding a search service (工具 → 添加搜索服务, or 添加供应商 → 搜索服务): the shipped
// AddSearchServiceDialog listing the catalog, and the API key dialog it hands a vendor to.
import { AddApiKeyDialog } from '@/components/settings/models/AddApiKeyDialog';
import { AddSearchServiceDialog } from '@/components/settings/models/AddSearchServiceDialog';
import { createSourceCollectionReadAuthority } from '@/components/settings/models/collectionReadAuthority';
import { webFetchTool } from '@/components/settings/models/webFetch';
import { webSearchTool } from '@/components/settings/models/searchServices';

import { frames } from '../../frame';
import { baseApi, hostedTools } from './fixtures';

const sourceReads = createSourceCollectionReadAuthority();
const status = hostedTools();
const search = webSearchTool(status)!;
const fetchTool = webFetchTool(status);
const exa = search.service_vendors!.find((vendor) => vendor.id === 'exa')!;

const electedSearch = webSearchTool(hostedTools({ parallelElected: true }))!;

const catalog = (tool = search) => () => (
  <AddSearchServiceDialog tool={tool} fetchTool={fetchTool} pending={false} sourceReads={sourceReads} onClose={() => {}} onAdded={() => {}} onOpenSource={() => {}} onViewOrder={() => {}} />
);
const vendorKey = () => <AddApiKeyDialog open serviceVendor={exa} sourceReads={sourceReads} onClose={() => {}} onAdded={() => {}} />;

const designFrames = frames([
  {
    name: 'catalog',
    title: '添加搜索服务 · 服务列表（Tavily 已添加、内置 Exa 免费；Parallel 免费默认关闭，需选择「用于网页搜索」）',
    width: 1000,
    height: 1500,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: catalog(),
  },
  {
    name: 'catalog-parallel-elected',
    title: '添加搜索服务 · Parallel（免费）已选用，改为「查看顺序」',
    width: 1000,
    height: 1500,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: catalog(electedSearch),
  },
  {
    name: 'catalog-phone',
    title: '添加搜索服务 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: catalog(),
  },
  {
    name: 'api-key',
    title: '添加 Exa · API Key',
    width: 1000,
    height: 720,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: vendorKey,
  },
]);

export default designFrames;
