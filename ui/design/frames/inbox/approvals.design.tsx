// What waits on the reader, where they look: the Vault requests an Agent is blocked on, at the top of the
// Inbox (both tabs) with the same rows and dialog as the Vaults page, counted in the Inbox entry's badge
// (sidebar icon, phone tab). The Session that asked is a link that reads as one without a hover; it opens
// that conversation, where the request's own card waits until it is answered.
import type { VaultRequest } from '@/context/ApiContext';
import { InboxPage } from '@/components/workbench/InboxPage';
import { VaultsPage } from '@/components/workbench/VaultsPage';

import { frames } from '../../frame';
import { DesktopScreen } from '../../shell/DesktopScreen';
import { inboxRow } from '../../shell/fixtures';
import { settingsConfig } from '../settings/fixtures';
import { phoneShell } from '../workbench/fixtures';
import { accessRequest, vaultApi, vaultFixtures } from '../vault/fixtures';

const page = <InboxPage onOpenSearch={() => {}} />;
const desktop = () => <DesktopScreen page={page} />;
const phone = phoneShell('/inbox', page);

const projectSession = (id: string, title: string) => ({ id, title, label: null, platform: 'avibe', scope_kind: 'project', is_workbench: true });

// A background Session asks for a Protected secret to promote a build (the owner's case), and another
// runs a command with a standard one.
const promote: VaultRequest = {
  ...accessRequest,
  id: 'vrq_promote',
  secret_name: 'STRIPE_LIVE_KEY',
  card: {
    ...(accessRequest.card as Record<string, unknown>),
    secret_name: 'STRIPE_LIVE_KEY',
    protection: 'protected',
    command: null,
    egress: 'api.stripe.com',
    source_selector: { env: ['STRIPE_LIVE_KEY'] },
  },
  session: projectSession('ses_promote', '发布收款服务到生产'),
};
const requests = [promote, accessRequest];

const rows = [
  inboxRow('ses_cso', null, '首席安全官', 'avault 依赖里发现 2 个高危漏洞，**升级到 0.7.0** 可以修复，要我开 PR 吗？', 6, 2),
  inboxRow('ses_promote', 'avibe', '发布收款服务到生产', '构建已经通过，发布要用 STRIPE_LIVE_KEY，已经发了批准请求，等你批准后继续。', 3, 1),
  inboxRow('ses_demo_pr', 'avibe', 'PR #3073 交付', 'CI 失败：`unit-tests (2/8)`，是 flaky 的 SQLite 锁超时，已经重跑。', 40),
];
const unread = { ses_cso: 2, ses_promote: 1 };

const withRequests = (extra: Parameters<typeof vaultFixtures>[1] = {}) => vaultFixtures(
  {
    ...vaultApi({ grants: [], requests }),
    // No saved language, so the phone shell keeps the frame's (zh or en) rather than adopting zh.
    '/api/config': { ...settingsConfig, language: undefined },
    // Review reads the request in full first.
    '/api/vault/requests/vrq_access': { ok: true, request: accessRequest },
    '/api/vault/requests/vrq_promote': { ok: true, request: promote },
  },
  { inboxSessions: rows, unreadBySession: unread, ...extra },
);
const fixtures = withRequests();
// Nothing unread: the requests still show, above "all caught up", and the badge still counts them.
// New Bitcoin requests are listed as summaries (#3871): the key's name and tier, no card or Session;
// one whose name is too long for the summary keeps the generic line. Review reads each in full first.
const btcSummary = (id: string, secret_name: string | null): VaultRequest => ({
  ...accessRequest, id, request_type: 'sign', secret_name, protection: 'protected',
  requester: null, delivery: null, message_id: null, card: null, session: null,
});
const btcFixtures = vaultFixtures(
  { ...vaultApi({ grants: [], requests: [promote, btcSummary('vrq_btc_1', 'R9_PROT_KEY'), btcSummary('vrq_btc_2', null)] }), '/api/config': { ...settingsConfig, language: undefined } },
  { inboxSessions: rows, unreadBySession: unread },
);
const caughtUp = withRequests({ inboxSessions: rows.map((row) => ({ ...row, unread: false, unread_count: 0 })), unreadBySession: {} });
// The second row is the standard request; its dialog is the one a browser and the desktop app both show.
const reviewStandard = ['role=button[name="{t:vaults.requests.review}"] >> nth=1'];

const designFrames = frames([
  {
    name: 'desktop',
    title: '收件箱 · 待处理请求在最上方，收件箱图标的数字把它算进去',
    width: 1440,
    height: 980,
    status: 'shipped',
    themes: ['light'],
    route: '/inbox',
    fixtures,
    render: desktop,
  },
  {
    name: 'review',
    title: '收件箱 · 点「审阅」打开原来的批准弹窗，「会话」是能点的链接',
    width: 1440,
    height: 980,
    status: 'shipped',
    themes: ['light'],
    route: '/inbox',
    fixtures,
    click: reviewStandard,
    render: desktop,
  },
  {
    name: 'btc-summaries',
    title: '收件箱 · 新的 Bitcoin 签名请求：摘要行带密钥名和保护档；名称过长时仍显示「签名请求 · 点审阅查看详情」',
    width: 1440,
    height: 980,
    status: 'shipped',
    themes: ['light'],
    route: '/inbox',
    fixtures: btcFixtures,
    render: desktop,
  },
  {
    name: 'popover',
    title: '收件箱 · 悬停收件箱图标：待处理请求排在最前，点进完整收件箱',
    width: 1440,
    height: 760,
    status: 'shipped',
    themes: ['light'],
    route: '/inbox',
    fixtures,
    hover: 'a[href="/inbox"][aria-label]',
    render: desktop,
  },
  {
    name: 'caught-up',
    title: '收件箱 · 没有未读，只有待处理请求',
    width: 1440,
    height: 760,
    status: 'shipped',
    themes: ['light'],
    route: '/inbox',
    fixtures: caughtUp,
    render: desktop,
  },
  {
    name: 'phone',
    title: '收件箱 · 手机 · 待处理请求与底部收件箱的数字',
    width: 390,
    height: 844,
    status: 'shipped',
    themes: ['light'],
    route: '/inbox',
    fixtures,
    render: phone,
  },
  {
    name: 'phone-review',
    title: '收件箱 · 手机 · 批准弹窗里的会话链接',
    width: 390,
    height: 844,
    status: 'shipped',
    themes: ['light'],
    route: '/inbox',
    fixtures,
    click: reviewStandard,
    render: phone,
  },
  {
    name: 'vaults',
    title: '保险库 · 同一组待处理请求，会话链接的新样式',
    width: 1440,
    height: 900,
    status: 'shipped',
    themes: ['light'],
    route: '/vaults',
    fixtures,
    render: () => <DesktopScreen page={<VaultsPage />} />,
  },
  {
    name: 'desktop-app-handoff',
    title: '桌面端 · 受保护的请求要去浏览器批准：弹窗里也写明是哪个会话',
    width: 1440,
    height: 900,
    status: 'shipped',
    themes: ['light'],
    route: '/vaults?request_id=vrq_promote',
    fixtures: withRequests({ desktopShell: true }),
    render: () => <DesktopScreen page={<VaultsPage />} />,
  },
]);

export default designFrames;
