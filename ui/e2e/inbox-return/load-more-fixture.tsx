import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { createHashRouter, Route, RouterProvider, Routes } from 'react-router-dom';
import { InboxPage } from '../../src/components/workbench/InboxPage';
import { ApiProvider } from '../../src/context/ApiContext';
import { WorkbenchInboxProvider } from '../../src/context/WorkbenchInboxProvider';
import { InstanceAuthorizationContext } from '../../src/context/InstanceAuthorizationContext';
import { ToastContext } from '../../src/context/ToastContext';
import { WindowManagerContext, type WindowManagerValue } from '../../src/context/WindowManagerContext';
import { OWNER_INSTANCE_CAPABILITIES } from '../../src/lib/sessionInfo';
import { APP_SHELL_SCROLL_ID } from '../../src/lib/mobileProjectsListMemory';
import '../../src/i18n';
import '../../src/index.css';

// The production Inbox provider and page, over whatever `GET /api/inbox` the spec
// routes: the feed, its cursor and the unread map all come from that one read, so
// the provider pages exactly as it does against a server.
const noop = () => {};

export function Fixture() {
  return (
    <WorkbenchInboxProvider>
      <main id={APP_SHELL_SCROLL_ID} className="min-h-dvh bg-background px-4 py-5 text-foreground md:px-10 md:py-8">
        <Routes>
          <Route path="/inbox" element={<InboxPage onOpenSearch={noop} />} />
        </Routes>
      </main>
    </WorkbenchInboxProvider>
  );
}

const router = createHashRouter([{ path: '*', element: <Fixture /> }]);

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <InstanceAuthorizationContext.Provider value={{
      remote: false, instanceKind: null, instanceRole: 'owner',
      capabilities: { ...OWNER_INSTANCE_CAPABILITIES, can_read_instance: false },
    }}>
      <ToastContext.Provider value={{ showToast: noop }}><ApiProvider>
        <WindowManagerContext.Provider value={{ focusedId: null, focusCanvas: noop } as WindowManagerValue}>
          <RouterProvider router={router} />
        </WindowManagerContext.Provider>
      </ApiProvider></ToastContext.Provider>
    </InstanceAuthorizationContext.Provider>
  </StrictMode>,
);
