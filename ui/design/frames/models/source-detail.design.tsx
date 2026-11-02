// A provider's detail: the shipped SourceDetailPanel, opened from a row of 模型供应商, and its
// 测试 dialog.
import { SourceDetailPanel } from '@/components/settings/models/SourceDetailPanel';
import { SourceTestDialog } from '@/components/settings/models/SourceTestDialog';
import type { TrackSourceMutation } from '@/components/settings/models/mutationSettlement';
import { readyRegion } from '@/components/settings/models/regionRead';
import type { Source } from '@/components/settings/models/types';

import { frames } from '../../frame';
import { baseApi, chatgptPro, claudeMax, hostedTools, relayClaude, relayGpt, tavily, usageApi } from './fixtures';

const pending: TrackSourceMutation = () => new Promise(() => {});
const tools = readyRegion(hostedTools());
const revokedTools = readyRegion(hostedTools({ tavilyRevoked: true }));
const gatewayBackends = new Set(['claude', 'codex', 'opencode', 'vibey'] as const);
const noPending = new Set<never>();

/**
 * The shipped panel at the size the Models page's provider dialog gives it (720 × 624; a tall
 * sheet on a phone). The dialog itself is inline in SettingsModelsPage and opens from page
 * state, so these frames draw the panel without it.
 */
const detail = (source: Source, serviceView = false, hosted = tools) => () => (
  <div className="flex h-full flex-col">
    <SourceDetailPanel
      source={source}
      serviceView={serviceView}
      hostedTools={hosted}
      hostedPending={noPending}
      onRetryHostedTools={() => {}}
      onClearHostedRefusal={() => {}}
      onSaveSearch={() => {}}
      onSaveFetch={() => {}}
      onRetryService={() => {}}
      onRetryFetch={() => {}}
      onManageSource={() => {}}
      onOpenUsage={() => {}}
      activeBackends={gatewayBackends}
      trackMutation={pending}
      onReauth={() => {}}
      onMutationCommitted={async () => {}}
    />
  </div>
);

const tavilyRevoked: Source = { ...tavily, state: { status: 'needs_action', retry_at: null, detail_key: 'models.source.needs_action.credential_revoked' } };
const chatgptExpired: Source = { ...chatgptPro, state: { status: 'needs_action', retry_at: null, detail_key: 'models.source.needs_action.oauth_expired' } };

const designFrames = frames([
  {
    name: 'api-key',
    title: '供应商详情 · API Key（ai-relay-gpt）',
    width: 720,
    height: 624,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: detail(relayGpt),
  },
  {
    name: 'capabilities',
    title: '供应商详情 · 模型能力标签（ai-relay-claude）',
    width: 720,
    height: 624,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: detail(relayClaude),
  },
  {
    name: 'subscription',
    title: '供应商详情 · 本机订阅（Claude Max）',
    width: 720,
    height: 624,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: detail(claudeMax),
  },
  {
    name: 'needs-action',
    title: '供应商详情 · 需要重新登录（ChatGPT Pro）',
    width: 720,
    height: 624,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: detail(chatgptExpired),
  },
  {
    name: 'search-service',
    title: '供应商详情 · 搜索服务（Tavily）',
    width: 720,
    height: 624,
    status: 'shipped',
    fixtures: { api: { ...baseApi, ...usageApi() } },
    render: detail(tavily, true),
  },
  {
    name: 'search-service-revoked',
    title: '供应商详情 · 搜索服务密钥无效（Tavily）',
    width: 720,
    height: 624,
    status: 'shipped',
    fixtures: { api: { ...baseApi, ...usageApi() } },
    render: detail(tavilyRevoked, true, revokedTools),
  },
  {
    name: 'test',
    title: '测试供应商（ai-relay-gpt）',
    width: 1000,
    height: 600,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: () => <SourceTestDialog source={relayGpt} onClose={() => {}} trackMutation={pending} />,
  },
  {
    name: 'phone',
    title: '供应商详情 · 手机',
    width: 390,
    height: 760,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: detail(relayGpt),
  },
]);

export default designFrames;
