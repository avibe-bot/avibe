// Approved 2026-11-20 (owner). The desktop app connected to an instance on another computer shows two
// things that update separately: that instance, and this computer's desktop app. Today the
// sidebar shows the app's version beside the instance's name, and its panel updates the app
// without saying so (`now-*`, the shipped components). The proposal: the version beside the
// instance's name is the instance's, and the one panel has a section for each, the instance
// first, each with its own version, state and button (kit/update-subjects.tsx).
//
// Nothing changes while the app shows the instance on its own computer (update/panel), or in
// a browser or on a phone (`elsewhere-*`): there one thing is on screen, and the panel is its.
import type { ReactNode } from 'react';
import { Route, Routes } from 'react-router-dom';

import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { AppShell, AppShellFooter } from '@/components/AppShell';
import { Workbench } from '@/components/Workbench';
import { SettingsGeneralPage } from '@/components/settings/SettingsGeneralPage';
import { UpdatePanel } from '@/components/update/UpdatePanel';
import { modelFromDesktopState, modelFromRuntime, type UpdatePanelModel } from '@/components/update/updatePanelModel';
import type { VersionInfo } from '@/context/ApiContext';
import { DesktopAccountRowView } from '@/desktop/DesktopAccountRowView';
import { DesktopUpdatePanel, UpdatePopoverView } from '@/desktop/AccountApp';
import type { DesktopClientProjection, DesktopClientState } from '@/desktop/bridge';
import type { DesktopUpdateState } from '@/lib/desktopShell';

import { frames, type FrameSpec } from '../../frame';
import { InstanceVersionRow, UpdateSubjectsPanel, type SubjectLang } from '../../kit/update-subjects';
import { agentsApi } from '../agents/fixtures';
import { copyFor } from '../desktop/instances-fixtures';
import { Badges, Captioned, Card, States, type PanelState, type Subjects } from './subjects';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import { homeFactsApi, workbenchFixtures } from '../workbench/fixtures';

// The design clock reads 2026-11-01 08:00Z, so these checks were eight minutes ago.
const CHECKED_AT = '2026-11-01T07:52:00Z';
const INSTANCE = 'alex';
const OLD = '3.2.8rc2';
const NEW = '3.2.8rc4';
// The desktop app stamps SemVer; the panel shows both in the Runtime's form.
const APP_OLD = '3.2.8-rc.2';
const APP_NEW = '3.2.8-rc.4';

const copy = (lang: SubjectLang) => copyFor(lang === 'en' ? en : zh);
const label = (lang: SubjectLang) => (z: string, e: string) => (lang === 'en' ? e : z);

/** The instance: a standalone Runtime, read from its `/api/version`. */
const info = (extra: Partial<VersionInfo> = {}): VersionInfo => ({
  current: OLD, latest: NEW, has_update: true, error: null, build: { kind: 'package' }, ...extra,
});
const quiet = { checking: false, installing: false, restarting: false, result: null, prereleases: true, autoUpdate: false, checkedAt: CHECKED_AT };
const instance = (extra: Partial<VersionInfo> = {}, view: Partial<Parameters<typeof modelFromRuntime>[1]> = {}): UpdatePanelModel =>
  modelFromRuntime(info(extra), { ...quiet, ...view });
const instanceCurrent = () => instance({ current: NEW, latest: NEW, has_update: false });

/** This computer's desktop app, from its own updater. */
const updater = (state: Partial<DesktopUpdateState> = {}): DesktopUpdateState => ({
  phase: 'available', latest: APP_NEW, channel: 'test', prereleases: true, skippedVersion: null, interactive: true,
  remoteRequest: null, checkedAt: CHECKED_AT, autoUpdate: false, hostActions: ['check', 'prereleases', 'auto_update', 'install'], ...state,
});
const app = (state: Partial<DesktopUpdateState> = {}, current = APP_OLD): UpdatePanelModel =>
  modelFromDesktopState(updater(state), { current, checkedAt: CHECKED_AT });
const appCurrent = () => app({ phase: 'current', latest: null }, APP_NEW);

const pair = (first: UpdatePanelModel | null, second: UpdatePanelModel, open?: 'instance' | 'app'): Subjects => ({
  instance: { name: INSTANCE, model: first, open: open === 'instance' },
  app: { model: second, open: open === 'app' },
});

const bothAvailable = pair(instance(), app());

