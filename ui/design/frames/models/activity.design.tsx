// The Models page's activity tabs, each the shipped tab component in the Settings pane (the
// page keeps its tab in local state): 订阅额度 (QuotaTab), 用量 (UsageTab), 日志 (RecentSwitchesCard).
import { emptyFeed } from '@/components/settings/models/eventFeed';
import { QuotaTab } from '@/components/settings/models/QuotaTab';
import { RecentSwitchesCard } from '@/components/settings/models/RecentSwitchesCard';
import { readyRegion, unreadRegion } from '@/components/settings/models/regionRead';
import type { QuotaSummary, UsageReport, UsageWindowKey } from '@/components/settings/models/types';
import { UsageTab } from '@/components/settings/models/UsageTab';

import { frames } from '../../frame';
import { DESIGN_NOW } from '../../shell/clock';
import { baseApi, demoSources, quotaSummary, settingsPaneClass, switchEvents, usageReport } from './fixtures';

const quota = (summary: QuotaSummary) => {
  const read = readyRegion(summary);
  return () => (
    <div className={settingsPaneClass}>
      <QuotaTab quota={read} now={DESIGN_NOW} onRefresh={() => {}} onRetry={() => {}} onRequestReauth={() => {}} />
    </div>
  );
};

const usage = (windowKey: UsageWindowKey, report: UsageReport | null = usageReport(windowKey)) => {
  const read = report ? readyRegion(report) : unreadRegion<UsageReport>();
  return () => (
    <div className={settingsPaneClass}>
      <UsageTab usage={read} windowKey={windowKey} onWindowChange={() => {}} onRetry={() => {}} />
    </div>
  );
};

const sourcesRead = readyRegion(demoSources);
const logs = (events: typeof switchEvents) => {
  const read = readyRegion({ ...emptyFeed, events });
  return () => (
    <div className={settingsPaneClass}>
      <RecentSwitchesCard events={read} sources={sourcesRead} onLoadMore={() => {}} onRetry={() => {}} />
    </div>
  );
};

const idle: UsageReport = { ...usageReport('24h'), sources: [], services: [], buckets: usageReport('24h').buckets.map((bucket) => ({ ...bucket, rows: [] })), totals: { requests: 0, token_reports: 0, input_tokens: 0, cached_input_tokens: 0, output_tokens: 0 } };

const designFrames = frames([
  {
    name: 'quota',
    title: '订阅额度 · Claude Max 与 ChatGPT Pro',
    width: 1280,
    height: 1000,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: quota(quotaSummary()),
  },
  {
    name: 'quota-expired',
    title: '订阅额度 · 登录过期',
    width: 1280,
    height: 1000,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: quota(quotaSummary({ claudeExpired: true })),
  },
  {
    name: 'quota-empty',
    title: '订阅额度 · 还没有订阅',
    width: 1280,
    height: 500,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: quota({ refresh_interval_seconds: 300, sources: [] }),
  },
  {
    name: 'quota-phone',
    title: '订阅额度 · 手机',
    width: 390,
    height: 1600,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: quota(quotaSummary()),
  },
  {
    name: 'usage-24h',
    title: '用量 · 最近 24 小时',
    width: 1280,
    height: 1500,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: usage('24h'),
  },
  {
    name: 'usage-30d',
    title: '用量 · 最近 30 天',
    width: 1280,
    height: 1500,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: usage('30d'),
  },
  {
    name: 'usage-empty',
    title: '用量 · 还没有用量',
    width: 1280,
    height: 700,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: usage('24h', idle),
  },
  {
    name: 'usage-phone',
    title: '用量 · 手机',
    width: 390,
    height: 1800,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: usage('24h'),
  },
  {
    name: 'logs',
    title: '日志 · 最近切换',
    width: 1280,
    height: 420,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: logs(switchEvents),
  },
  {
    name: 'logs-empty',
    title: '日志 · 还没有切换',
    width: 1280,
    height: 360,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: logs([]),
  },
  {
    name: 'logs-phone',
    title: '日志 · 手机',
    width: 390,
    height: 640,
    status: 'shipped',
    fixtures: { api: baseApi },
    render: logs(switchEvents),
  },
]);

export default designFrames;
