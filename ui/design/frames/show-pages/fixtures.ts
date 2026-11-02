// Show Pages as `/api/show-pages` lists them: every Session's page, private, public, or offline.
import type { ShowPage } from '@/lib/showPagesStore';

import { DESIGN_NOW } from '../../shell/clock';

const at = (minutesAgo: number) => new Date(DESIGN_NOW - minutesAgo * 60_000).toISOString();

const showPage = (sessionId: string, title: string, agent: string, minutesAgo: number, extra: Partial<ShowPage> = {}): ShowPage => ({
  session_id: sessionId,
  visibility: 'private',
  access_mode: 'private',
  access_revision: 1,
  can_manage: true,
  can_publish_public: true,
  title,
  platform: 'avibe',
  agent,
  path: `/Users/demo/.avibe/show/${sessionId}`,
  icon_version: null,
  active_url: `http://127.0.0.1:5123/apps/show/${sessionId}`,
  private_url: `http://127.0.0.1:5123/apps/show/${sessionId}`,
  public_url: null,
  url_available: true,
  share_id: null,
  offline: false,
  offline_at: null,
  created_at: at(minutesAgo + 60 * 24 * 3),
  updated_at: at(minutesAgo),
  ...extra,
});

export const showPages: ShowPage[] = [
  showPage('ses_funnel', 'Q4 漏斗看板', 'vibey', 12, {
    visibility: 'public',
    access_mode: 'public',
    public_url: 'https://avibe.bot/p/3xKf9aQ2',
    active_url: 'https://avibe.bot/p/3xKf9aQ2',
    share_id: '3xKf9aQ2',
  }),
  showPage('ses_design_review', '侧边栏设计评审', 'claude', 60),
  showPage('ses_release', 'v3.2.4 发布说明', 'docs-writer', 60 * 5, { platform: 'slack' }),
  showPage('ses_old_demo', '投资人演示（旧）', 'codex', 60 * 24 * 3, {
    visibility: 'offline',
    offline: true,
    offline_at: at(60 * 24 * 2),
    active_url: null,
    private_url: null,
    url_available: false,
  }),
];

export const showPagesApi = (pages: ShowPage[] = showPages) => ({ '/api/show-pages': { pages } });
