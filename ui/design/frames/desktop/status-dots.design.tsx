// Shipped 2026-11-19: a run-status dot before every name in the Desktop instance list, carrying the
// App's approved semantics (`mobile-app/status-dots/*`, `ui/src/mobile/InstanceList.tsx`) across.
// Drawn with the shipped `InstanceSwitcherView`; the dot is the shipped `StatusDot`, centred on the
// letters of the name's first line (`LineGlyph`). No new token, copy, or backend field.
//
// The current row (「正在使用」) follows the connection, as the sidebar's bottom row does, with the
// bottom row's labels (Max, 2026-11-19 09:26):
//   switching or opening  → connecting (gold)   「连接中」
//   remote_unreachable    → stopped   (grey)    「当前实例已断开」; the row reads 「离线 · …」
//   otherwise             → running   (green)   「当前实例已连接」 — this computer, or a connected
//                                                remote whether or not the catalog lists it
// Every other row follows the catalog, labelled 「在线」 only when green:
//   opening            → connecting (gold)  — only the row this computer is opening right now
//   available === false → stopped  (grey)   — the row already reads 「离线 · …」
//   available === true  → running  (green)
//   otherwise           → no dot           — the column keeps its width so names stay aligned
// This computer among the other rows always opens, so it is never grey: green when its catalog
// entry reports it reachable, no dot when the entry is missing or reports it unavailable.
//
// The list's ordinary states already draw the dot in their own frames: green, grey and shared in
// `desktop/instances/instances-*`, gold in `switching-*`, an unreachable current remote in `offline-*`,
// a shared current or offline instance in `current-shared-*` / `offline-shared-*`. These frames hold
// the rows whose dot no other frame decides.
import { desktopEn as en, desktopZh as zh } from '@/i18n/desktop';
import { InstanceSwitcherView, type InstanceSwitcherViewProps } from '@/desktop/InstanceSwitcherView';
import { frames, type FrameSpec } from '../../frame';
import { copyFor, switcherViewProps } from './instances-fixtures';
import { workbenchFixtures, homeFactsApi } from '../workbench/fixtures';
import { agentsApi } from '../agents/fixtures';
import type { DesktopInstanceState } from '../../kit/desktop-instances';

type Lang = 'zh' | 'en';

/** The `desktop/instances` frames' own fixtures, each changed only in the row it is about. */
const propsFor = (state: DesktopInstanceState, lang: Lang): InstanceSwitcherViewProps => {
  const value = switcherViewProps({ state, lang, connection: 'signed-in', updateAvailable: false });
  if (!value) throw new Error(`no switcher fixture for ${state}`);
  return value;
};

const canvas = (lang: Lang) => workbenchFixtures({
  ...agentsApi(),
  ...homeFactsApi({ model: true, push: true, computerUse: true, chatApps: true, paired: true }),
  '/api/session': { remote: false },
  '/api/version': { current: '3.2.4', latest: '3.2.4', has_update: false, error: null },
}, { copy: copyFor(lang === 'en' ? en : zh) });

function frame(name: string, title: string, lang: Lang, props: InstanceSwitcherViewProps, themes: FrameSpec['themes'] = ['light', 'dark']): FrameSpec {
  return {
    name: `${name}-${lang}`,
    title: `${title} · ${lang}`,
    width: 1200, height: 800, themes, status: 'shipped', route: '/',
    fixtures: canvas(lang),
    render: () => <div className="h-full overflow-y-auto bg-background px-10 py-5 text-foreground"><InstanceSwitcherView {...props} /></div>,
  };
}

const studio = { kind: 'remote', name: 'Studio', instance_id: 'studio', canonical_origin: 'https://studio-app.avibe.bot' } as const;

/** The list's own fixture with this computer's catalog entry changed, optionally while on Studio. */
const variant = (lang: Lang, local: 'unavailable' | 'unlisted', onStudio: boolean): InstanceSwitcherViewProps => {
  const props = propsFor('instances', lang);
  const instances = local === 'unlisted' ? props.state.catalog.instances.filter((item) => item.id !== 'local')
    : props.state.catalog.instances.map((item) => item.id === 'local' ? { ...item, available: false } : item);
  const studioItem = props.state.catalog.instances.find((item) => item.id === 'studio') ?? null;
  return { ...props, state: { ...props.state, catalog: { ...props.state.catalog, instances },
    ...(onStudio ? { connection: { status: 'remote_connected' as const, committed: studio, pending: null }, current_instance: studioItem } : {}) } };
};

/** A connected remote the catalog does not list: native proves identity, the connection proves reach. */
const currentUnlisted = (lang: Lang): InstanceSwitcherViewProps => {
  const props = propsFor('instances', lang);
  return {
    ...props,
    state: {
      ...props.state,
      connection: { status: 'remote_connected', committed: studio, pending: null },
      current_instance: null,
      catalog: { ...props.state.catalog, instances: props.state.catalog.instances.filter((item) => item.id !== 'studio') },
    },
  };
};

const scenarios: [string, string, (lang: Lang) => InstanceSwitcherViewProps][] = [
  ['current-local-unlisted', '正在使用本机、本机不在目录里：按连接画绿', (lang) => variant(lang, 'unlisted', false)],
  ['local-unavailable', '正在使用 Studio，本机在目录里但报不可用：本机仍可打开，不画灰点，点列仍占位', (lang) => variant(lang, 'unavailable', true)],
  ['local-unlisted', '正在使用 Studio，本机不在目录里：不画点，点列仍占位，名称照常对齐', (lang) => variant(lang, 'unlisted', true)],
  ['current-unlisted', '当前远端不在目录里但连着：按连接画绿，不判离线', currentUnlisted],
];

const designFrames = frames([
  ...scenarios.map(([name, title, props]) => frame(name, title, 'zh', props('zh'))),
  // English carries the same states; dark is enough to check the copy and the dot together.
  ...scenarios.map(([name, title, props]) => frame(name, title, 'en', props('en'), ['dark'])),
]);

export default designFrames;
