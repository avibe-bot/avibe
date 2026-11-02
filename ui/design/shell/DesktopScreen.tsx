import type { ReactNode } from 'react';
import { Route, Routes } from 'react-router-dom';

import { Workbench } from '@/components/Workbench';
import { ChatPage } from '@/components/workbench/ChatPage';
import { WorkbenchSidebar } from '@/components/workbench/WorkbenchSidebar';

import { SidebarColumn } from './SidebarColumn';

/** The desktop Workbench as AppShell lays it out at md and up: the sidebar column, and the routed
 *  page beside it (`/` the home, `/chat/:sessionId` a conversation), padded as AppShell pads it. */
export function DesktopScreen({ overlay }: { overlay?: ReactNode }) {
  return (
    <div className="relative flex h-full bg-background text-foreground">
      <div className="h-full w-(--app-sidebar-w) shrink-0">
        <SidebarColumn>
          <WorkbenchSidebar />
        </SidebarColumn>
      </div>
      <main className="page-glow-console relative h-full min-w-0 flex-1 overflow-hidden">
        <div className="mx-auto h-full px-10 py-8">
          <Routes>
            <Route path="/" element={<Workbench />} />
            <Route path="/chat/:sessionId" element={<ChatPage />} />
          </Routes>
        </div>
        {overlay}
      </main>
    </div>
  );
}