const states: PanelState[] = [
  { name: 'current', zh: '都是最新', en: 'Both up to date', subjects: pair(instanceCurrent(), appCurrent()) },
  { name: 'available', zh: '两个都有新版本', en: 'Both have an update', subjects: bothAvailable },
  {
    name: 'updating', zh: '点了 alex 的「更新」：安装中', en: "After Update on alex: installing",
    subjects: pair(instance({}, { installing: true }), app()),
  },
  {
    name: 'updated', zh: 'alex 更新完成，这台电脑上的应用还是旧版本', en: 'alex is updated; the app on this computer is still behind',
    subjects: pair(instance({ current: NEW, latest: NEW, has_update: false }, { result: { ok: true, target: NEW } }), app()),
  },
  {
    name: 'failed', zh: 'alex 更新失败', en: 'The update of alex failed',
    subjects: pair(instance({}, { result: { ok: false, target: NEW } }), appCurrent()),
  },
  {
    name: 'app-downloading', zh: '点了这台电脑上的应用的「更新」：下载中', en: "After Update on this computer's app: downloading",
    subjects: pair(instanceCurrent(), app({
      phase: 'downloading',
      remoteRequest: { requestId: 'request-1', target: APP_NEW, phase: 'downloading', progress: 42, errorCode: null, updatedAt: CHECKED_AT },
    })),
  },
  { name: 'details', zh: '点名字展开：这一项的上次检查和开关', en: 'A name opens its own last check and switches', subjects: pair(instance(), appCurrent(), 'instance') },
  { name: 'offline', zh: 'alex 连接断开', en: 'alex cannot be reached', subjects: pair(null, appCurrent()) },
  {
    name: 'member', zh: '不能管理 alex 的成员：看得到，没有按钮和开关', en: 'A member who cannot manage alex: the state, no button or switches',
    subjects: {
      instance: { name: INSTANCE, model: instance({}, { prereleases: null, autoUpdate: null }), canUpdate: false },
      app: { model: appCurrent() },
    },
  },
  {
    name: 'long-name', zh: '实例名字很长：名字和按钮都截断', en: 'A long instance name truncates, in its row and in its button',
    subjects: { instance: { name: 'mac-mini-in-the-studio-upstairs-office', model: instance() }, app: { model: appCurrent() } },
  },
];

const badgeRows = (lang: SubjectLang): [string, ReactNode][] => {
  const l = label(lang);
  return [
    [l('现在：实例名旁边是这台电脑上的应用的版本', "Today: this computer's app version beside the instance's name"),
      <DesktopAccountRowView state={projection(true)} onPresent={() => {}} localUpdater={null} />],
    [l('提议：alex 的版本，都是最新', "Proposed: alex's version, both up to date"), <InstanceVersionRow lang={lang} name={INSTANCE} version={NEW} />],
    [l('alex 或这台电脑上的应用有新版本', "An update for alex or for this computer's app"), <InstanceVersionRow lang={lang} name={INSTANCE} version={OLD} attention />],
    [l('alex 更新中（面板关着也看得到）', 'alex is updating (shown with the panel closed)'), <InstanceVersionRow lang={lang} name={INSTANCE} version={OLD} updating />],
    [l('alex 连接断开', 'alex cannot be reached'), <InstanceVersionRow lang={lang} name={INSTANCE} version={OLD} offline />],
  ];
};

const projection = (update: boolean): DesktopClientProjection => ({
  version: 1, revision: 1, generation: 1,
  account: { status: 'signed_in', identity: { id: 'fixture', email: 'max@example.test' } },
  connection: {
    status: 'remote_connected',
    committed: { kind: 'remote', name: INSTANCE, instance_id: 'alex', canonical_origin: 'https://instance.example.test' },
    pending: null,
  },
  local: { name: '小麦的 MacBook', remote_access: 'off' },
  desktop: { version: APP_OLD, update: updater(update ? {} : { phase: 'current', latest: null }) },
  remote_auxiliary: 'disabled_incomplete',
});
// The popup page reads the full client state; only its update fields matter here.
const clientState = { ...projection(true), account: { ...projection(true).account, email: null, gate: null, methods: [] } } as unknown as DesktopClientState;
const install = { failed: false, accept: () => {}, reset: () => {} } as Parameters<typeof DesktopUpdatePanel>[0]['install'];

const windowFixtures = (lang: SubjectLang) => workbenchFixtures({
  ...agentsApi(),
  ...homeFactsApi({ model: true, push: true, computerUse: true, chatApps: true, paired: true }),
  '/api/session': { remote: false },
  '/api/version': info(),
}, { copy: copy(lang) });

/** The desktop app's window on the instance's page, with the given bottom row and, when open, the shell's popup over it. */
const appWindow = (row: ReactNode, popup?: { height: string; label: string; content: ReactNode }) => () => (
  <div className="relative h-full">
    <Routes>
      <Route element={<AppShell footer={({ tools }) => <AppShellFooter tools={tools}>{row}</AppShellFooter>} />}>
        <Route path="/" element={<Workbench />} />
      </Route>
    </Routes>
    {/* The popup window: `fixed` inside UpdatePopoverView resolves to this transformed box, placed where the
        shell puts its window (8 px in from the left, 40 px above the bottom). */}
    {popup && (
      <div className={`absolute bottom-10 left-2 z-50 w-84 transform-gpu ${popup.height}`}>
        <UpdatePopoverView label={popup.label} onClose={() => {}}>{popup.content}</UpdatePopoverView>
      </div>
    )}
  </div>
);

