import { frames } from '../../frame';
import { AccountPage, LicensesPage } from '../../kit/mobile-app';
import { bilingual, licensesFixture, personal } from './fixtures';

/** Proposed: the App's open-source licenses page and its entry on Account. */
const proposed = (specs: ReturnType<typeof bilingual>) => specs.map(frame => ({ ...frame, status: 'proposed' as const }));
const expanded = { ...licensesFixture, groups: licensesFixture.groups.map((group, index) => ({ ...group, open: index === 0 })) };

const designFrames = frames([
  ...proposed(bilingual('entry', '开源许可 · 账户里的入口', () => <AccountPage instance={personal} licenses />)),
  ...proposed(bilingual('default', '开源许可 · 首屏', () => <LicensesPage fixture={licensesFixture} />)),
  ...proposed(bilingual('group-open', '开源许可 · 展开一组许可文本（整页）', () => <LicensesPage fixture={expanded} />, [390, 2400])),
]);

export default designFrames;
