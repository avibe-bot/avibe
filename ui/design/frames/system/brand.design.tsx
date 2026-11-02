// The brand as shipped: the Avibe mark and the files the browser and the home screen use, the Vibey
// mascot, the Avibe · Agent OS lockup as the sidebar draws it, and the backend and platform marks.
import logo from '@/assets/logo.png';
import cloudMono from '@/assets/cloud-tuanzi-logo-mono.png';
import vibey from '@/assets/pet/vibey.png';
import { WorkbenchSidebar } from '@/components/workbench/WorkbenchSidebar';
import { BackendAvatar, BackendIcon } from '@/components/visual/BackendIcon';
import { PlatformIcon } from '@/components/visual/PlatformIcon';
import type { BackendId } from '@/lib/agentBackends';

// Served from public/ by URL (index.html, manifest.webmanifest, lib/faviconBadge.ts); imported here
// so the canvas bundles the same files.
import appIcon from '../../../public/icon-512.png';
import unreadFavicon from '../../../public/logo-unread.png';

import { frames } from '../../frame';
import { demoProjects } from '../../shell/fixtures';
import { SidebarColumn } from '../../shell/SidebarColumn';
import { systemFixtures } from './fixtures';
import { Section, Sheet, Specimen } from './sheet';

const sizes = [16, 32, 64, 128] as const;

const marksSheet = () => (
  <Sheet>
    <Section title="Avibe 标志" note="src/assets/logo.png：侧栏、引导页（画法见侧栏组合）；public/logo.png 是同一张图的网页图标">
      {sizes.map((size) => (
        <Specimen key={size} name={`${size}px`} className="w-32">
          <div className="flex h-32 items-end">
            <img src={logo} alt="" width={size} height={size} />
          </div>
        </Specimen>
      ))}
    </Section>
    <Section title="图标文件">
      <Specimen name="public/logo-unread.png" use="有未读时的网页图标" className="w-48">
        <img src={unreadFavicon} alt="" width={64} height={64} />
      </Specimen>
      <Specimen name="public/icon-512.png" use="PWA 与主屏图标（apple-touch-icon 同图）" className="w-56">
        <img src={appIcon} alt="" width={96} height={96} className="rounded-2xl" />
      </Specimen>
      <Specimen name="cloud-tuanzi-logo-mono.png" use="平台列表里的 Avibe" className="w-56">
        <img src={cloudMono} alt="" width={64} height={64} />
      </Specimen>
      <Specimen name="pet/vibey.png" use="歪比：主伙伴头像、桌面宠物" className="w-56">
        <img src={vibey} alt="" width={80} height={80} />
      </Specimen>
    </Section>
  </Sheet>
);

// The lockup is the sidebar's own header; the frame is cut to it.
const lockup = () => (
  <SidebarColumn>
    <WorkbenchSidebar />
  </SidebarColumn>
);

const backends: BackendId[] = ['vibey', 'claude', 'codex', 'opencode'];
const platforms = ['slack', 'discord', 'telegram', 'lark', 'wechat', 'avibe'] as const;

const thirdPartySheet = () => (
  <Sheet>
    <Section title="后端标记" note="BackendIcon 的三种画法与 BackendAvatar">
      {backends.map((backend) => (
        <Specimen key={backend} name={backend} className="w-56">
          <div className="flex items-center gap-3">
            <BackendIcon backend={backend} size={40} />
            <BackendIcon backend={backend} size={28} variant="glyph" />
            <BackendIcon backend={backend} size={28} variant="brand" />
            <BackendAvatar backend={backend} size={32} />
          </div>
        </Specimen>
      ))}
    </Section>
    <Section title="平台标记" note="PlatformIcon：裸标记与 tile">
      {platforms.map((platform) => (
        <Specimen key={platform} name={platform} className="w-36">
          <div className="flex items-center gap-3">
            <PlatformIcon platform={platform} size={28} />
            <PlatformIcon platform={platform} size={28} tile />
          </div>
        </Specimen>
      ))}
    </Section>
  </Sheet>
);

const designFrames = frames([
  { name: 'marks', title: '品牌 · 标志与图标文件', width: 1280, height: 520, status: 'shipped', fixtures: systemFixtures(), render: marksSheet },
  {
    name: 'lockup',
    title: '品牌 · Avibe · Agent OS 组合（侧栏）',
    width: 248,
    height: 64,
    status: 'shipped',
    fixtures: { ...systemFixtures(), projects: demoProjects },
    render: lockup,
  },
  { name: 'third-party', title: '品牌 · 后端与平台标记', width: 1280, height: 340, status: 'shipped', fixtures: systemFixtures(), render: thirdPartySheet },
]);

export default designFrames;
