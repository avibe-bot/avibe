import { useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';

import { MessageRow } from '../../src/components/workbench/ChatPage';
import { SelectionQuoteToolbar } from '../../src/components/workbench/SelectionQuoteToolbar';
import type { WorkbenchMessage, WorkbenchSession } from '../../src/context/ApiContext';
import { InstanceAuthorizationContext } from '../../src/context/InstanceAuthorizationContext';
import { ToastContext } from '../../src/context/ToastContext';
import { DENIED_INSTANCE_CAPABILITIES } from '../../src/lib/sessionInfo';
import '../../src/i18n';
import '../../src/index.css';

const session = { id: 'interaction-fixture', metadata: {} } as WorkbenchSession;
const message = {
  id: 'answer', session_id: session.id, author: 'agent', source: 'agent', type: 'result',
  text: 'Use **bold 中文** now.\n\nSecond paragraph 🌱.', content: {}, metadata: {},
} as WorkbenchMessage;
const notice = {
  ...message, id: 'notice', type: 'notify', text: 'Model connection interrupted.',
  metadata: { event: 'backend_failure', failure_id: 'failure', turn_id: 'turn_0123456789abcdef0123456789abcdef' },
} as WorkbenchMessage;
const noop = () => {};

export function Fixture() {
  const containerRef = useRef<HTMLDivElement>(null);
  const failure = new URLSearchParams(location.search).has('failure');
  return (
    <div ref={containerRef} className="h-dvh overflow-y-auto bg-background px-4 py-32 text-foreground">
      <MessageRow message={failure ? notice : message} session={session} messageFontSize={14} />
      <SelectionQuoteToolbar containerRef={containerRef} onQuote={noop} />
    </div>
  );
}

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <ToastContext.Provider value={{ showToast: noop }}>
      <InstanceAuthorizationContext.Provider value={{
        remote: false, instanceKind: 'personal', instanceRole: 'owner',
        capabilities: { ...DENIED_INSTANCE_CAPABILITIES, can_manage_instance: true },
      }}>
        <Fixture />
      </InstanceAuthorizationContext.Provider>
    </ToastContext.Provider>
  </MemoryRouter>,
);
