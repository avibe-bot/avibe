// AV-8 · Desktop binds this computer (docs/plans/desktop-name-claim.md). Signing in binds nothing;
// the person binds this computer from the Desktop's Account page, and confirms the exact name and
// account there. Every state below is one answer native gives the Account page (`desktop_claim_review`,
// `observe_current_binding`, `desktop_account_recovery`); the page adds no state of its own.
// Verification reuses the sign-in's own steps (CodeEntry, SecondFactor) without "change email",
// "resend" or "another account", since this flow's account and code are fixed.
import type { ReactNode } from 'react';
import { desktopEn as en, desktopZh as zh } from '@/i18n/desktop';
import { AccountDetailsView } from '@/desktop/AccountDetailsView';
import { BindDialog, ComputerBindingCard, type BindStep, type ComputerBinding } from '@/desktop/BindComputerView';
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

const card = (binding: ComputerBinding) => <ComputerBindingCard binding={binding} computer={COMPUTER}
  onBind={noop} onRefresh={noop} onUpdates={noop} onSignInOther={noop} />;
const account = (binding: ComputerBinding) => column(
  <AccountDetailsView email={ACCOUNT} busy={false} onBack={noop} onSignOut={noop}>{card(binding)}</AccountDetailsView>,
);

/** The dialog over the Account page, as the person sees it after pressing Bind this computer. */
const bind = (step: BindStep, { busy = false } = {}) => column(
  <AccountDetailsView email={ACCOUNT} busy={false} onBack={noop} onSignOut={noop}>
    {card({ state: 'unbound' })}
    <BindDialog step={step} account={ACCOUNT} busy={busy}
      onNameChange={noop} onPick={noop} onConfirm={noop} onSendCode={noop} onCodeChange={noop} onSubmitCode={noop} onBeginSetup={noop} onSavedCodes={noop}
      onFactorStep={noop} onPasskey={noop} onRestart={noop} onRecover={noop} onUpdates={noop} onClose={noop} />
  </AccountDetailsView>,
);

const HELD = [{ prefix: 'maxlab', host: 'maxlab-app.avibe.bot' }, { prefix: 'maxhome', host: 'maxhome-app.avibe.bot' }];
type State = { name: string; title: string; render: () => ReactNode; height?: number; width?: number; both?: boolean };

// Why a name cannot be had, one reason at a time (product, 2026-11-25), at the desktop's width and
// at 390, where no sentence may wrap.
const REASONS: [string, string, string][] = [
  ['characters', '用了不允许的字符', 'max well'],
  ['short', '太短', 'm'],
  ['long', '太长（47 个字符）', 'a'.repeat(47)],
  ['hyphen-ends', '以连字符开头或结尾', 'max-'],
  ['double-hyphen', '第 3、4 个字符都是连字符', 'ab--cd'],
];
const reasonStates: State[] = REASONS.flatMap(([key, title, name]) => [
  { name: `name-invalid-${key}`, title: `弹窗 · 不合规则：${title}`, render: bind({ kind: 'name', name, check: 'invalid' }) },
  { name: `name-invalid-${key}-390`, title: `弹窗 · 不合规则：${title} · 390 宽`, render: bind({ kind: 'name', name, check: 'invalid' }), width: 390, height: 844 },
]);

