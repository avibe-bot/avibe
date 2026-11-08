import type { ReactNode } from 'react';
import { frames } from '../../frame';
import { ForgotPasswordPage, PasswordPage, WeakPasswordNotice, WelcomeIntro, type MobilePasswordFixture } from '../../kit/mobile-app';
import { account, bilingual } from './fixtures';

// Password sign-in, the alternative first factor to the email code for an account
// that set a password on avibe.bot. After either factor, an account with two-step
// verification continues to `two-factor/*`; everyone else reaches the instance picker.
const longEmail = 'alexandra.chen+research-and-product-planning@international.example.com';
const filled: MobilePasswordFixture = { email: account.email, password: 'correct-horse-battery' };
const states: { name: string; title: string; render: () => ReactNode; size?: [number, number] }[] = [
  { name: 'email', title: '输入邮箱 · 验证码为主，密码为辅', render: () => <WelcomeIntro fixture={{ step: 'email', email: account.email, password: true }} /> },
  { name: 'email-invalid', title: '输入邮箱 · 格式错误（两种方式共用提示）', render: () => <WelcomeIntro fixture={{ step: 'email', email: 'max@', error: 'email-invalid', password: true }} /> },
  { name: 'default', title: '密码 · 未输入', render: () => <PasswordPage fixture={{ email: account.email }} /> },
  { name: 'filled', title: '密码 · 已输入可登录', render: () => <PasswordPage fixture={filled} /> },
  { name: 'shown', title: '密码 · 显示明文', render: () => <PasswordPage fixture={{ ...filled, shown: true }} /> },
  { name: 'signing-in', title: '密码 · 登录中', render: () => <PasswordPage fixture={{ ...filled, busy: true }} /> },
  { name: 'invalid', title: '邮箱或密码不正确 · 含“还没设置密码”', render: () => <PasswordPage fixture={{ ...filled, error: 'invalid-credentials' }} /> },
  { name: 'unconfirmed', title: '邮箱尚未确认 · 用验证码完成确认', render: () => <PasswordPage fixture={{ ...filled, error: 'email-not-confirmed' }} /> },
  { name: 'rate-limited', title: '尝试过多 · 等待后重试', render: () => <PasswordPage fixture={{ ...filled, error: 'rate-limited', wait: 45 }} /> },
  { name: 'network', title: '网络错误 · 保留输入', render: () => <PasswordPage fixture={{ ...filled, error: 'network' }} /> },
  { name: 'forgot', title: '忘记密码 · 验证码登录或去 avibe.bot 设置', render: () => <ForgotPasswordPage email={account.email} /> },
  { name: 'weak', title: '已登录 · 密码出现在数据泄露中（一次性提示）', render: () => <WeakPasswordNotice email={account.email} /> },
  {
    name: 'small',
    title: '密码错误 · 长邮箱与小屏',
    render: () => <PasswordPage fixture={{ ...filled, email: longEmail, error: 'invalid-credentials', compact: true }} />,
    size: [375, 667],
  },
  {
    // keyboardResizesViewport: UIKit shrinks the WebView to the space above the keyboard
    // (iPhone SE: 667 − 216 keyboard − 44 suggestion bar), so this is the whole viewport.
    name: 'keyboard',
    title: '小屏键盘弹起 · 键盘上方的可见区域',
    render: () => <PasswordPage fixture={{ ...filled, focused: true, compact: true }} />,
    size: [375, 407],
  },
];

const designFrames = frames(states.flatMap(({ name, title, render, size }) =>
  bilingual(name, title, render, size, 'proposed')));

export default designFrames;
