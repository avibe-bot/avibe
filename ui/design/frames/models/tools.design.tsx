// 工具: the shipped ToolsTab of the Models page — web search with its services and model
// executors, web fetch order, X search, and image generation. The page keeps its tab in local
// state, so these frames draw the tab's body in the Settings pane.
import { readyRegion, unreadRegion } from '@/components/settings/models/regionRead';
import { ToolsTab } from '@/components/settings/models/ToolsTab';
import type { HostedRefusal, HostedToolStatus, HostedToolsStatus, Source } from '@/components/settings/models/types';

import { frames } from '../../frame';
import { at, baseApi, demoSources, hostedTools, relayGpt, settingsPaneClass, tavily } from './fixtures';

const noPending = new Set<never>();

const tools = (read: ReturnType<typeof readyRegion<HostedToolsStatus>>, sources: Source[] = demoSources) => () => (
  <div className={settingsPaneClass}>
    <ToolsTab
      read={read}
      sources={sources}
      pending={noPending}
      onSave={() => {}}
      onClear={() => {}}
      onRetry={() => {}}
      onAddService={() => {}}
      onOpenService={() => {}}
      onRetryService={() => {}}
      onRetryFetch={() => {}}
    />
  </div>
);

/** The image-generation card alone, as the tab draws it when the gateway offers no image model. */
const imageOnly = (image: Partial<Extract<HostedToolStatus, { name: 'image_generate' }>>): HostedToolsStatus => {
  const tool = hostedTools().tools.find((t) => t.name === 'image_generate')!;
  return { tools: [{ ...tool, default_model: null, effective_default: null, models: [], ...image } as HostedToolStatus] };
};
const refusedImage: HostedRefusal = { source_id: relayGpt.id, model_id: 'gpt-image-2', aspect: 'call', at: at(-90), reason: 'http_400 unsupported_model' };

const revokedTavily: Source = { ...tavily, state: { status: 'needs_action', retry_at: null, detail_key: 'models.source.needs_action.credential_revoked' } };

const designFrames = frames([
  {
    name: 'default',
    title: '工具 · 搜索服务优先、网页读取、图片生成',
    width: 1280,
    height: 1900,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: tools(readyRegion(hostedTools())),
  },
  {
    name: 'no-services',
    title: '工具 · 未添加搜索服务（内置 Exa 免费）',
    width: 1280,
    height: 1400,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: tools(readyRegion(hostedTools({ tavilyAdded: false })), demoSources.filter((s) => s.id !== tavily.id)),
  },
  {
    name: 'key-invalid',
    title: '工具 · Tavily 密钥无效、已自动改用',
    width: 1280,
    height: 1400,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: tools(readyRegion(hostedTools({ tavilyRevoked: true })), demoSources.map((s) => (s.id === tavily.id ? revokedTavily : s))),
  },
  {
    name: 'image-refused',
    title: '工具 · 图片生成 · 唯一的图片模型被拒绝',
    width: 1280,
    height: 320,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: tools(readyRegion(imageOnly({ image_offered_by: ['openai'], status: { latest_ok: null, verified: [], refused: [refusedImage] } }))),
  },
  {
    name: 'image-no-provider',
    title: '工具 · 图片生成 · 还没有能生成图片的供应商',
    width: 1280,
    height: 280,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: tools(readyRegion(imageOnly({ image_offered_by: ['openai', 'gemini'], status: { latest_ok: null, verified: [], refused: [] } }))),
  },
  {
    name: 'image-one-vendor',
    title: '工具 · 图片生成 · 只差一家的 API Key',
    width: 1280,
    height: 280,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: tools(readyRegion(imageOnly({ image_offered_by: ['openai'], status: { latest_ok: null, verified: [], refused: [] } }))),
  },
  {
    name: 'read-failed',
    title: '工具 · 读取失败',
    width: 1280,
    height: 400,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: tools(unreadRegion()),
  },
  {
    name: 'phone',
    title: '工具 · 手机',
    width: 390,
    height: 2400,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: tools(readyRegion(hostedTools())),
  },
]);

export default designFrames;
