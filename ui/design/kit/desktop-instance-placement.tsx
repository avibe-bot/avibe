import { Pin } from 'lucide-react';
import { Workbench } from '@/components/Workbench';
import { AgentsPage } from '@/components/workbench/AgentsPage';
import { ChatHeaderBar } from '@/components/workbench/ChatPage';
import { PlacementShell, type DesktopInstanceFixture, type InstancePlacement, type PlacementStatus } from './desktop-instances';
import { chatSession, withChatProviders } from '../frames/chat/fixtures';
import { demoAgents } from '../frames/agents/fixtures';
import showPreview from '../assets/show-page-funnel.svg';

const noop = () => {};
export type PlacementPage = 'home' | 'chat' | 'agents';
export type PlacementFixture = DesktopInstanceFixture & { placement: InstancePlacement; page: PlacementPage; status: PlacementStatus; compact?: boolean; menu?: boolean };
function Conversation({ lang }: { lang: 'zh' | 'en' }) {
 return withChatProviders(<div className="flex h-full flex-col">
   <ChatHeaderBar session={chatSession({title: lang === 'zh' ? 'Q4 漏斗看板' : 'Q4 funnel dashboard'})} agents={demoAgents} defaultAgentName="vibey" onPatch={noop} onBack={noop} showPageMode showPageBusy={false} onToggleShowPage={noop} onPrepareShowPageLaunch={async () => true} annotation={{state:{available:true, enabled:false, mode:'smart'},setIframe:noop,handleIframeLoad:noop,handleShortcutKeyDown:noop,enable:noop,disable:noop,setMode:noop}} readOnlyReason={null} canManageInstance canPinToDock sessionActions={[{id:'pin',group:'organize',icon:Pin,label:lang === 'zh' ? '置顶' : 'Pin',onSelect:noop}]} />
   <div className="min-h-0 flex-1 overflow-auto"><img src={showPreview} className="h-auto w-full" alt={lang === 'zh' ? '可视化页面静态示例' : 'Static Show Page example'} /></div>
 </div>);
}
export function PlacementComparison({ fixture }: { fixture: PlacementFixture }) {
 const page=fixture.page === 'home' ? <Workbench /> : fixture.page === 'agents' ? <AgentsPage /> : <Conversation lang={fixture.lang} />;
 return <PlacementShell fixture={fixture} placement={fixture.placement} status={fixture.status} compact={fixture.compact} menu={fixture.menu} updatePanel={fixture.status === 'update' && !fixture.menu} remote={!['signed-out','switching'].includes(fixture.status)} page={page} flush={fixture.page === 'chat'} />;
}
