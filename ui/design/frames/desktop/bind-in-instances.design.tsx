// AV-8, the owner's question (2026-11-23 21:53): where is the entry to binding, and could binding live
// on the entry page itself? Desktop's Instances page today (shipped), then the same page with this
// computer's binding under its own row (proposal). The claim dialog is unchanged.
import type { ReactNode } from 'react';
import { desktopEn as en, desktopZh as zh } from '@/i18n/desktop';
import { InstanceSwitcherView, type InstanceSwitcherViewProps } from '@/desktop/InstanceSwitcherView';
import { BindDialog, ThisComputerLine, type ComputerBinding } from '@/desktop/BindComputerView';
import type { DesktopClientState, Instance } from '@/desktop/bridge';
import { frames, type FrameSpec } from '../../frame';
import { DesktopAccountMenu } from '../../kit/desktop-account-menu';
import { workbenchFixtures } from '../workbench/fixtures';
import { copyFor } from './instances-fixtures';

const EMAIL = 'max@example.test';
const noop = () => {};
const LOCAL: Instance = { id: 'local', name: 'max', host: 'max-app.avibe.bot', url: 'https://max-app.avibe.bot', relationship: 'owner', owner_email: EMAIL, available: true };
const OTHERS: Instance[] = [
  { id: 'studio', name: 'Studio', host: 'studio-app.avibe.bot', url: 'https://studio-app.avibe.bot', relationship: 'owner', owner_email: EMAIL, available: true },
  { id: 'weekend', name: 'Weekend lab', host: 'weekend-app.avibe.bot', url: 'https://weekend-app.avibe.bot', relationship: 'owner', owner_email: EMAIL, available: false },
  { id: 'team', name: 'Product team', host: 'team-app.avibe.bot', url: 'https://team-app.avibe.bot', relationship: 'shared', owner_email: 'zhou.min@example.test', available: true },
];

/** `bound`: this computer has its domain (its catalog row). `others`: the account's other Avibes. */
function state({ bound, others, remote = false }: { bound: boolean; others: boolean; remote?: boolean }): DesktopClientState {
  const instances = [...(bound ? [LOCAL] : []), ...(others ? OTHERS : [])];
  return {
    version: 1, revision: 1, generation: 1,
    account: { status: 'signed_in', identity: { id: 'fixture', email: EMAIL }, email: EMAIL, gate: null, methods: ['totp'] },
    connection: remote
      ? { status: 'remote_connected', committed: { kind: 'remote', name: 'Studio', instance_id: 'studio', canonical_origin: 'https://studio-app.avibe.bot' }, pending: null }
      : { status: 'local', committed: { kind: 'local', name: 'Max 的 MacBook Pro', instance_id: bound ? 'local' : null }, pending: null },
    current_instance: remote ? OTHERS[0] : null,
    enrollment: { status: 'unknown', instance_id: null, error_code: null },
    local: { name: 'Max 的 MacBook Pro', instance_id: bound ? 'local' : null, slug: bound ? 'max' : null, remote_access: bound ? 'on' : 'off' },
    catalog: { status: 'ready', instances },
    desktop: { version: '3.2.8', update: { phase: 'current', latest: null, channel: 'stable', skippedVersion: null, interactive: false } },
    instance_update: null, consent: null, remote_auxiliary: 'local',
  };
}

const props = (s: DesktopClientState, extra: Partial<InstanceSwitcherViewProps> = {}): InstanceSwitcherViewProps => ({
  state: s, localInstanceId: s.local.instance_id ?? null, busy: false, pendingId: null, failure: null,
  onSelect: noop, onLocal: noop, onCancel: noop, onRefresh: noop, onConsent: noop, onSignOut: noop, onAdd: noop, ...extra,
});
const page = (children: ReactNode) => () => <div className="h-full overflow-y-auto bg-background px-10 py-5 text-foreground">{children}</div>;
const line = (binding: ComputerBinding) => ({ line: <ThisComputerLine binding={binding} onBind={noop} onRefresh={noop} onUpdates={noop} onSignInOther={noop} />, dim: binding.state === 'unknown' });

/** Today: the page as shipped. */
const today = (s: DesktopClientState) => page(<InstanceSwitcherView {...props(s)} />);
/** Proposal: this computer's binding under its row. */
const proposal = (s: DesktopClientState, binding: ComputerBinding, after?: ReactNode) => page(<>
  <InstanceSwitcherView {...props(s, { thisComputer: line(binding) })} />{after}
</>);

