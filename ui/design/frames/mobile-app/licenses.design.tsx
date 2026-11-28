import { AccountPage, LicensesPage } from '@/mobile/MobileOnboarding';
import { frames } from '../../frame';
import { bilingual, licensesFixture, noop, personal, signedInSession } from './fixtures';

/**
 * The App's open-source licenses page and its entry on Account; approved by 产品伙伴 on 2026-11-11,
 * shipped in `ui/src/mobile/MobileOnboarding.tsx` and `ui/src/mobile/LicensesPage.tsx`.
 * The fixture is illustrative; the App reads generated data.
 */
const designFrames = frames([
  ...bilingual('entry', '开源许可 · 账户里的入口', () => (
    <AccountPage
      session={signedInSession([personal])}
      busy={false}
      locked={false}
      errorKey={null}
      licenses
      showCurrent
      onBack={noop}
      onSwitch={noop}
      onLogout={noop}
      onLicenses={noop}
      onAccountData={noop}
    />
  )),
  ...bilingual('default', '开源许可 · 首屏', () => <LicensesPage licenses={licensesFixture} onBack={noop} onOpenLink={noop} />),
  ...bilingual('group-open', '开源许可 · 展开一组许可文本（整页）', () => <LicensesPage licenses={licensesFixture} openGroup={0} onBack={noop} onOpenLink={noop} />, [390, 2400]),
]);

export default designFrames;
