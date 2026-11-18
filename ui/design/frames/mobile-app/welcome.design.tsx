import { frames } from '../../frame';
import { WelcomeIntro, WelcomePage } from '../../kit/mobile-app';
import { bilingual, loginStates, smallCodeFixture } from './fixtures';
import { WelcomeScreen } from '@/mobile/MobileOnboarding';

const noop = () => {};
const proposed = (specs: ReturnType<typeof bilingual>) => specs.map(frame => ({ ...frame, status: 'proposed' as const }));

const designFrames = frames([
  ...bilingual('intro', '欢迎 · 云间来信', () => <WelcomePage />),
  ...loginStates.flatMap(({ name, title, fixture }) => bilingual(name, title, () => <WelcomeIntro fixture={fixture} />)),
  // Google, GitHub and Apple on Welcome (owner, 2026-11-18): email stays the primary action; the
  // other accounts follow avibe.bot's 「或」 in its order and wording, then Apple. Two proposals.
  ...proposed(bilingual('providers', '欢迎 · 邮箱为主，Google / GitHub / Apple 为次要按钮（提案 A）', () => <WelcomeScreen locked={false} onContinue={noop} onProvider={noop} />)),
  ...proposed(bilingual('providers-small', '欢迎 · 提案 A · 375×667：首屏可滚动，邮箱按钮在首屏内', () => <WelcomeScreen locked={false} onContinue={noop} onProvider={noop} />, [375, 667])),
  ...proposed(bilingual('providers-row', '欢迎 · 邮箱为主，Google / GitHub / Apple 为一行图标（提案 B）', () => <WelcomeScreen locked={false} onContinue={noop} onProvider={noop} providerStyle="row" />)),
  ...bilingual('code-small', '验证码 · 长邮箱与小屏', () => <WelcomeIntro fixture={smallCodeFixture} />, [375, 667]),
]);

export default designFrames;
