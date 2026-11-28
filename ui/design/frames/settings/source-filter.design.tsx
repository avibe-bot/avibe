// Settings › Experimental › Information cocoon (docs/plans/vibey-source-filter-hooks.md). Experimental is a list of
// experiments; Information cocoon is one row that opens its own page (owner decision 2026-11-27). The page shows the
// switch, the guide link and, when on, the hook folder and each script's state. The phone's section list with the
// new row is settings/sections/phone. The failure shown is an example.
import {
  SettingsExperimentalPage,
  SettingsSourceFilterPage,
} from '@/components/settings/experimental/SettingsExperimentalPages';
import type { SourceFilterStatus } from '@/components/settings/experimental/sourceFilterApi';

import { frames, type FrameSpec } from '../../frame';
import { DESIGN_NOW } from '../../shell/clock';
import { IPHONE_17_PRO } from '../../shell/fixtures';
import { settingsFixtures, settingsPage } from './fixtures';

const DIR = '/Users/demo/.avibe/hooks/source_filter';
const off: SourceFilterStatus = {
  enabled: false,
  dir: DIR,
  hooks: {
    before_search: { found: false, last_failure: null },
    after_search: { found: false, last_failure: null },
    before_fetch: { found: false, last_failure: null },
    after_fetch: { found: false, last_failure: null },
  },
};
// On, with each state a script shows: found, not found (skipped), and a last run that failed.
const on: SourceFilterStatus = {
  enabled: true,
  dir: DIR,
  hooks: {
    before_search: { found: true, last_failure: null },
    after_search: { found: true, last_failure: null },
    before_fetch: { found: false, last_failure: null },
    after_fetch: {
      found: true,
      last_failure: { at: new Date(DESIGN_NOW - 28 * 60_000).toISOString(), reason: 'it timed out after 600 s' },
    },
  },
};

const fixtures = (status: SourceFilterStatus) => settingsFixtures({
  '/api/settings/experimental/source-filter': { ok: true, ...status },
});

type Device = 'phone' | 'desktop';
const size = (device: Device, height: number) => (device === 'phone' ? { ...IPHONE_17_PRO, height } : { width: 1440, height });

const list = (device: Device, status: SourceFilterStatus, title: string, height: number): FrameSpec => ({
  name: `${device}-list-${status.enabled ? 'on' : 'off'}`,
  title,
  ...size(device, height),
  status: 'shipped',
  route: '/settings/experimental',
  fixtures: fixtures(status),
  render: settingsPage('experimental', <SettingsExperimentalPage />),
});

const page = (device: Device, status: SourceFilterStatus, title: string, height: number): FrameSpec => ({
  name: `${device}-${status.enabled ? 'on' : 'off'}`,
  title,
  ...size(device, height),
  status: 'shipped',
  route: '/settings/experimental/source-filter',
  fixtures: fixtures(status),
  render: settingsPage('experimental/source-filter', <SettingsSourceFilterPage />),
});

const designFrames = frames([
  list('phone', off, '手机 · 实验性 · 项目列表（信息茧房关闭）', IPHONE_17_PRO.height),
  list('phone', on, '手机 · 实验性 · 项目列表（信息茧房已开启）', IPHONE_17_PRO.height),
  page('phone', off, '手机 · 实验性 › 信息茧房 · 关闭（默认）', IPHONE_17_PRO.height),
  page('phone', on, '手机 · 实验性 › 信息茧房 · 打开：已找到、未找到、上次运行失败', IPHONE_17_PRO.height),
  list('desktop', on, '桌面 · 实验性 · 项目列表（信息茧房已开启）', 760),
  page('desktop', off, '桌面 · 实验性 › 信息茧房 · 关闭（默认）', 760),
  page('desktop', on, '桌面 · 实验性 › 信息茧房 · 打开：已找到、未找到、上次运行失败', 900),
]);

export default designFrames;
