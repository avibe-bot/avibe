import { DesktopSignIn } from '@/desktop/DesktopSignIn';
import type { DesktopClientState } from '@/desktop/bridge';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { SIGN_IN_PROVIDERS, type SignInMethods } from '@/sign-in/types';
import { frames, type FrameSpec } from '../../frame';
import { copyFor } from './instances-fixtures';

// Desktop sign-in on the shared flow (#3946), shipped (Max, 2026-11-18: as on the iPhone). Compare each with the shipped card it
// replaces: start/email ← desktop/instances/email, password ← password, code ← code,
// two-factor ← two-factor. The steps, order and words are the iPhone App's; the Desktop keeps its
// window column, "continue on this computer", and its in-app authenticator setup. Its first page is
// the iPhone's Welcome, smaller, without the brand row or the registration line, with the Desktop's
// own subtitle (Max, 2026-11-20). Frames show every way in, as both apps offer them once native supports
// each; the fixture runs no native call.

const EMAIL = 'max@example.test';
const ALL: SignInMethods = { providers: SIGN_IN_PROVIDERS, passkey: true };

const state = (account: DesktopClientState['account']): DesktopClientState => ({
  version: 1,
  revision: 1,
  generation: 1,
  account,
  connection: { status: 'local', committed: { kind: 'local', name: 'Studio', instance_id: null }, pending: null },
  enrollment: { status: 'unpaired', instance_id: null, error_code: null },
  catalog: { status: 'idle', instances: [] },
  local: { name: 'Studio', remote_access: 'off' },
  desktop: { version: '3.2.7', update: { phase: 'idle', latest: null, channel: 'stable', skippedVersion: null, interactive: false } },
  consent: null,
  remote_auxiliary: 'local',
});

const signedOut = state({ status: 'signed_out', identity: null, email: null, gate: null, methods: [] });
const codeSent = state({ status: 'verifying', identity: null, email: EMAIL, gate: null, methods: [] });
const twoFactor = state({ status: 'second_factor_required', identity: null, email: EMAIL, gate: 'challenge', methods: ['passkey', 'totp'] });

const noop = () => {};
const send = async () => null;

type Variant = { name: string; title: string; native: DesktopClientState; click?: FrameSpec['click'] };
const continueWithEmail = 'role=button[name="{t:mobileApp.welcome.continueWithEmail}"]';
const variants: Variant[] = [
  { name: 'start', title: '登录第一页 · iPhone 的 Welcome，Desktop 自己的副标题，无品牌行与登记说明', native: signedOut },
  { name: 'email', title: '邮箱 · 发送验证码', native: signedOut, click: [continueWithEmail] },
  {
    name: 'password',
    title: '邮箱 · 密码登录',
    native: signedOut,
    click: [continueWithEmail, 'role=button[name="{t:mobileApp.password.entry}"]'],
  },
  { name: 'code', title: '验证码 · 已发送', native: codeSent },
  { name: 'two-factor', title: '两步验证 · 通行密钥优先', native: twoFactor },
  {
    name: 'passkey',
    title: '通行密钥登录 · 无需邮箱',
    native: signedOut,
    click: ['role=button[name="{t:signIn.passkey.use}"]'],
  },
];

const designFrames = frames(variants.flatMap(({ name, title, native, click }) => (['zh', 'en'] as const).map((lang): FrameSpec => ({
  name: `${name}-${lang}`,
  title: `${title} · ${lang === 'zh' ? '中文' : 'English'}`,
  width: 1200,
  height: 800,
  themes: ['light', 'dark'],
  status: 'shipped',
  ...(click ? { click } : {}),
  fixtures: { copy: copyFor(lang === 'en' ? en : zh) },
  render: () => (
    <div className="flex h-full flex-col overflow-y-auto bg-background px-10 py-5 text-foreground">
      <DesktopSignIn
        state={native}
        send={send}
        methods={ALL}
        onLocal={noop}
        onDifferentAccount={noop}
        onOpenSecurity={noop}
        renderEnroll={() => null}
      />
    </div>
  ),
}))));

export default designFrames;
