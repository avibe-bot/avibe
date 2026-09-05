import { useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';

import { Transcript } from '../../src/components/workbench/ChatPage';
import { ApiProvider, type WorkbenchMessage, type WorkbenchSession } from '../../src/context/ApiContext';
import { ToastContext } from '../../src/context/ToastContext';
import { WindowManagerContext, type WindowManagerValue } from '../../src/context/WindowManagerContext';
import '../../src/i18n';
import '../../src/index.css';

const noop = () => {};
const session = { id: 'paging-fixture', title: 'History', metadata: {} } as WorkbenchSession;
const history: WorkbenchMessage[] = Array.from({ length: 600 }, (_, index) => ({
  id: `message-${index + 1}`,
  scope_id: null,
  session_id: session.id,
  platform: 'avibe',
  author: index % 2 ? 'agent' : 'user',
  type: index % 2 ? 'result' : 'user',
  source: index % 2 ? 'agent' : 'user',
  author_id: null,
  author_name: null,
  native_message_id: null,
  parent_native_message_id: null,
  text: `Message ${index + 1}\nFirst line of history.\nSecond line of history.`,
  content: {},
  metadata: {},
  created_at: new Date(Date.UTC(2026, 8, 1, 0, 0, index)).toISOString(),
  updated_at: new Date(Date.UTC(2026, 8, 1, 0, 0, index)).toISOString(),
  delivered_at: null,
  read_at: null,
}));
const params = new URLSearchParams(window.location.search);
const initialCount = Number(params.get('count') ?? 50);
const delay = Number(params.get('delay') ?? 100);

export function Fixture() {
  const [messages, setMessages] = useState(history.slice(-initialCount));
  const [before, setBefore] = useState(history.length - initialCount);
  const [loading, setLoading] = useState(false);
  const [loads, setLoads] = useState(0);
  const [needsLatestReload, setNeedsLatestReload] = useState(false);
  const followingTailRef = useRef(true);

  const loadOlder = async () => {
    setLoading(true);
    setLoads((count) => count + 1);
    await new Promise((resolve) => setTimeout(resolve, delay));
    if (params.has('fail') && loads === 0) {
      setLoading(false);
      return false;
    }
    const start = Math.max(0, before - 50);
    if (!params.has('empty') || loads !== 0) {
      setMessages((current) => [...history.slice(start, before), ...current].slice(0, 300));
      if (messages.length + before - start > 300) setNeedsLatestReload(true);
    }
    setBefore(start);
    setLoading(false);
    return true;
  };

  return (
    <div className="flex h-dvh flex-col bg-background text-foreground" data-testid="chat-paging-fixture" data-loads={loads}>
      <Transcript
        messages={messages}
        session={session}
        agentDisplayName="Agent"
        working={false}
        hasOlder={before > 0}
        loadingOlder={loading}
        onLoadOlder={loadOlder}
        needsLatestReload={needsLatestReload}
        onReloadLatest={async () => {
          setMessages(history.slice(-50));
          setBefore(history.length - 50);
          setNeedsLatestReload(false);
          return true;
        }}
        jumpTarget={null}
        onJumpHandled={noop}
        highlightedId={null}
        messageFontSize={14}
        onQuickReply={noop}
        provisionRequestsByMessage={new Map()}
        onVaultRequestResolved={noop}
        onQuoteSelection={noop}
        onAskInNewSession={noop}
        readOnly
        followingTailRef={followingTailRef}
      />
    </div>
  );
}

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <ToastContext.Provider value={{ showToast: noop }}>
      <ApiProvider>
        <WindowManagerContext.Provider value={{ focusedId: null, focusCanvas: noop } as WindowManagerValue}>
          <Fixture />
        </WindowManagerContext.Provider>
      </ApiProvider>
    </ToastContext.Provider>
  </MemoryRouter>,
);
