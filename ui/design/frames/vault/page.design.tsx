// The shipped Vaults page (Workbench · Capabilities · Vaults): pending requests, active grants, and the
// secret inventory with its search, tag, quick-filter, and sort controls (the view lives in the URL).
import { VaultsPage } from '@/components/workbench/VaultsPage';
import { DENIED_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';

import { frames } from '../../frame';
import { consolePage, phoneShell } from '../workbench/fixtures';
import { vaultApi, vaultFixtures } from './fixtures';

const page = consolePage(<VaultsPage />);
const phone = phoneShell('/vaults', <VaultsPage />);
const fixtures = vaultFixtures();
const quiet = vaultFixtures(vaultApi({ grants: [], requests: [] }));
const rowActions = (nth: number) => `role=button[name="{t:vaults.rowActions}"] >> nth=${nth}`;

// An Editor of a shared instance: may use secrets but not manage them (no edit, delete, or settings).
const editor = { ...DENIED_INSTANCE_CAPABILITIES, can_read_instance: true, can_chat: true, can_use_agents: true, can_use_vault_secrets: true };
// A viewer: may read the instance but not use secrets, so the page is read-only.
const viewer = { ...DENIED_INSTANCE_CAPABILITIES, can_read_instance: true, can_chat: true };

const designFrames = frames([
  {
    name: 'default',
    title: '保险库 · 待处理请求、活跃授权与密钥',
    width: 1192,
    height: 1180,
    status: 'shipped',
    route: '/vaults',
    fixtures,
    render: page,
  },
  {
    name: 'inventory',
    title: '保险库 · 只有密钥（无请求、无授权）',
    width: 1192,
    height: 860,
    status: 'shipped',
    route: '/vaults',
    fixtures: quiet,
    render: page,
  },
  {
    name: 'empty',
    title: '保险库 · 还没有密钥',
    width: 1192,
    height: 480,
    status: 'shipped',
    route: '/vaults',
    fixtures: vaultFixtures(vaultApi({ secrets: [], grants: [], requests: [] })),
    render: page,
  },
  {
    name: 'filtered',
    title: '保险库 · 按技能 deploy-aws 筛选并搜索',
    width: 1192,
    height: 640,
    status: 'shipped',
    route: '/vaults?skill=deploy-aws&q=aws',
    fixtures: quiet,
    render: page,
  },
  {
    name: 'no-match',
    title: '保险库 · 没有匹配的密钥',
    width: 1192,
    height: 520,
    status: 'shipped',
    route: '/vaults?filter=unused&q=github',
    fixtures: quiet,
    render: page,
  },
  {
    name: 'tag-filter',
    title: '保险库 · 标签筛选展开',
    width: 1192,
    height: 860,
    status: 'shipped',
    route: '/vaults',
    fixtures: quiet,
    click: ['role=button[name="{t:vaults.filter.tags}"]'],
    render: page,
  },
  {
    name: 'row-menu',
    title: '保险库 · 密钥的更多操作',
    width: 1192,
    height: 860,
    status: 'shipped',
    route: '/vaults',
    fixtures: quiet,
    click: [rowActions(0)],
    render: page,
  },
  {
    name: 'delete-keypair',
    title: '保险库 · 删除签名密钥（按住确认）',
    width: 1192,
    height: 860,
    status: 'shipped',
    route: '/vaults',
    fixtures: quiet,
    // ETH_HOT_KEY sorts second by name.
    click: [rowActions(1), 'role=menuitem[name="{t:vaults.delete}"]'],
    render: page,
  },
  {
    name: 'history',
    title: '保险库 · 使用记录',
    width: 1192,
    height: 1100,
    status: 'shipped',
    route: '/vaults',
    fixtures: quiet,
    click: ['role=button[name="{t:vaults.history}"]'],
    render: page,
  },
  {
    name: 'avault-authorization',
    title: '保险库 · 需要授权 avault 一次',
    width: 1192,
    height: 860,
    status: 'shipped',
    route: '/vaults',
    fixtures: vaultFixtures({
      ...vaultApi({ grants: [], requests: [] }),
      '/api/vault/avault-authorization': { ok: true, required: true, detected_at: '2026-11-01T07:40:00Z', attempt: null },
    }),
    render: page,
  },
  {
    name: 'editor',
    title: '保险库 · 共享实例的编辑者（可用，不可管理）',
    width: 1192,
    height: 860,
    status: 'shipped',
    route: '/vaults',
    fixtures: vaultFixtures(vaultApi({ grants: [], requests: [] }), { capabilities: editor }),
    render: page,
  },
  {
    name: 'read-only',
    title: '保险库 · 只读',
    width: 1192,
    height: 760,
    status: 'shipped',
    route: '/vaults',
    fixtures: vaultFixtures(vaultApi({ grants: [], requests: [] }), { capabilities: viewer }),
    render: page,
  },
  {
    name: 'phone',
    title: '保险库 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/vaults',
    fixtures,
    render: phone,
  },
  {
    name: 'phone-inventory',
    title: '保险库 · 手机 · 密钥列表',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/vaults',
    fixtures: quiet,
    render: phone,
  },
]);

export default designFrames;
