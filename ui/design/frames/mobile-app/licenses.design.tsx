import { frames } from '../../frame';
import { AccountPage, LicensesPage } from '../../kit/mobile-app';
import { bilingual, licensesFixture, personal } from './fixtures';

/** Proposed: the App's open-source licenses page and its entry on Account. */
const proposed = (specs: ReturnType<typeof bilingual>) => specs.map(frame => ({ ...frame, status: 'proposed' as const }));

const designFrames = frames([
  ...proposed(bilingual('entry', '开源许可 · 账户里的入口', () => <AccountPage instance={personal} licenses />)),
  ...proposed(bilingual('default', '开源许可 · 首屏', () => <LicensesPage fixture={licensesFixture} />)),
  ...proposed(bilingual('scrolled', '开源许可 · 滚动后的内容（截取）', () => <LicensesPage fixture={licensesFixture} />, [390, 2600])),
]);

export default designFrames;
