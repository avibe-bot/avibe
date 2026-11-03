// A pinned Show Page opened as an app: the shipped window frames the page's private `/show/<id>/` surface,
// with the chat, share, annotate, and open-in-tab controls in its title bar. The page itself is the
// agent's work, drawn here from a design asset.
import funnelPage from '../../assets/show-page-funnel.svg';
import { frames } from '../../frame';
import { session } from '../../shell/fixtures';
import { showPages } from '../show-pages/fixtures';
import { appsFixtures, pinnedDock, soloWindow, windowOnly } from './fixtures';

const page = showPages[0];
const sid = page.session_id;
const access = {
  ok: true,
  mode: 'personal',
  ownership_status: 'unmanaged',
  instance_id: null,
  organization_id: null,
  policy_organization_id: null,
  access_level: 'owner',
  group_ids: [],
  policy_revision: null,
  last_applied_control_plane_revision: null,
  can_use: true,
  can_manage: true,
  can_publish_public: true,
};
const api = (archived = false) => ({
  '/api/dock': { ok: true, dock: pinnedDock() },
  [`/api/sessions/${sid}`]: session(sid, 'proj_avibe', page.title ?? '', { status: archived ? 'archived' : 'active' }),
  [`/show/${sid}/`]: funnelPage,
  [`/api/show-pages/${sid}`]: page,
  [`/api/show-pages/${sid}/access`]: access,
  [`/api/show-pages/${sid}/access-settings/read`]: {
    show_access: { page_id: sid, access_mode: page.access_mode, share_id: page.share_id, revision: page.access_revision, normalized_emails: [], entries: [] },
  },
});
const layout = (width: number, height: number) =>
  soloWindow('showpage', width, height, { title: page.title ?? '', params: { sessionId: sid, title: page.title } });

const designFrames = frames([
  {
    name: 'window',
    title: 'Show Page 应用 · 窗口',
    width: 1100,
    height: 760,
    status: 'shipped',
    fixtures: appsFixtures(api(), { storage: layout(1100, 760) }),
    wait: 800,
    render: windowOnly,
  },
  {
    name: 'share',
    title: 'Show Page 应用 · 分享与固定到 Dock',
    width: 1100,
    height: 760,
    status: 'shipped',
    fixtures: appsFixtures(api(), { storage: layout(1100, 760) }),
    wait: 800,
    click: ['role=button[name="{t:chat.showPage.share}"]'],
    render: windowOnly,
  },
  {
    name: 'missing',
    title: 'Show Page 应用 · 页面已不在',
    width: 1100,
    height: 560,
    status: 'shipped',
    fixtures: appsFixtures(api(true), { storage: layout(1100, 560) }),
    wait: 800,
    render: windowOnly,
  },
]);

export default designFrames;
