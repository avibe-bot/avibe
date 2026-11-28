import { CodeEntryScreen, EmailEntryScreen, WelcomeScreen } from '@/mobile/MobileOnboarding';
import { frames } from '../../frame';
import { account, bilingual, longEmail, noop } from './fixtures';

const allProviders = ['google', 'github', 'apple'] as const;

type EmailProps = Partial<Parameters<typeof EmailEntryScreen>[0]>;
const emailStep = (props: EmailProps = {}) => () => (
  <EmailEntryScreen
    mode="code"
    email=""
    password=""
    shown={false}
    busy={null}
    locked={false}
    cooldown={0}
    errorKey={null}
    retryIn={0}
    onBack={noop}
    onEmailChange={noop}
    onPasswordChange={noop}
    onToggleShown={noop}
    onSubmit={noop}
    onSwitchMode={noop}
    onForgot={noop}
    onUseCode={noop}
    {...props}
  />
);

type CodeProps = Partial<Parameters<typeof CodeEntryScreen>[0]>;
const codeStep = (props: CodeProps = {}) => () => (
  <CodeEntryScreen
    email={account.email}
    code=""
    busy={null}
    locked={false}
    errorKey={null}
    cooldown={0}
    providerCooldownActive={false}
    resent={false}
    onBack={noop}
    onCodeChange={noop}
    onVerify={noop}
    onResend={noop}
    {...props}
  />
);

const designFrames = frames([
  ...bilingual('intro', '欢迎 · 云间来信', () => <WelcomeScreen locked={false} onContinue={noop} />),
  ...bilingual('default', '登录 / 注册 · 输入邮箱', emailStep()),
  ...bilingual('email-invalid', '邮箱格式错误', emailStep({ email: 'max@', errorKey: 'mobileApp.email.invalid' })),
  ...bilingual('sending', '发送验证码中', emailStep({ email: account.email, busy: 'send', locked: true })),
  ...bilingual('email-rate-limited', '发送 · 请求过于频繁', emailStep({ email: account.email, cooldown: 15, errorKey: 'mobileApp.code.rateLimited' })),
  ...bilingual('email-network-error', '发送 · 网络错误', emailStep({ email: account.email, errorKey: 'mobileApp.errors.network' })),
  ...bilingual('code-sent', '验证码已发送 · 可修改邮箱', codeStep()),
  ...bilingual('code-partial', '验证码 · 输入中', codeStep({ code: '12' })),
  ...bilingual('code-ready', '已输入六位 · 可验证', codeStep({ code: '123456' })),
  ...bilingual('resending', '重新发送中', codeStep({ busy: 'resend', locked: true })),
  ...bilingual('resend-success', '重发成功 · 60 秒倒计时', codeStep({ cooldown: 60, resent: true })),
  ...bilingual('resend-failed', '重发失败 · 15 秒倒计时', codeStep({ cooldown: 15, errorKey: 'mobileApp.errors.network' })),
  ...bilingual('verifying', '验证中', codeStep({ code: '123456', busy: 'verify', locked: true })),
  ...bilingual('code-invalid', '验证码错误 · 沿用合并提示', codeStep({ code: '123455', errorKey: 'mobileApp.code.invalid' })),
  ...bilingual('code-expired', '验证码过期 · 沿用合并提示', codeStep({ code: '123456', errorKey: 'mobileApp.code.invalid' })),
  ...bilingual('rate-limited', '重发 · 请求过于频繁', codeStep({ cooldown: 15, providerCooldownActive: true, errorKey: 'mobileApp.code.rateLimited' })),
  ...bilingual('network-error', '验证 · 网络错误', codeStep({ errorKey: 'mobileApp.errors.network' })),
  // Google, GitHub and Apple on Welcome (owner, 2026-11-18: layout B). Email stays the primary
  // action; the other accounts follow avibe.bot's 「或」 as one row of marks.
  ...bilingual('providers-row', '欢迎 · 邮箱为主，Google / GitHub / Apple 为一行图标', () => <WelcomeScreen locked={false} providers={allProviders} onContinue={noop} onProvider={noop} />),
  ...bilingual('providers-row-error', '欢迎 · Google 邮箱未验证时的提示', () => <WelcomeScreen locked={false} providers={allProviders} errorKey="mobileApp.provider.googleEmailUnverified" onContinue={noop} onProvider={noop} />),
  ...bilingual('code-small', '验证码 · 长邮箱与小屏', codeStep({ email: longEmail, cooldown: 15, errorKey: 'mobileApp.errors.network' }), [375, 667]),
]);

export default designFrames;
