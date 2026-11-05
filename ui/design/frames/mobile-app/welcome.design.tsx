import { frames } from '../../frame';
import { WelcomeIntro } from '../../kit/mobile-app';
import { bilingual } from './fixtures';

const designFrames = frames([
  ...bilingual('default', '欢迎 · 登录或注册', () => <WelcomeIntro />),
  ...bilingual('signing-in', '等待系统登录返回 · 按钮禁用', () => <WelcomeIntro state="signing-in" />),
  ...bilingual('cancelled', '用户取消 · 中性反馈', () => <WelcomeIntro state="cancelled" />),
  ...bilingual('network-error', '登录 · 网络错误', () => <WelcomeIntro state="network-error" />),
  ...bilingual('denied', '登录 · 未获授权', () => <WelcomeIntro state="denied" />),
]);

export default designFrames;
