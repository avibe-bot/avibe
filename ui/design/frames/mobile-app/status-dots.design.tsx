// Approved 2026-11-17. The implementation replaces the proposal kit with the
// actual page/sheet consumers. Waiting rows keep their available tone; only
// opening is amber. Long names wrap as in the approved 375px frame.
import type { ReactNode } from 'react';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { InstanceSheetPage, InstancesPage } from '@/mobile/MobileOnboarding';
import type { MobileInstance, MobileSessionState } from '@/mobile/bridge';
import { frames, type FrameSpec } from '../../frame';
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO } from '../../shell/fixtures';
import { agentsApi, mainRunningFixtures } from '../../shell/teamFixtures';
import { copyFor } from '../desktop/instances-fixtures';
import { homeFactsApi, workbenchFixtures } from '../workbench/fixtures';
import { SheetLanguage } from '../../kit/mobile-instance-sheet';

const email = 'max@example.com';
const SMALL = { width: 375, height: 667, safeArea: { top: 20, bottom: 0 } };

const instance = (id: string, name: string, extra: Partial<MobileInstance> = {}): MobileInstance => ({
  id,
  name,
  host: `${id}-app.avibe.bot`,
  url: `https://${id}-app.avibe.bot`,
  relationship: 'owner',
  owner_email: null,
  available: true,
  ...extra,
});

const max = instance('max', 'max');
const driver = instance('avibe-e2e-driver', 'avibe-e2e-driver');
const preview = instance('avibe-preview', 'avibe-preview');
const win = instance('max-win', 'max-win', { available: false });
const feedback = instance('avibe-feedback', 'avibe-feedback');
const octoarms = instance('octoarms', 'octoarms');
const teamTest = instance('team-test', 'team-test', { available: false, relationship: 'shared', owner_email: 'Zhou Min' });
const others = [driver, preview, win, feedback, octoarms, teamTest];
const allOnline = [driver, preview, feedback, octoarms, { ...win, available: true }, { ...teamTest, available: true }];

const longCurrent = { ...max, name: 'max 的主力工作站 · Mac Studio M4 Max（书房）' };
const longOthers = [
  { ...driver, name: 'avibe-e2e-driver-regression-master-unified', host: 'avibe-e2e-driver-regression-master.avibe.bot' },
  { ...teamTest, name: 'Product research team workspace for the shared regression runs' },
  { ...win, name: 'max-win-11-parallels-virtual-machine-on-the-studio' },
];
// Names that start in an ideograph centre their dot on the cap height, Latin ones on the x-height.
const cjkCurrent = { ...max, name: '小麦的 Mac' };
const mixedOthers = [
  instance('studio', '工作室 Studio'),
  driver,
  { ...teamTest, available: true, name: '跨时区产品研发团队 — 共享回归环境与评审工作站（书房）' },
  { ...win, name: 'max-win' },
];

function nativeFixtures(lang: 'zh' | 'en') {
  return workbenchFixtures({
    ...agentsApi,
    ...homeFactsApi({ model: true, push: false, computerUse: true, chatApps: true, paired: true }),
    '/api/session': { remote: true, authenticated: true, email },
  }, {
    ...mainRunningFixtures,
    nativeShell: IOS_APP_CAPABILITIES,
    copy: copyFor(lang === 'en' ? en : zh),
  });
}

function frame(
  name: string,
  title: string,
  render: () => ReactNode,
  options: { lang?: 'zh' | 'en'; size?: typeof SMALL | typeof IPHONE_17_PRO; status?: FrameSpec['status'] } = {},
): FrameSpec {
  const lang = options.lang ?? 'zh';
  return {
    name,
    title,
    ...(options.size ?? IPHONE_17_PRO),
    status: options.status ?? 'shipped',
    themes: ['light', 'dark'],
    route: '/',
    fixtures: nativeFixtures(lang),
    render: () => <SheetLanguage lang={lang}>{render()}</SheetLanguage>,
  };
}


const noop = () => {};
const session = (current = max, list = others): Extract<MobileSessionState, { status: 'signedIn' }> => ({
  status: 'signedIn', account: { id: 'account', email }, selected_instance_id: current.id, instances: [current, ...list],
});
type PageProps = Parameters<typeof InstancesPage>[0];
const rows = (overrides: Partial<PageProps['rows']> = {}): PageProps['rows'] => ({
  openingId: null, failure: null, expandedId: null, onOpen: noop, ...overrides,
});
const page = (props: Partial<PageProps> = {}) => () => (
  <InstancesPage session={session()} busy={null} locked={Boolean(props.rows?.openingId)} errorKey={null}
    rows={rows()} onRefresh={noop} onAccount={noop} onAdd={noop} onReopen={noop} {...props} />
);
const designFrames = frames([
  frame('recommended', 'Approved · status dots in the instance page', page({ rows: rows({ openingId: preview.id }) })),
  frame('recommended-en', 'Approved · status dots in the instance page', page({ rows: rows({ openingId: preview.id }) }), { lang: 'en' }),
  frame('all-online', 'All instances online', page({ session: session(max, allOnline) })),
  frame('offline-expanded', 'Offline instance with its explanation', page({ rows: rows({ expandedId: win.id }) })),
  frame('open-failed', 'Open failure retains its availability dot', page({ rows: rows({ failure: { id: feedback.id, reason: 'mobileApp.sheet.openFailed' } }) })),
  frame('stale', 'Refresh failure retains the cached list', page({ errorKey: 'mobileApp.errors.network' })),
  frame('mixed-scripts', 'Latin- and ideograph-led names, each dot centred on its own first line', page({ session: session(cjkCurrent, mixedOthers) }), { size: SMALL }),
  frame('long-names', 'Long names wrap within the name column', page({ session: session(longCurrent, longOthers), rows: rows({ openingId: driver.id }) }), { size: SMALL }),
  frame('sheet', 'The sheet uses the same status rows', () => (
    <InstanceSheetPage session={session()} locked refreshing={false} stale={false}
      rows={rows({ openingId: preview.id })} onClose={noop} onRefresh={noop} onAdd={noop} />
  )),
  // The current block follows the connection: over a live instance it is green whatever the catalog
  // last said; with nothing open, gold while it reopens and grey when reopening failed.
  frame('current-live-sheet', 'Over a live instance the catalog calls offline: the current block stays green', () => (
    <InstanceSheetPage session={session({ ...max, available: false })} locked={false} refreshing={false} stale={false}
      rows={rows()} onClose={noop} onRefresh={noop} onAdd={noop} />
  )),
  frame('current-live-page', 'The page over a live instance (Back returns to it): green whatever the catalog says', page({ session: session({ ...max, available: false }), onBack: noop })),
  frame('current-reopening', 'Nothing open: the current block reopening is gold', page({ locked: true, rows: rows({ openingId: max.id }) })),
  frame('current-reopen-failed', 'Nothing open: reopening failed, the current block is grey', page({ rows: rows({ failure: { id: max.id, reason: 'mobileApp.sheet.openFailed' } }) })),
]);

export default designFrames;