const langs = ['zh', 'en'] as const;
const panelTitle = (lang: SubjectLang) => (lang === 'en' ? en : zh).dashboard.versionAndUpdate;

const designFrames = frames(langs.flatMap((lang): FrameSpec[] => [
  {
    name: `now-${lang}`,
    title: `现在 · 桌面应用连接另一台电脑的实例：实例名旁边的版本号和面板都是这台电脑上的应用的，界面没有说 · ${lang}`,
    width: 1200,
    height: 800,
    status: 'shipped',
    route: '/',
    fixtures: windowFixtures(lang),
    render: appWindow(
      <DesktopAccountRowView state={projection(true)} onPresent={() => {}} localUpdater={null} />,
      // The shell's window today: 336 × 520 (w-84, h-130).
      { height: 'h-130', label: panelTitle(lang), content: <DesktopUpdatePanel state={clientState} act={async () => true} busy={false} install={install} close={() => {}} /> },
    ),
  },
  {
    name: `window-${lang}`,
    title: `提议 · 实例名旁边是实例的版本；面板里先是实例，再是这台电脑上的桌面应用，各有各的按钮 · ${lang}`,
    width: 1200,
    height: 800,
    status: 'approved',
    route: '/',
    fixtures: windowFixtures(lang),
    render: appWindow(
      <InstanceVersionRow lang={lang} name={INSTANCE} version={OLD} attention />,
      // The shell sizes its window to the panel, as tall as it needs; drawn here in the 336 × 520 it has today.
      { height: 'h-130', label: panelTitle(lang), content: <UpdateSubjectsPanel lang={lang} {...bothAvailable} /> },
    ),
  },
  {
    name: `window-updating-${lang}`,
    title: `提议 · 实例更新中，面板已关：版本号上转圈，直到更新完成 · ${lang}`,
    width: 1200,
    height: 800,
    status: 'approved',
    route: '/',
    fixtures: windowFixtures(lang),
    render: appWindow(<InstanceVersionRow lang={lang} name={INSTANCE} version={OLD} updating />),
  },
  {
    name: `states-${lang}`,
    title: `提议 · 面板的各个状态 · ${lang}`,
    width: 1472,
    height: 1760,
    status: 'approved',
    fixtures: { copy: copy(lang) },
    render: () => <States lang={lang} states={states} />,
  },
  {
    name: `badge-${lang}`,
    title: `提议 · 侧边栏底部一行的各个状态（第一行是现在）· ${lang}`,
    width: 420,
    height: 480,
    status: 'approved',
    fixtures: { copy: copy(lang) },
    render: () => <Badges lang={lang} rows={badgeRows(lang)} />,
  },
  {
    name: `narrow-${lang}`,
    title: `提议 · 窗口变窄时，从实例页面自己的版本号打开的是同一块面板 · ${lang}`,
    width: 480,
    height: 800,
    status: 'approved',
    route: '/settings/general',
    fixtures: settingsFixtures({ '/api/version': info() }, { copy: copy(lang) }),
    render: () => (
      <div className="relative h-full">
        {settingsPage('general', <SettingsGeneralPage />)()}
        <div className="absolute bottom-10 left-2 z-50 h-130 w-84 transform-gpu">
          <UpdatePopoverView label={panelTitle(lang)} onClose={() => {}}>
            <UpdateSubjectsPanel lang={lang} {...bothAvailable} />
          </UpdatePopoverView>
        </div>
      </div>
    ),
  },
  {
    name: `elsewhere-${lang}`,
    title: `对照 · 浏览器和手机不变：只有一样东西，面板就是它的；别处发起的更新这里也显示进行中 · ${lang}`,
    width: 800,
    height: 560,
    status: 'approved',
    fixtures: { copy: copy(lang) },
    render: () => {
      const l = label(lang);
      const elsewhere = instance({}, { installing: true });
      const actions = { check: { run: () => {}, disabled: true }, setPrereleases: () => {}, setAutoUpdate: () => {}, settingsBusy: true, close: () => {} };
      return (
        <div lang={lang} className="flex h-full items-start justify-center gap-8 bg-background p-8 text-foreground">
          <Captioned title={l('浏览器', 'Browser')} note={l('打开 alex，侧栏版本号', "alex's page, the version in the sidebar")}>
            <Card><UpdatePanel model={elsewhere} actions={actions} /></Card>
          </Captioned>
          <Captioned title={l('手机', 'Phone')} note={l('同一时刻', 'At the same moment')}>
            <Card><UpdatePanel model={elsewhere} actions={actions} /></Card>
          </Captioned>
        </div>
      );
    },
  },
]));

export default designFrames;
