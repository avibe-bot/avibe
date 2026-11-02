// Adding a provider from 模型供应商: the shipped API key dialog (添加供应商 → API Key) and the
// subscription dialog (添加订阅 → a vendor), each in the state it opens in.
import { AddApiKeyDialog } from '@/components/settings/models/AddApiKeyDialog';
import { createSourceCollectionReadAuthority } from '@/components/settings/models/collectionReadAuthority';
import { OAuthConnectDialog } from '@/components/settings/models/OAuthConnectDialog';
import type { OAuthFlow, Source } from '@/components/settings/models/types';

import { frames } from '../../frame';
import { at, baseApi, chatgptPro, demoSources, ok } from './fixtures';

const sourceReads = createSourceCollectionReadAuthority();
const addApiKey = () => <AddApiKeyDialog open sourceReads={sourceReads} onClose={() => {}} onAdded={() => {}} />;

const subscription = (vendor: string, reauth: Source | null = null) => () => (
  <OAuthConnectDialog open vendor={vendor} reauth={reauth} sources={demoSources} onClose={() => {}} onConnected={() => {}} />
);

/** The flow `/api/models/oauth/start` answers with, and the status every poll reads back. */
const flowApi = (flow: Partial<OAuthFlow> & Pick<OAuthFlow, 'vendor' | 'channel' | 'presentation'>) => {
  const full: OAuthFlow = { flow_id: 'flow_design', state: 'awaiting_action', expires_at: at(9), intent: 'create', ...flow };
  return {
    ...baseApi,
    '/api/models/oauth/start': ok({ flow: full }),
    '/api/models/oauth/status/flow_design': ok({ flow: full }),
    // A re-login starts from the Source rather than from the vendor.
    ...(flow.source_id ? { [`/api/models/sources/${flow.source_id}/reauth`]: ok({ flow: full }) } : {}),
  };
};

const chatgptExpired: Source = { ...chatgptPro, state: { status: 'needs_action', retry_at: null, detail_key: 'models.source.needs_action.oauth_expired' } };

const designFrames = frames([
  {
    name: 'api-key',
    title: '添加 API Key',
    width: 1000,
    height: 760,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: addApiKey,
  },
  {
    name: 'api-key-phone',
    title: '添加 API Key · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: addApiKey,
  },
  {
    name: 'claude',
    title: '添加订阅 · Claude（选择接入方式）',
    width: 1000,
    height: 720,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: subscription('anthropic'),
  },
  {
    name: 'claude-phone',
    title: '添加订阅 · Claude · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: subscription('anthropic'),
  },
  {
    name: 'chatgpt',
    title: '添加订阅 · ChatGPT（选择接入方式）',
    width: 1000,
    height: 720,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: subscription('openai'),
  },
  {
    name: 'kimi-device',
    title: '添加订阅 · Kimi（设备码登录）',
    width: 1000,
    height: 720,
    status: 'shipped',
    fixtures: {
      api: flowApi({ vendor: 'kimi', channel: 'hub', presentation: { auth_url: 'https://www.kimi.com/device', device_code: 'WXKP-7QJM', expects: 'none', instructions_key: null } }),
    },
    render: subscription('kimi'),
  },
  {
    name: 'reauth-callback',
    title: '重新登录 · ChatGPT Pro（粘贴回调地址）',
    width: 1000,
    height: 720,
    status: 'shipped',
    fixtures: {
      api: flowApi({
        vendor: 'openai',
        channel: 'hub',
        intent: 'reauth',
        source_id: chatgptPro.id,
        presentation: { auth_url: 'https://auth.openai.com/oauth/authorize?client_id=app_design&state=design', device_code: null, expects: 'paste_callback_url', instructions_key: null },
      }),
    },
    render: subscription('openai', chatgptExpired),
  },
  {
    name: 'failed',
    title: '添加订阅 · 登录失败',
    width: 1000,
    height: 720,
    status: 'shipped',
    fixtures: {
      api: flowApi({ vendor: 'kimi', channel: 'hub', state: 'failed', error_key: 'models.oauth.provider_denied', error_detail: 'access_denied: user cancelled the consent screen', presentation: { expects: 'none' } }),
    },
    render: subscription('kimi'),
  },
]);

export default designFrames;