const states: State[] = [
  // The card answers only: can I use it, and what do I do.
  { name: 'account-reading', title: '卡 · 正在读（只读）', render: account({ state: 'reading' }) },
  { name: 'account-unbound', title: '卡 · 未绑定：一个按钮（旧版没完成或还在进行的尝试也是这样）', render: account({ state: 'unbound' }), both: true },
  { name: 'account-bound', title: '卡 · 已绑定：域名（远程访问开着时可点）；读不到账号列表时也这样显示，后台再查', render: account({ state: 'bound', host: HOST, url: `https://${HOST}` }), both: true },
  { name: 'account-bound-off', title: '卡 · 已绑定，远程访问关着', render: account({ state: 'bound', host: HOST, url: null }) },
  { name: 'account-other-account', title: '卡 · 绑定在另一个账号下：用那个账号登录（保留：要人做不同的事）', render: account({ state: 'otherAccount' }) },
  { name: 'account-unknown', title: '卡 · 现在确认不了：再看一次', render: account({ state: 'unknown' }) },
  { name: 'account-upgrade', title: '卡 · 要先更新（保留：要人做不同的事）', render: account({ state: 'upgrade' }) },
  { name: 'account-unavailable', title: '卡 · 旧版记录已结束、原生还不能替换：死路，请联系我们（与墓碑同一句）', render: account({ state: 'unavailable' }) },
  { name: 'account-repair', title: '卡 · 要在终端修复（保留，等 #4133）', render: account({ state: 'repair' }) },

  // The dialog: claim your own domain.
  { name: 'opening', title: '弹窗 · 正在打开', render: bind({ kind: 'opening' }) },
  { name: 'name', title: '弹窗 · 预填邮箱前缀：可用', render: bind({ kind: 'name', name: 'max', check: 'free' }), both: true },
  { name: 'name-held', title: '弹窗 · 自己保留着的名字放在输入框下面', render: bind({ kind: 'name', name: 'max-macmini', check: 'free', held: HELD }) },
  { name: 'name-checking', title: '弹窗 · 正在查', render: bind({ kind: 'name', name: 'maxwell', check: 'checking' }) },
  { name: 'name-taken', title: '弹窗 · 已被占用：不加数字', render: bind({ kind: 'name', name: 'max', check: 'taken' }) },
  { name: 'name-reclaim', title: '弹窗 · 你用过的：只说这个词，小按钮只放其他保留着的名字', render: bind({ kind: 'name', name: 'maxlab', check: 'reclaim', held: HELD }) },
  { name: 'name-empty', title: '弹窗 · 输入框空着：什么都不说，不能按', render: bind({ kind: 'name', name: '', check: 'invalid' }) },
  ...reasonStates,
  { name: 'limit', title: '弹窗 · 名字到上限（数字来自服务器）', render: bind({ kind: 'limit', name: 'max', held: 10, earliest: inDays(11), eligible: HELD }) },
  { name: 'existing', title: '弹窗 · 这台电脑在账号里已有域名：只能确认', render: bind({ kind: 'existing', host: 'mac-7f3k-app.avibe.bot' }) },
  { name: 'binding', title: '弹窗 · 正在绑定', render: bind({ kind: 'name', name: 'max', check: 'free' }, { busy: true }) },
  { name: 'proof', title: '弹窗 · 要先确认是你本人：发送是单独一按', render: bind({ kind: 'proof' }) },
  { name: 'code', title: '弹窗 · 邮件验证码（登录同款）', render: bind({ kind: 'code', code: '' }), height: 1000 },
  { name: 'code-invalid', title: '弹窗 · 验证码不对：留在原地', render: bind({ kind: 'code', code: '', invalid: true }), height: 1000 },
  { name: 'factor-totp', title: '弹窗 · 两步验证', render: bind({ kind: 'factor', method: 'totp', methods: ['passkey', 'totp'], code: '' }), height: 1000 },
  { name: 'factor-setup', title: '弹窗 · 先添加验证器', render: bind({ kind: 'factorSetup', material: null, code: '' }), height: 900 },
  { name: 'code-wait', title: '弹窗 · 邮件验证码 429：原地等，按钮锁住直到截止', render: bind({ kind: 'code', code: '', limited: true, waitUntil: DESIGN_NOW + 60_000 }), height: 1000 },
  { name: 'factor-wait', title: '弹窗 · 两步验证 429：原地等 N 分钟', render: bind({ kind: 'factor', method: 'totp', methods: ['passkey', 'totp'], code: '', error: 'rateLimited', retryMinutes: 2, waitUntil: DESIGN_NOW + 120_000 }), height: 1000 },
  { name: 'factor-passkey-failed', title: '弹窗 · 通行密钥没完成：原地再试或改用验证码', render: bind({ kind: 'factor', method: 'passkey', methods: ['passkey', 'totp'], code: '', error: 'passkey' }), height: 1000 },
  { name: 'factor-setup-key', title: '弹窗 · 添加验证器：密钥和二维码（帧里是假数据）', render: bind({ kind: 'factorSetup', material: { uri: 'otpauth://totp/avibe.bot:fixture?secret=FIXTUREFIXTUREFI', secret: 'FIXT UREF IXTU REFI' }, code: '' }), height: 1000 },
  { name: 'factor-setup-wait', title: '弹窗 · 添加验证器 429：保留密钥和二维码，锁住直到截止', render: bind({ kind: 'factorSetup', material: { uri: 'otpauth://totp/avibe.bot:fixture?secret=FIXTUREFIXTUREFI', secret: 'FIXT UREF IXTU REFI' }, code: '', limited: true, retryMinutes: 1, waitUntil: DESIGN_NOW + 60_000 }), height: 1000 },
  { name: 'codes', title: '弹窗 · 恢复码：挡住离开，按「我已保存恢复码」才继续（帧里是遮住的假码）', render: bind({ kind: 'codes', codes: ['•••••-••001', '•••••-••002', '•••••-••003', '•••••-••004', '•••••-••005', '•••••-••006', '•••••-••007', '•••••-••008'] }), height: 900 },
  { name: 'saving', title: '弹窗 · 验证之后，正在完成', render: bind({ kind: 'saving' }) },
  { name: 'saving-recover', title: '弹窗 · 上一次验证没保存完：按「继续」接着做（不显示在卡上）', render: bind({ kind: 'saving', recover: true }) },
  { name: 'changed', title: '弹窗 · 验证之后内容变了：说哪几项，再按一次', render: bind({ kind: 'name', name: 'max', check: 'taken', changed: ['availability'] }) },
  { name: 'bound', title: '弹窗 · 已绑定', render: bind({ kind: 'bound', host: HOST }), both: true },
  { name: 'problem-unreachable', title: '弹窗 · 出错：连不上 avibe.bot，重新开始', render: bind({ kind: 'problem', problem: 'unreachable' }) },
  { name: 'problem-busy', title: '弹窗 · 旧版绑定还在进行：关闭，稍后再开', render: bind({ kind: 'problem', problem: 'busy' }) },
  { name: 'problem-unavailable', title: '弹窗 · 这台电脑不能绑定（墓碑）：死路，请联系我们', render: bind({ kind: 'problem', problem: 'unavailable' }) },
  { name: 'problem-storage', title: '弹窗 · 钥匙串写不进去（保留：要人做不同的事）', render: bind({ kind: 'problem', problem: 'storage' }) },
  { name: 'conflict', title: '弹窗 · 这台电脑属于另一个账号（owner_conflict）', render: bind({ kind: 'conflict' }) },

  { name: 'setup-complete', title: '设置完成 · 登录不再自动登记本机', render: column(<SetupCompleteView onLocal={noop} onSignIn={noop} />) },
];

const designFrames = frames(states.flatMap(({ name, title, render, height = 800, width = 1200, both }) => (['zh', 'en'] as const).map((lang): FrameSpec => ({
  name: `${name}-${lang}`,
  title: `${title} · ${lang}`,
  width,
  height,
  status: 'proposed',
  themes: both ? ['light', 'dark'] : ['light'],
  route: '/',
  fixtures: workbenchFixtures({}, { copy: copyFor(lang === 'en' ? en : zh) }),
  render,
}))));

export default designFrames;
