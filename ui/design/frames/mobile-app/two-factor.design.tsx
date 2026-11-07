import { frames } from '../../frame';
import { SecondFactorPage, type MobileSecondFactorFixture } from '../../kit/mobile-app';
import { account, bilingual } from './fixtures';

// Second step of mobile sign-in for an account with two-step verification
// (avibe.bot owns it: passkeys, an authenticator app, recovery codes).
const both: MobileSecondFactorFixture = { email: account.email, methods: ['passkey', 'totp'], step: 'passkey' };
const totp: MobileSecondFactorFixture = { ...both, step: 'totp' };
const recovery: MobileSecondFactorFixture = { ...both, step: 'recovery' };
const states: { name: string; title: string; fixture: MobileSecondFactorFixture; size?: [number, number] }[] = [
  { name: 'passkey', title: '两步验证 · 通行密钥优先', fixture: both },
  { name: 'passkey-waiting', title: '两步验证 · 等待系统通行密钥面板', fixture: { ...both, busy: 'passkey' } },
  { name: 'passkey-failed', title: '两步验证 · 通行密钥没完成', fixture: { ...both, error: 'passkey-failed' } },
  { name: 'totp', title: '两步验证 · 身份验证器验证码', fixture: totp },
  { name: 'totp-partial', title: '验证器验证码 · 输入中', fixture: { ...totp, code: '28' } },
  { name: 'totp-verifying', title: '验证器验证码 · 验证中', fixture: { ...totp, code: '287082', busy: 'verifying' } },
  { name: 'totp-invalid', title: '验证器验证码 · 不正确', fixture: { ...totp, code: '287083', error: 'code-invalid' } },
  { name: 'totp-rate-limited', title: '验证器验证码 · 尝试过多', fixture: { ...totp, code: '287082', error: 'rate-limited' } },
  { name: 'totp-network', title: '验证器验证码 · 网络错误保留输入', fixture: { ...totp, code: '287082', error: 'network' } },
  { name: 'totp-only', title: '只有身份验证器 · 没有通行密钥入口', fixture: { ...totp, methods: ['totp'] } },
  { name: 'recovery', title: '使用恢复码', fixture: { ...recovery, code: 'k7m2p-9xq4r' } },
  { name: 'recovery-invalid', title: '恢复码不正确或已用过', fixture: { ...recovery, code: 'k7m2p-9xq4r', error: 'code-invalid' } },
  { name: 'setup-required', title: '必须先开启 · 去 avibe.bot 设置', fixture: { ...both, methods: [], step: 'setup' } },
  {
    name: 'totp-small',
    title: '验证器验证码 · 长邮箱与小屏',
    fixture: { ...totp, email: 'alexandra.chen+research-and-product-planning@international.example.com', code: '28', compact: true },
    size: [375, 667],
  },
];

const designFrames = frames(states.flatMap(({ name, title, fixture, size }) =>
  bilingual(name, title, () => <SecondFactorPage fixture={fixture} />, size).map(frame => ({ ...frame, status: 'proposed' as const }))));

export default designFrames;
