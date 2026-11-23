// AV-8 · Desktop binds this computer (docs/plans/desktop-name-claim.md). Signing in binds nothing;
// the person binds this computer from the Desktop's Account page, and confirms the exact name and
// account there. Every state below is one answer native gives the Account page (`desktop_claim_review`,
// `observe_current_binding`, `desktop_account_recovery`); the page adds no state of its own.
// Verification reuses the sign-in's own steps (CodeEntry, SecondFactor) without "change email",
// "resend" or "another account", since this flow's account and code are fixed.
import type { ReactNode } from 'react';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { AccountDetailsView } from '@/desktop/AccountDetailsView';
import { BindComputerView, ComputerBindingCard, type AccountRecovery, type BindStep, type ComputerBinding } from '@/desktop/BindComputerView';
import { SetupCompleteView } from '@/desktop/SetupCompleteView';
import { DESIGN_NOW } from '../../shell/clock';
import { frames, type FrameSpec } from '../../frame';
import { workbenchFixtures } from '../workbench/fixtures';
import { copyFor } from './instances-fixtures';

const ACCOUNT = 'max@example.test';
const COMPUTER = 'Max 的 MacBook Pro';
const HOST = 'max-app.avibe.bot';
const noop = () => {};
const inDays = (days: number) => new Date(DESIGN_NOW + days * 86_400_000).toISOString().replace(/\.\d{3}Z$/, '.000Z');

/** The Account window's column as AccountApp lays it out under its header. */
const column = (children: ReactNode, centered = false) => () => <div className={centered
  ? 'flex h-full flex-col overflow-y-auto bg-background px-10 py-5 text-foreground'
  : 'h-full overflow-y-auto bg-background px-10 py-5 text-foreground'}>{children}</div>;

const account = (binding: ComputerBinding, recovery: AccountRecovery | null = null) => column(
  <AccountDetailsView email={ACCOUNT} localRemoteUrl={null} busy={false} onBack={noop} onSignOut={noop}>
    <ComputerBindingCard binding={binding} account={ACCOUNT} computer={COMPUTER} recovery={recovery}
      onBind={noop} onRefresh={noop} onUpdates={noop} onRecover={noop} />
  </AccountDetailsView>,
);

const bind = (step: BindStep, { busy = false, computer = COMPUTER } = {}) => column(
  <BindComputerView step={step} account={ACCOUNT} computer={computer} busy={busy}
    onNameChange={noop} onPick={noop} onConfirm={noop} onSendCode={noop} onCodeChange={noop} onSubmitCode={noop}
    onFactorStep={noop} onPasskey={noop} onRestart={noop} onUpdates={noop} onClose={noop} />,
  step.kind === 'code' || step.kind === 'factor',
);

type State = { name: string; title: string; render: () => ReactNode; height?: number; both?: boolean };

