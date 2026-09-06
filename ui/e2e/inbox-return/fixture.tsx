import { StrictMode, useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { HashRouter, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { InboxPage } from '../../src/components/workbench/InboxPage';
import { ApiProvider, type InboxSession } from '../../src/context/ApiContext';
import { WorkbenchInboxContext } from '../../src/context/WorkbenchInboxContext';
import { InstanceAuthorizationContext } from '../../src/context/InstanceAuthorizationContext';
import { ToastContext } from '../../src/context/ToastContext';
import { WindowManagerContext, type WindowManagerValue } from '../../src/context/WindowManagerContext';
import { OWNER_INSTANCE_CAPABILITIES } from '../../src/lib/sessionInfo';
import { APP_SHELL_SCROLL_ID } from '../../src/lib/mobileProjectsListMemory';
import '../../src/i18n';
import '../../src/index.css';

const noop = () => {};
const activateFeed = () => noop;
const makeRow = (id: number): InboxSession => ({
  session_id: `session-${id}`, title: `Conversation ${id}`, scope_id: null,
  project_id: 'fixture', project_name: 'Regression', last_activity_at: '2026-09-06T00:00:00Z',
  last_message_author: 'agent', replied: false,
  preview_text: `Completed review ${id}.\nThe conversation is ready to read.`,
  preview_at: null, unread_count: 1, unread: true,
});
const history = Array.from({ length: 90 }, (_, index) => makeRow(index + 1));
const params = new URLSearchParams(window.location.search);
const delay = Number(params.get('delay') ?? 0);

function Chat({ markRead, addActivity }: { markRead: (id: string) => Promise<void>; addActivity: () => void }) {
  const { sessionId } = useParams();
  const navigate = useNavigate();
  useEffect(() => {
    void markRead(sessionId!);
  }, [markRead, sessionId]);
  return <div className="flex h-full flex-col gap-4 p-4" data-testid="chat-detail">
    <button type="button" aria-label="Back" className="size-10" onClick={() => navigate(-1)}><ArrowLeft /></button>
    <div>{sessionId}</div>
    <button type="button" onClick={addActivity}>New activity</button>
    <button type="button" onClick={() => navigate('/inbox')}>New Inbox entry</button>
  </div>;
}

export function Fixture() {
  const location = useLocation();
  const chat = location.pathname.startsWith('/chat/');
  const [sessions, setSessions] = useState(history.slice(0, 60));
  const [unread, setUnread] = useState(Object.fromEntries(history.map((row) => [row.session_id, 1])));
  const markRead = useCallback(async (id: string) => {
    if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
    setUnread((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);
  const addActivity = () => {
    setSessions((prev) => prev.some((row) => row.session_id === 'session-100') ? prev : [makeRow(100), ...prev]);
    setUnread((prev) => ({ ...prev, 'session-100': 1 }));
  };
  return <WorkbenchInboxContext.Provider value={{
    inboxSessions: sessions, unreadBySession: unread, totalUnread: Object.keys(unread).length,
    unreadSessions: Object.keys(unread).length, nextCursor: sessions.length < 90 ? 'next' : null,
    loading: false, loadingMore: false, refresh: async () => {},
    loadMore: async () => { setSessions(history); }, markRead, activateFeed,
  }}>
    <div className="flex h-dvh flex-col overflow-hidden bg-background text-foreground md:block md:h-auto md:overflow-visible">
      {!chat && <header className="h-16 shrink-0 border-b border-border px-4 py-5 md:hidden">Inbox</header>}
      <main id={APP_SHELL_SCROLL_ID} className={chat
        ? 'min-h-0 flex-1 overflow-hidden md:overflow-visible'
        : 'min-h-0 flex-1 overflow-y-auto pb-[88px] md:overflow-visible md:pb-0'}>
        <div className={chat ? 'h-full' : 'px-4 py-5 md:px-10 md:py-8'}>
          <Routes>
            <Route path="/inbox" element={<InboxPage />} />
            <Route path="/chat/:sessionId" element={<Chat markRead={markRead} addActivity={addActivity} />} />
          </Routes>
        </div>
      </main>
    </div>
  </WorkbenchInboxContext.Provider>;
}

createRoot(document.getElementById('root')!).render(
  <StrictMode><HashRouter>
    <InstanceAuthorizationContext.Provider value={{
      remote: false, instanceKind: null, instanceRole: 'owner',
      capabilities: { ...OWNER_INSTANCE_CAPABILITIES, can_read_instance: false },
    }}>
      <ToastContext.Provider value={{ showToast: noop }}><ApiProvider>
        <WindowManagerContext.Provider value={{ focusedId: null, focusCanvas: noop } as WindowManagerValue}>
          <Fixture />
        </WindowManagerContext.Provider>
      </ApiProvider></ToastContext.Provider>
    </InstanceAuthorizationContext.Provider>
  </HashRouter></StrictMode>,
);
