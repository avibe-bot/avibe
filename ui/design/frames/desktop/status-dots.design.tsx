// Shipped 2026-11-19: a run-status dot before every name in the Desktop instance list, carrying the
// App's approved semantics (`mobile-app/status-dots/*`, `ui/src/mobile/InstanceList.tsx`) across.
// Drawn with the shipped `InstanceSwitcherView`; the dot is the shipped `StatusDot` and its accessible
// label the shipped `desktopClient.online`. No new component, token, copy, or backend field.
//
// One rule decides the tone, in this order:
//   opening            → connecting (gold)  — only the row this computer is opening right now
//   offline            → stopped   (grey)   — the catalog says unavailable, or the current remote
//                                             is unreachable; the row already reads 「离线 · …」
//   available === true → running   (green)  — the catalog last reported it reachable
//   otherwise          → no dot              — the column keeps its width so names stay aligned
// This computer's row always opens, so it is never offline: green when its catalog entry reports
// it reachable, no dot when the entry is missing or reports it unavailable.
//
// The list's ordinary states already draw the dot in their own frames: green, grey and shared in
// `desktop/instances/instances-*`, gold in `switching-*`, an unreachable current remote in `offline-*`,
// a shared current or offline instance in `current-shared-*` / `offline-shared-*`. These frames hold
// only the rows that draw no dot, which no other frame shows.
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
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

/** This computer is in the catalog but reports it unavailable: the row still opens, so no grey and no 「离线」. */
const localUnavailable = (lang: Lang): InstanceSwitcherViewProps => {
  const props = propsFor('instances', lang);
  return { ...props, state: { ...props.state, catalog: { ...props.state.catalog, instances: props.state.catalog.instances.map((item) => item.id === 'local' ? { ...item, available: false } : item) } } };
};

/** This computer is not in the catalog at all: no fact about it, so no dot. */
const localUnlisted = (lang: Lang): InstanceSwitcherViewProps => {
  const props = propsFor('instances', lang);
  return { ...props, state: { ...props.state, catalog: { ...props.state.catalog, instances: props.state.catalog.instances.filter((item) => item.id !== 'local') } } };
};

/** A committed remote the catalog does not list: native proves identity, nothing proves reach. */
const currentUnlisted = (lang: Lang): InstanceSwitcherViewProps => {
  const props = propsFor('instances', lang);
  return {
    ...props,
    state: {
      ...props.state,
      connection: { status: 'remote_connected', committed: { kind: 'remote', name: 'Studio', instance_id: 'studio', canonical_origin: 'https://studio-app.avibe.bot' }, pending: null },
      current_instance: null,
      catalog: { ...props.state.catalog, instances: props.state.catalog.instances.filter((item) => item.id !== 'studio') },
    },
  };
};

const scenarios: [string, string, (lang: Lang) => InstanceSwitcherViewProps][] = [
  ['local-unavailable', '本机在目录里但报不可用：本机仍可打开，不画灰点，点列仍占位', localUnavailable],
  ['local-unlisted', '本机不在目录里：不画点，点列仍占位，名称照常对齐', localUnlisted],
  ['current-unlisted', '当前远端不在目录里：同样不画点，身份由原生证明，可达性无来源', currentUnlisted],
];

const designFrames = frames([
  ...scenarios.map(([name, title, props]) => frame(name, title, 'zh', props('zh'))),
  // English carries the same states; dark is enough to check the copy and the dot together.
  ...scenarios.map(([name, title, props]) => frame(name, title, 'en', props('en'), ['dark'])),
]);

export default designFrames;
