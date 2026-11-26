// 升级并重试 (owner, 2026-11-25): Avibe on this computer is too old for what was just asked (naming
// this computer, reading its binding). One sentence about this computer and 升级并重试; 继续使用本机
// second. While it upgrades: a spinner and 正在升级…. It did not finish, or is still too old: one
// sentence with the same button. In Instances, this computer's row says it needs an update to
// recognize its domain, with the same button.
import type { ReactNode } from 'react';
import { desktopEn as en, desktopZh as zh } from '@/i18n/desktop';
import { BindDialog, LocalTooOldLine, type BindStep } from '@/desktop/BindComputerView';
import { InstanceSwitcherView } from '@/desktop/InstanceSwitcherView';
import type { DesktopClientState, Instance } from '@/desktop/bridge';
import { frames, type FrameSpec } from '../../frame';
import { DesktopBackHeader } from '../../kit/desktop-back-header';
import { workbenchFixtures } from '../workbench/fixtures';
import { copyFor } from './instances-fixtures';

const EMAIL = 'max@example.test';
const noop = () => {};
const STUDIO: Instance = { id: 'studio', name: 'Studio', host: 'studio-app.avibe.bot', url: 'https://studio-app.avibe.bot', relationship: 'owner', owner_email: EMAIL, available: true };

/** Too old to say what it is bound to: no instance id (native does not guess which catalog row is it). */
const tooOld: DesktopClientState = {
  version: 1, revision: 1, generation: 1,
  account: { status: 'signed_in', identity: { id: 'fixture', email: EMAIL }, email: EMAIL, gate: null, methods: ['totp'] },
  connection: { status: 'local', committed: { kind: 'local', name: 'Max 的 MacBook Pro', instance_id: null }, pending: null },
  current_instance: null,
  enrollment: { status: 'unknown', instance_id: null, error_code: null },
  local: { name: 'Max 的 MacBook Pro', instance_id: null, slug: null, remote_access: 'on', runtime_too_old: true },
  catalog: { status: 'ready', instances: [STUDIO] },
  desktop: { version: '3.2.8', update: { phase: 'current', latest: null, channel: 'stable', skippedVersion: null, interactive: false } },
  instance_update: null, consent: null, remote_auxiliary: 'local',
};

/** The claim view: the shell's own document in front of the Workbench, and the dialog over it. */
const dialog = (step: BindStep) => () => <div className="flex h-full flex-col bg-background text-foreground">
  <div aria-hidden="true" className="h-7 shrink-0 bg-surface" />
  <DesktopBackHeader />
  <main className="min-h-0 flex-1" />
  <BindDialog step={step} account={EMAIL} busy={step.kind === 'tooOld' && step.phase === 'running'}
    onNameChange={noop} onPick={noop} onConfirm={noop} onSendCode={noop} onCodeChange={noop} onSubmitCode={noop} onBeginSetup={noop} onSavedCodes={noop}
    onFactorStep={noop} onPasskey={noop} onRestart={noop} onRecover={noop} onUpdates={noop} onUpgrade={noop} onClose={noop} />
</div>;

const instances = (phase?: 'running' | 'failed') => () => <div className="h-full overflow-y-auto bg-background px-10 py-5 text-foreground">
  <InstanceSwitcherView state={tooOld} localInstanceId={null} busy={false} pendingId={null} failure={null}
    onSelect={noop} onLocal={noop} onCancel={noop} onRefresh={noop} onConsent={noop} onSignOut={noop} onAdd={noop}
    thisComputer={{ line: <LocalTooOldLine phase={phase} onUpgrade={noop} /> }} />
</div>;

type State = { name: string; title: string; render: () => ReactNode; height?: number };
const states: State[] = [
  { name: 'dialog', title: '领取域名 · 这台电脑上的 Avibe 太旧：一句话，主按钮「升级并重试」，「继续使用本机」次要', render: dialog({ kind: 'tooOld' }) },
  { name: 'dialog-running', title: '领取域名 · 正在升级：转圈加「正在升级…」', render: dialog({ kind: 'tooOld', phase: 'running' }) },
  { name: 'dialog-failed', title: '领取域名 · 没升级成，或升级后仍太旧：一句话加同一个按钮', render: dialog({ kind: 'tooOld', phase: 'failed' }) },
  { name: 'instances', title: '「实例」· 这台电脑太旧、认不出它的域名：它自己那一行写明，加同一个按钮；不列在「其他实例」里', render: instances(), height: 760 },
  { name: 'instances-running', title: '「实例」· 正在升级', render: instances('running'), height: 760 },
  { name: 'instances-failed', title: '「实例」· 没升级成', render: instances('failed'), height: 760 },
];

const designFrames = frames(states.flatMap(({ name, title, render, height = 700 }) => (['zh', 'en'] as const).map((lang): FrameSpec => ({
  name: `${name}-${lang}`,
  title: `${title} · ${lang}`,
  width: 1200,
  height,
  status: 'shipped',
  themes: ['light'],
  route: '/',
  fixtures: workbenchFixtures({}, { copy: copyFor(lang === 'en' ? en : zh) }),
  render,
}))));

export default designFrames;
