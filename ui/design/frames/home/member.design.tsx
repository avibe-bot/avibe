// Home, for a member without Bots: unchanged — the shipped new-conversation home with
// its project picker. Bots are the Owner's, so a member's sidebar has no strip.
import { Workbench } from '@/components/Workbench';
import { DENIED_INSTANCE_CAPABILITIES, OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';

import { frames } from '../../frame';
import { DesktopScreen } from '../../shell/DesktopScreen';
import type { WorkbenchFixtures } from '../../shell/fixtures';
import { PhoneScreen } from '../../shell/PhoneScreen';
import { agentsApi, teamFixtures } from '../../shell/teamFixtures';

const member: WorkbenchFixtures = {
  ...teamFixtures,
  capabilities: { ...OWNER_INSTANCE_CAPABILITIES, is_instance_owner: false, can_manage_instance: DENIED_INSTANCE_CAPABILITIES.can_manage_instance },
  bots: [],
  unreadBySession: { ses_demo_cache: 3 },
  inboxSessions: [],
  // A browser, not the desktop app: no Computer Use, and no Cloud voice.
  api: { ...agentsApi, '/api/asr/status': { available: false }, '/api/desktop/computer-use/status': { supported: false } },
};

const designFrames = frames([
  {
    name: 'desktop',
    title: '首页 · 没有伙伴的成员：保持现在的首页',
    width: 1440,
    height: 900,
    status: 'shipped',
    fixtures: member,
    route: '/',
    render: () => <DesktopScreen />,
  },
  {
    name: 'phone',
    title: '首页 · 手机 · 没有伙伴的成员',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures: member,
    route: '/',
    render: () => (
      <PhoneScreen flat>
        <Workbench />
      </PhoneScreen>
    ),
  },
]);

export default designFrames;
