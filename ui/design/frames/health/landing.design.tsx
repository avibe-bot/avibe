import { HealthLandingView } from '@/components/health/HealthRequest';
import { frames } from '../../frame';
import { settingsConfig } from '../settings/fixtures';
import { consolePage, phoneShell, workbenchFixtures } from '../workbench/fixtures';

const route = '/mobile/health/mreq_0123456789abcdef0123456789abcdef';
const path = '/mobile/health/:requestId';
const fixtures = workbenchFixtures({ '/api/config': { ...settingsConfig, language: null } });
const designFrames = frames([
  { name: 'phone', title: 'Health notification · authorize', width: 402, height: 874, status: 'shipped', route, fixtures, render: phoneShell(path, <HealthLandingView state="authorize" />) },
  { name: 'phone-connected', title: 'Health notification · connection established', width: 402, height: 874, status: 'shipped', route, fixtures, render: phoneShell(path, <HealthLandingView state="connected" />) },
  { name: 'phone-expired', title: 'Health notification · request expired', width: 402, height: 874, status: 'shipped', route, fixtures, render: phoneShell(path, <HealthLandingView state="expired" />) },
  { name: 'desktop', title: 'Health notification · continue on iPhone', width: 808, height: 560, status: 'shipped', route, fixtures, render: consolePage(<HealthLandingView state="waiting" />) },
]);

export default designFrames;
