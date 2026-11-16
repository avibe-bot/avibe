import { frames } from '../../frame';
import { WelcomeIntro, WelcomePage } from '../../kit/mobile-app';
import { bilingual, loginStates, smallCodeFixture } from './fixtures';

const designFrames = frames([
  ...bilingual('intro', '欢迎 · 云间来信', () => <WelcomePage />),
  ...loginStates.flatMap(({ name, title, fixture }) => bilingual(name, title, () => <WelcomeIntro fixture={fixture} />)),
  ...bilingual('code-small', '验证码 · 长邮箱与小屏', () => <WelcomeIntro fixture={smallCodeFixture} />, [375, 667]),
]);

export default designFrames;
