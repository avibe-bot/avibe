// AV-8, one home (product, 2026-11-23 22:03): remote access lives in Settings › Devices; its one action
// is 「开启远程访问」; its one dialog is 「领取你的专属域名」. In the Desktop the dialog is the shell's own
// trusted sheet: the Workbench asks the shell to show it (avibe://desktop/claim, local Workbench only),
// the shell brings its bundled document to the front with the dialog, binds there, and returns to the
// same Workbench page. A signed-out App opens the dialog on its own sign-in, then the name.
import type { ReactNode } from 'react';
import { desktopEn as en, desktopZh as zh } from '@/i18n/desktop';
import { DevicesPageView } from '@/components/devices/DevicesPageView';
import type { Device } from '@/components/devices/types';
import { SetupRemoteAccessView } from '@/components/onboarding/SetupRemoteAccessView';
import { AccountDetailsView } from '@/desktop/AccountDetailsView';
import { BindDialog, ComputerBindingCard, type BindStep } from '@/desktop/BindComputerView';
import type { DesktopClientState } from '@/desktop/bridge';
import { frames, type FrameSpec } from '../../frame';
import { ClaimDialogSignIn, DesktopBackHeader } from '../../kit/desktop-back-header';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import { setupFixtures } from '../setup/fixtures';
import { copyFor } from './instances-fixtures';

const EMAIL = 'max@example.test';
const noop = () => {};
const macbook: Device = {
  id: 'host', group: 'mine', kind: 'host', name: 'Max 的 MacBook Pro', client: { kind: 'runtime', version: '3.2.8', platform: 'macOS' }, online: {},
  functions: [{ kind: 'computerUse', status: 'ready' }, { kind: 'notifications', on: true, canChange: true }], self: true,
};

const copy = (lang: 'zh' | 'en') => copyFor(lang === 'en' ? en : zh);

const signedOut: DesktopClientState = {
  version: 1, revision: 1, generation: 1,
  account: { status: 'signed_out', identity: null, email: null, gate: null, methods: [] },
  connection: { status: 'local', committed: { kind: 'local', name: 'Max 的 MacBook Pro', instance_id: null }, pending: null },
  enrollment: { status: 'unknown', instance_id: null, error_code: null },
  catalog: { status: 'idle', instances: [] },
  local: { name: 'Max 的 MacBook Pro', remote_access: 'off' },
  desktop: { version: '3.2.8', update: { phase: 'current', latest: null, channel: 'stable', skippedVersion: null, interactive: false } },
  consent: null, remote_auxiliary: 'local', instance_update: null,
};

const dialog = (step: BindStep) => <BindDialog step={step} account={EMAIL}
  signIn={<ClaimDialogSignIn state={signedOut} />}
  onNameChange={noop} onPick={noop} onConfirm={noop} onSendCode={noop} onCodeChange={noop} onSubmitCode={noop} onBeginSetup={noop} onSavedCodes={noop}
  onFactorStep={noop} onPasskey={noop} onRestart={noop} onRecover={noop} onUpdates={noop} onClose={noop} />;

/** The Devices page, remote access off, as the Desktop's Workbench shows it. */
const devicesOff = () => <DevicesPageView reach={{ state: 'off', surface: 'desktop' }} devices={[macbook]} />;
/** The shell's trusted sheet as it can be today: its bundled document brought to the front (the
 *  Workbench hidden behind it, kept as it was), with the dialog. */
const sheet = (step: BindStep) => () => <div className="flex h-full flex-col bg-background text-foreground">
  <div aria-hidden="true" className="h-7 shrink-0 bg-surface" />
  <DesktopBackHeader />
  <main className="min-h-0 flex-1" />
  {dialog(step)}
</div>;

type State = { name: string; title: string; render: () => ReactNode; route?: string; setup?: boolean; height?: number };
const states: State[] = [
  // Settings › Devices, remote access off: one action.
  { name: 'devices-off', title: '设置 › 设备 · 远程访问关着：一个动作「开启远程访问」', route: '/settings/remote-access', render: settingsPage('remote-access', devicesOff()) },
  { name: 'devices-off-sheet', title: '按下后（这次能做的）：壳把自己的受信界面换到前面，弹窗在上面；做完回到同一个「设备」页', render: sheet({ kind: 'name', name: 'max', check: 'free' }) },
  { name: 'devices-off-overlay', title: '按下后（理想，要新做窗口叠层，原生审查人不建议这次做）：弹窗直接盖在「设备」页上', route: '/settings/remote-access', render: () => <>{settingsPage('remote-access', devicesOff())()}{dialog({ kind: 'name', name: 'max', check: 'free' })}</> },
  // Setup's optional step.
  { name: 'setup-step', title: '设置向导 · 可选的一步：开启远程访问，或者跳过', route: '/setup', setup: true, render: () => <SetupRemoteAccessView surface="desktop" onTurnOn={noop} onSkip={noop} /> },
  { name: 'setup-signed-in-sheet', title: '设置向导 · 已登录：按下后，受信界面里的弹窗直接从起名开始', render: sheet({ kind: 'name', name: 'max', check: 'free' }) },
  { name: 'setup-signed-out-sheet', title: '设置向导 · 未登录（新装）：弹窗第一面是登录（弹窗自己的标题，同样的登录方式，「取消」），登好以后在同一个弹窗里起名', render: sheet({ kind: 'signIn' }), height: 900 },
  // The Account page keeps only the account.
  { name: 'account-bound', title: '「账号」页 · 只留账号本身；这台电脑绑定了，就显示一行只读的域名', render: () => <div className="h-full overflow-y-auto bg-background px-10 py-5 text-foreground">
    <AccountDetailsView email={EMAIL} busy={false} onBack={noop} onSignOut={noop}>
      <ComputerBindingCard binding={{ state: 'bound', host: 'max-app.avibe.bot', url: 'https://max-app.avibe.bot' }} computer="Max 的 MacBook Pro" onBind={noop} onRefresh={noop} onUpdates={noop} onSignInOther={noop} />
    </AccountDetailsView>
  </div> },
  { name: 'account-unbound', title: '「账号」页 · 没绑定时什么都不显示（绑定在「设备」）', render: () => <div className="h-full overflow-y-auto bg-background px-10 py-5 text-foreground">
    <AccountDetailsView email={EMAIL} busy={false} onBack={noop} onSignOut={noop} />
  </div> },
];

const designFrames = frames(states.flatMap(({ name, title, render, route = '/', setup, height = 800 }) => (['zh', 'en'] as const).map((lang): FrameSpec => ({
  name: `${name}-${lang}`,
  title: `${title} · ${lang}`,
  width: 1200,
  height,
  status: 'proposed',
  themes: ['light'],
  route,
  fixtures: setup ? { ...setupFixtures({}), copy: copy(lang) } : settingsFixtures({}, { copy: copy(lang) }),
  render,
}))));

export default designFrames;
