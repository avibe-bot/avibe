import type { ReactNode } from 'react';
import { EmailEntryScreen, ForgotPasswordScreen, WeakPasswordNoticeScreen } from '@/mobile/MobileOnboarding';
import { frames, type FrameSpec } from '../../frame';
import { account, bilingual, longEmail, noop } from './fixtures';

// Password sign-in, the alternative first factor to the email code for an account
// that set a password on avibe.bot. "Sign in with password" turns the email step's
// form into email + password at once (the owner's change of 2026-11-09 01:51,
// replacing identifier-first). After either factor, an account with two-step
// verification continues to `two-factor/*`; everyone else reaches the instance picker.
type EmailProps = Partial<Parameters<typeof EmailEntryScreen>[0]>;
const emailStep = (props: EmailProps = {}) => () => (
  <EmailEntryScreen
    mode="code"
    email={account.email}
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

const filledPassword = 'correct-horse-battery';
const passwordStep = (props: EmailProps = {}) => emailStep({ mode: 'password', password: filledPassword, ...props });

const states: { name: string; title: string; render: () => ReactNode; size?: [number, number]; click?: FrameSpec['click'] }[] = [
  { name: 'email', title: '输入邮箱 · 验证码为主，密码为辅', render: emailStep() },
  { name: 'email-invalid', title: '发送验证码 · 邮箱格式错误', render: emailStep({ email: 'max@', errorKey: 'mobileApp.email.invalid' }) },
  { name: 'switched-empty', title: '切换到密码登录 · 没填邮箱也能切换', render: passwordStep({ email: '', password: '' }) },
  { name: 'default', title: '切换到密码登录 · 已填的邮箱带过来', render: passwordStep({ password: '' }) },
  { name: 'filled', title: '邮箱 + 密码 · 可登录', render: passwordStep() },
  { name: 'shown', title: '密码 · 显示明文', render: passwordStep({ shown: true }) },
  { name: 'signing-in', title: '登录中', render: passwordStep({ busy: 'signIn', locked: true }) },
  { name: 'password-email-invalid', title: '点登录 · 邮箱格式错误', render: passwordStep({ email: 'max@', errorKey: 'mobileApp.email.invalid' }) },
  { name: 'invalid', title: '邮箱或密码不正确 · 含“还没设置密码”', render: passwordStep({ errorKey: 'mobileApp.password.invalid' }) },
  { name: 'unconfirmed', title: '邮箱尚未确认 · 用验证码完成确认', render: passwordStep({ errorKey: 'mobileApp.password.unconfirmed' }) },
  { name: 'rate-limited', title: '尝试过多 · 等待后重试', render: passwordStep({ errorKey: 'mobileApp.password.rateLimited', retryIn: 45 }) },
  { name: 'network', title: '网络错误 · 保留输入', render: passwordStep({ errorKey: 'mobileApp.errors.network' }) },
  {
    name: 'forgot',
    title: '忘记密码 · 验证码登录或去 avibe.bot 设置',
    render: () => (
      <ForgotPasswordScreen
        email={account.email}
        busy={false}
        locked={false}
        cooldown={0}
        errorKey={null}
        onBack={noop}
        onSendCode={noop}
        onOpenSecurity={noop}
      />
    ),
  },
  {
    name: 'weak',
    title: '已登录 · 密码出现在数据泄露中（一次性提示）',
    render: () => (
      <WeakPasswordNoticeScreen
        email={account.email}
        reasons={['pwned']}
        locked={false}
        onContinue={noop}
        onOpenSecurity={noop}
      />
    ),
  },
  {
    name: 'small',
    title: '密码错误 · 长邮箱与小屏',
    render: passwordStep({ email: longEmail, errorKey: 'mobileApp.password.invalid' }),
    size: [375, 667],
  },
  {
    // keyboardResizesViewport: UIKit shrinks the WebView to the space above the keyboard
    // (iPhone SE: 667 − 216 keyboard − 44 suggestion bar); focusing the password field reveals its form.
    name: 'keyboard',
    title: '小屏键盘弹起 · 键盘上方的可见区域',
    render: passwordStep(),
    size: [375, 407],
    click: ['#mobile-login-password'],
  },
];

const designFrames = frames(states.flatMap(({ name, title, render, size, click }) =>
  bilingual(name, title, render, { size, click })));

export default designFrames;
