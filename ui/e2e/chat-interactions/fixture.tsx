import { useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';

import { MessageRow } from '../../src/components/workbench/ChatPage';
import { HARNESS_GROUP_RAIL, HarnessGroup } from '../../src/components/workbench/HarnessGroup';
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
const harness = (id: string, text: string) => ({
  ...message, id, author: 'harness', source: 'harness', type: 'harness', author_name: 'agent_run', text,
  source_session_id: 'ses_source', source_session_title: 'Vault sandbox Taproot signing and boundary audit',
} as WorkbenchMessage);
// Long one-line previews, as Agent callbacks usually are: the chip must truncate
// inside its row rather than size itself to the text.
const callbacks = [
  harness('callback-1', 'Implementation incorporates shared packaged JSON schema validation for every signing path.'),
  harness('callback-2', 'Progress: both source boundaries implemented; 27 focused tests pass and the browser suite is next.'),
];
const noop = () => {};

// A lone Harness row, then a group both collapsed and expanded, its members on
// the rail exactly as the Transcript draws them.
function HarnessRows() {
  return (
    <div className="flex flex-col gap-3">
      <MessageRow message={callbacks[0]} session={session} messageFontSize={14} />
      <HarnessGroup groupKey="collapsed" members={callbacks} expanded={false} onToggle={noop} />
      <HarnessGroup groupKey="expanded" members={callbacks} expanded onToggle={noop} />
      {callbacks.map((member) => (
        <div key={member.id} className={HARNESS_GROUP_RAIL}>
          <MessageRow message={member} session={session} messageFontSize={14} />
        </div>
      ))}
    </div>
  );
}

export function Fixture() {
  const containerRef = useRef<HTMLDivElement>(null);
  const params = new URLSearchParams(location.search);
  const failure = params.has('failure');
  return (
    <div ref={containerRef} className="h-dvh overflow-y-auto bg-background px-4 py-32 text-foreground">
      {params.has('harness') ? (
        <HarnessRows />
      ) : (
        <MessageRow message={failure ? notice : message} session={session} messageFontSize={14} />
      )}
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
