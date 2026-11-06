// The chat header (ChatHeaderBar): the click-to-rename title with the session menu, and the Show Page
// controls. In Show Page mode the page itself is the Show runtime's, so these frames draw the header alone,
// with the annotation control as the overlay reports it (off, Smart, screenshot).
import { Pin } from 'lucide-react';

import { ChatHeaderBar } from '@/components/workbench/ChatPage';
import type { SessionActionDescriptor } from '@/components/workbench/sessionActions';
import type { AnnotationState } from '@/components/workbench/useShowPageAnnotation';

import { frames } from '../../frame';
import { demoAgents } from '../agents/fixtures';
import { chatFixtures, chatSession, withChatProviders } from './fixtures';

const noop = () => {};

// The session menu's trigger shows when the session has actions; the menu itself opens on a click.
const sessionActions: SessionActionDescriptor[] = [
  { id: 'pin', group: 'organize', icon: Pin, label: '置顶', onSelect: noop },
];

const header = (options: { title?: string; showPageMode?: boolean; annotation?: AnnotationState | null }) => () =>
  withChatProviders(
    <ChatHeaderBar
      session={chatSession({ title: options.title ?? 'Q3 发布看板' })}
      agents={demoAgents}
      defaultAgentName="vibey"
      onPatch={noop}
      onBack={noop}
      showPageMode={options.showPageMode ?? false}
      showPageBusy={false}
      onToggleShowPage={noop}
      onPrepareShowPageLaunch={() => Promise.resolve(true)}
      annotation={{
        state: options.annotation ?? null,
        setIframe: noop,
        handleIframeLoad: noop,
        handleShortcutKeyDown: noop,
        enable: noop,
        disable: noop,
        setMode: noop,
      }}
      readOnlyReason={null}
      canManageInstance
      canPinToDock
      sessionActions={sessionActions}
    />,
  );

const off: AnnotationState = { available: true, enabled: false, mode: 'smart' };
const smart: AnnotationState = { available: true, enabled: true, mode: 'smart' };
const screenshot: AnnotationState = { available: true, enabled: true, mode: 'screenshot' };

const designFrames = frames([
  {
    name: 'mobile-runtime',
    title: '会话头部 · 手机 · 与首页同高',
    width: 390,
    height: 56,
    status: 'shipped',
    fixtures: chatFixtures(),
    render: header({ title: '移动端交互设计' }),
  },
  {
    name: 'title-hover',
    title: '会话头部 · 标题悬停（点击改名）',
    width: 1192,
    height: 56,
    status: 'shipped',
    fixtures: chatFixtures(),
    hover: '[data-chat-header] button:has(> span.truncate)',
    render: header({ title: '移动端交互设计' }),
  },
  {
    name: 'title-editing',
    title: '会话头部 · 点击标题改名',
    width: 1192,
    height: 56,
    status: 'shipped',
    fixtures: chatFixtures(),
    click: ['role=button[name="移动端交互设计"]'],
    render: header({ title: '移动端交互设计' }),
  },
  {
    name: 'show-page',
    title: '会话头部 · Show Page 已打开，标注关闭',
    width: 1192,
    height: 56,
    status: 'shipped',
    fixtures: chatFixtures(),
    render: header({ showPageMode: true, annotation: off }),
  },
  {
    name: 'annotate-smart',
    title: '会话头部 · 标注模式 · Smart',
    width: 1192,
    height: 56,
    status: 'shipped',
    fixtures: chatFixtures(),
    render: header({ showPageMode: true, annotation: smart }),
  },
  {
    name: 'annotate-screenshot',
    title: '会话头部 · 标注模式 · 截图',
    width: 1192,
    height: 56,
    status: 'shipped',
    fixtures: chatFixtures(),
    render: header({ showPageMode: true, annotation: screenshot }),
  },
  {
    name: 'phone-show-page',
    title: '会话头部 · Show Page 已打开 · 手机',
    width: 390,
    height: 56,
    status: 'shipped',
    fixtures: chatFixtures(),
    render: header({ showPageMode: true, annotation: off }),
  },
  {
    name: 'phone-annotate-menu',
    title: '会话头部 · 标注模式选择 · 手机',
    width: 390,
    height: 300,
    status: 'shipped',
    fixtures: chatFixtures(),
    click: ['role=button[name="{t:chat.showPage.annotate.toggle}"]'],
    render: header({ showPageMode: true, annotation: smart }),
  },
  {
    name: 'phone-annotating',
    title: '会话头部 · 标注中 · 手机',
    width: 390,
    height: 56,
    status: 'shipped',
    fixtures: chatFixtures(),
    render: header({ showPageMode: true, annotation: smart }),
  },
]);

export default designFrames;