type State = { name: string; title: string; render: () => ReactNode; width?: number; height?: number };
const states: State[] = [
  { name: 'today-none-bound', title: '今天 · 「实例」：一台都没绑定（本机一行，只能回本机）', render: today(state({ bound: false, others: false })) },
  { name: 'today-one-bound', title: '今天 · 「实例」：本机绑定了', render: today(state({ bound: true, others: false })) },
  { name: 'today-several', title: '今天 · 「实例」：好几个实例', render: today(state({ bound: true, others: true })), height: 1000 },

  { name: 'proposal-unbound', title: '提议 · 本机没绑定：这一行下面一个按钮「绑定这台电脑」', render: proposal(state({ bound: false, others: false }), { state: 'unbound' }) },
  { name: 'proposal-unbound-several', title: '提议 · 本机没绑定，还有别的实例', render: proposal(state({ bound: false, others: true }), { state: 'unbound' }), height: 1000 },
  { name: 'proposal-bound', title: '提议 · 本机已绑定：第一行就是它，显示域名，标「本机」', render: proposal(state({ bound: true, others: true }), { state: 'bound', host: LOCAL.host, url: LOCAL.url }), height: 1000 },
  { name: 'proposal-other-account', title: '提议 · 绑定不在这个账号名下：一句话 + 用绑定它的账号登录 / 再看一次', render: proposal(state({ bound: false, others: true }), { state: 'otherAccount' }), height: 1000 },
  { name: 'proposal-upgrade', title: '提议 · 要先更新：一句话 + 检查更新', render: proposal(state({ bound: false, others: false }), { state: 'upgrade' }) },
  { name: 'proposal-repair', title: '提议 · 要在终端修复：一句话', render: proposal(state({ bound: false, others: false }), { state: 'repair' }) },
  { name: 'proposal-unknown', title: '提议 · 现在确认不了：这一行灰掉 + 再看一次', render: proposal(state({ bound: false, others: false }), { state: 'unknown' }) },
  { name: 'proposal-unavailable', title: '提议 · 现在不能绑定（死路）：一句话 + support 邮箱', render: proposal(state({ bound: false, others: false }), { state: 'unavailable' }) },
  { name: 'proposal-dialog', title: '提议 · 在这一屏按下后，领取弹窗打开在这一屏上（弹窗不变）', render: proposal(state({ bound: false, others: true }), { state: 'unbound' },
    <BindDialog step={{ kind: 'name', name: 'max', check: 'free' }} account={EMAIL}
      onNameChange={noop} onPick={noop} onConfirm={noop} onSendCode={noop} onCodeChange={noop} onSubmitCode={noop} onBeginSetup={noop} onSavedCodes={noop}
      onFactorStep={noop} onPasskey={noop} onRestart={noop} onRecover={noop} onUpdates={noop} onClose={noop} />), height: 1000 },
  { name: 'proposal-remote-current', title: '提议的弱点 · 正在用另一个实例时，本机那一行在「其他实例」里，绑定按钮夹在切换的行里', render: proposal(state({ bound: false, others: true, remote: true }), { state: 'unbound' }), height: 1000 },
  { name: 'proposal-narrow', title: '提议 · 窄窗口（640）', render: proposal(state({ bound: false, others: true }), { state: 'unbound' }), width: 640, height: 1100 },
  { name: 'proposal-account-menu', title: '提议 · 账号页只剩「谁登录着、在此设备退出」：收进「实例」底部账号行的小菜单', render: page(<div className="[&_[data-instance-account]]:hidden">
    <InstanceSwitcherView {...props(state({ bound: true, others: false }), { thisComputer: line({ state: 'bound', host: LOCAL.host, url: LOCAL.url }) })} />
    <DesktopAccountMenu email={EMAIL} />
  </div>), height: 1000 },
];

const designFrames = frames(states.flatMap(({ name, title, render, width = 1200, height = 800 }) => (['zh', 'en'] as const).map((lang): FrameSpec => ({
  name: `${name}-${lang}`,
  title: `${title} · ${lang}`,
  width,
  height,
  status: name.startsWith('today') ? 'shipped' : 'proposed',
  themes: ['light'],
  route: '/',
  fixtures: workbenchFixtures({}, { copy: copyFor(lang === 'en' ? en : zh) }),
  render,
}))));

export default designFrames;