const states: State[] = [
  // The Account page: what this computer's binding is now.
  { name: 'account-reading', title: '账号页 · 正在读这台电脑的绑定（只读，不开始绑定）', render: account({ state: 'reading' }) },
  { name: 'account-unbound', title: '账号页 · 未绑定：入口，一个按钮', render: account({ state: 'unbound' }), both: true },
  { name: 'account-unbound-previous', title: '账号页 · 未绑定，上一次旧版绑定没有在本机完成', render: account({ state: 'unbound', previous: true }) },
  { name: 'account-bound', title: '账号页 · 已绑定在这个账号名下：域名、账号（远程访问开着，域名可点）', render: account({ state: 'bound', host: HOST, url: `https://${HOST}` }), both: true },
  { name: 'account-bound-off', title: '账号页 · 已绑定，远程访问关着：仍是「已绑定」，域名不可点', render: account({ state: 'bound', host: HOST, url: null }) },
  { name: 'account-unconfirmed', title: '账号页 · 已绑定，但没能确认在这个账号名下', render: account({ state: 'unconfirmed', host: 'studio-app.avibe.bot' }) },
  { name: 'account-pending', title: '账号页 · 上一次（旧版）绑定还在进行', render: account({ state: 'pending' }) },
  { name: 'account-repair', title: '账号页 · 旧版绑定记录需要在终端修复', render: account({ state: 'repair' }) },
  { name: 'account-upgrade', title: '账号页 · 需要先更新本机的 Avibe', render: account({ state: 'upgrade' }) },
  { name: 'account-unknown', title: '账号页 · 读不到绑定（不是「未绑定」）', render: account({ state: 'unknown' }) },
  { name: 'account-recovery', title: '账号页 · 普通账号恢复：上一次验证已通过、还没保存完（按「继续」才恢复）', render: account({ state: 'unbound' }, 'available') },
  { name: 'account-recovery-blocked', title: '账号页 · 普通账号恢复：系统凭据存储写不进去', render: account({ state: 'unbound' }, 'blocked') },

  // Naming and confirming, one screen: the account, this computer and the exact domain, then 绑定.
  { name: 'opening', title: '起名 · 正在打开（读 avibe.bot 的建议）', render: bind({ kind: 'opening' }) },
  { name: 'name', title: '起名并确认 · 预填邮箱前缀，可改，域名写全', render: bind({ kind: 'name', name: 'max', check: 'free' }), both: true },
  { name: 'name-second', title: '起名 · 同一账号的第二台电脑：avibe.bot 预填「前缀-电脑名」', render: bind({ kind: 'name', name: 'max-macmini', check: 'free' }, { computer: 'Mac mini（办公室）' }) },
  { name: 'name-empty', title: '起名 · avibe.bot 没有建议：空着，按钮不可用', render: bind({ kind: 'name', name: '', check: 'invalid' }) },
  { name: 'name-checking', title: '起名 · 改了名称，正在查', render: bind({ kind: 'name', name: 'maxwell', check: 'checking' }) },
  { name: 'name-taken', title: '起名 · 已被占用：只提示，不加数字', render: bind({ kind: 'name', name: 'max', check: 'taken' }) },
  { name: 'name-reclaim', title: '起名 · 这是自己放弃过的名字，可以用回来', render: bind({ kind: 'name', name: 'maxlab', check: 'reclaim' }) },
  { name: 'limit', title: '起名 · 名字到了上限，有可取回的自己的名字和最早释放日期', render: bind({ kind: 'limit', name: 'max', held: 5, earliest: inDays(11), eligible: [{ prefix: 'maxlab', host: 'maxlab-app.avibe.bot' }] }) },
  { name: 'limit-plain', title: '起名 · 名字到了上限，avibe.bot 没给明细', render: bind({ kind: 'limit', name: 'max', held: null, earliest: null, eligible: [] }) },
  { name: 'existing', title: '确认 · 这台电脑在这个账号下已有域名：只能确认，不能改', render: bind({ kind: 'existing', host: 'mac-7f3k-app.avibe.bot' }) },
  { name: 'binding', title: '确认之后 · 正在绑定', render: bind({ kind: 'name', name: 'max', check: 'free' }, { busy: true }) },

  // avibe.bot wants recent proof: a separate choice to send, the sign-in's code step, a second factor.
  { name: 'proof', title: '需要邮件验证码 · 发送是单独的一步', render: bind({ kind: 'proof' }) },
  { name: 'code', title: '邮件验证码 · 登录同款，没有「修改邮箱」和「重新发送」', render: bind({ kind: 'code', code: '' }), height: 900 },
  { name: 'factor-totp', title: '两步验证 · 身份验证器', render: bind({ kind: 'factor', method: 'totp', methods: ['passkey', 'totp'], code: '' }), height: 900 },
  { name: 'factor-passkey', title: '两步验证 · 通行密钥', render: bind({ kind: 'factor', method: 'passkey', methods: ['passkey', 'totp'], code: '' }), height: 900 },
  { name: 'again', title: '验证之后 · 重新读同一张票，再确认一次', render: bind({ kind: 'name', name: 'max', check: 'free', again: true }) },

  // Ends.
  { name: 'bound', title: '已绑定 · 域名、账号、电脑（不说「已连通」）', render: bind({ kind: 'bound', host: HOST }), both: true },
  { name: 'conflict', title: '这台电脑属于另一个账号（owner_conflict）：什么都没改，换账号登录', render: bind({ kind: 'conflict' }) },
  { name: 'upgrade', title: '需要先更新本机的 Avibe', render: bind({ kind: 'upgrade' }) },
  { name: 'problem-unreachable', title: '出错 · 连不上 avibe.bot：重新开始', render: bind({ kind: 'problem', problem: 'unreachable' }) },
  { name: 'problem-code', title: '出错 · 验证码不对或已过期：重新开始', render: bind({ kind: 'problem', problem: 'codeInvalid' }) },
  { name: 'problem-busy', title: '出错 · 另一项更改在进行：返回', render: bind({ kind: 'problem', problem: 'busy' }) },
  { name: 'problem-lost', title: '出错 · 确认后回复丢了，无法再取：重新开始', render: bind({ kind: 'problem', problem: 'lost' }) },

  // Setup's last screen: signing in no longer registers this computer.
  { name: 'setup-complete', title: '设置完成 · 登录不再自动登记本机', render: column(<SetupCompleteView onLocal={noop} onSignIn={noop} />) },
];

const designFrames = frames(states.flatMap(({ name, title, render, height = 800, both }) => (['zh', 'en'] as const).map((lang): FrameSpec => ({
  name: `${name}-${lang}`,
  title: `${title} · ${lang}`,
  width: 1200,
  height,
  status: 'proposed',
  themes: both ? ['light', 'dark'] : ['light'],
  route: '/',
  fixtures: workbenchFixtures({}, { copy: copyFor(lang === 'en' ? en : zh) }),
  render,
}))));

export default designFrames;
