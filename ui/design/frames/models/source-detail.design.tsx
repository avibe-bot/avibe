// A provider's detail: the shipped SourceDetailPanel in the Models page's provider dialog,
// opened from a row of 模型供应商, and its 测试 dialog.
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import { SourceDetailPanel } from '@/components/settings/models/SourceDetailPanel';
import { SourceTestDialog } from '@/components/settings/models/SourceTestDialog';
import type { TrackSourceMutation } from '@/components/settings/models/mutationSettlement';
import { readyRegion } from '@/components/settings/models/regionRead';
import type { Source } from '@/components/settings/models/types';

import { frames } from '../../frame';
import { baseApi, chatgptPro, claudeMax, hostedTools, relayGpt, tavily, usageApi } from './fixtures';

const pending: TrackSourceMutation = () => new Promise(() => {});
const tools = readyRegion(hostedTools());
const gatewayBackends = new Set(['claude', 'codex', 'opencode', 'vibey'] as const);
const noPending = new Set<never>();

/** The page's provider dialog (SettingsModelsPage), around the shipped panel. */
const detail = (source: Source, serviceView = false) => () => (
  <Dialog open>
    {/* The page moves focus to the panel's heading, not to its first control. */}
    <DialogContent
      onOpenAutoFocus={(event) => event.preventDefault()}
      mobileSheetHeight="tall"
      className="model-hub-source-dialog flex h-[min(624px,calc(100dvh-32px))] w-[min(720px,calc(100vw-32px))] max-w-[720px] flex-col gap-0 overflow-hidden rounded-[14px] border-border-strong bg-surface p-0 shadow-[var(--model-hub-dialog-shadow)] max-md:w-full max-md:max-w-none max-md:rounded-t-2xl"
    >
      <DialogTitle className="sr-only">{source.display_name}</DialogTitle>
      <DialogDescription className="sr-only">{source.display_name}</DialogDescription>
      <SourceDetailPanel
        source={source}
        serviceView={serviceView}
        hostedTools={tools}
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
    </DialogContent>
  </Dialog>
);

const chatgptExpired: Source = { ...chatgptPro, state: { status: 'needs_action', retry_at: null, detail_key: 'models.source.needs_action.oauth_expired' } };

const designFrames = frames([
  {
    name: 'api-key',
    title: '供应商详情 · API Key（ai-relay-gpt）',
    width: 1000,
    height: 720,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: detail(relayGpt),
  },
  {
    name: 'subscription',
    title: '供应商详情 · 本机订阅（Claude Max）',
    width: 1000,
    height: 720,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: detail(claudeMax),
  },
  {
    name: 'needs-action',
    title: '供应商详情 · 需要重新登录（ChatGPT Pro）',
    width: 1000,
    height: 720,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: detail(chatgptExpired),
  },
  {
    name: 'search-service',
    title: '供应商详情 · 搜索服务（Tavily）',
    width: 1000,
    height: 720,
    status: 'shipped',
    fixtures: { api: { ...baseApi, ...usageApi() } },
    render: detail(tavily, true),
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
    height: 844,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: detail(relayGpt),
  },
]);

export default designFrames;
