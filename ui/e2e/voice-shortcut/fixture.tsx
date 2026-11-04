import { StrictMode, useEffect, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { MobileBotCenter } from '../../src/components/MobileChrome';
import { MainBotHoldOverlay, MainBotVoiceToast } from '../../src/components/voice/MainBotVoice';
import { useHoldToTalk } from '../../src/components/voice/useHoldToTalk';
import { BotHoldProvider } from '../../src/components/workbench/bots/BotHold';
import { BotStrip } from '../../src/components/workbench/bots/BotStrip';
import { Composer, type ComposerHandle } from '../../src/components/workbench/Composer';
import { ApiProvider } from '../../src/context/ApiContext';
import { BotsContext, type BotsState } from '../../src/context/BotsContext';
import { ToastProvider } from '../../src/context/ToastProvider';
import { UnsavedChangesContext } from '../../src/context/unsavedChangesContext';
import type { Bot } from '../../src/lib/bots';
import i18n from '../../src/i18n';
import '../../src/index.css';

void i18n.changeLanguage('en');

declare global {
  interface Window {
    voiceShortcutTest: {
      sent: string[];
      /** Messages spoken by holding the composer, with what they were sent with. */
      spoken: { text: string; metadata: Record<string, unknown> }[];
      /** Times the main Bot's conversation was opened (a tap, or the sent toast's View). */
      opened: number;
      /** Messages a Bot's avatar sent, by its Session (`?surface=strip`). */
      posted: { session: string; text: string; metadata: Record<string, unknown> }[];
      /** The conversation each transcription request carried (the local relay's JSON). */
      transcribed: { reply?: string; user_previous?: string }[];
      /** Conversations a hold read its context from, by Session. */
      contextReads: string[];
    };
  }
}

window.voiceShortcutTest = { sent: [], spoken: [], opened: 0, posted: [], transcribed: [], contextReads: [] };

/** Each fixture conversation's last two rows: the user's message, then the Agent's reply. */
const conversationTail = (session: string) => [
  { id: `${session}-ask`, author: 'user', type: 'user', text: `Question for ${session}`, content: {}, metadata: {} },
  { id: `${session}-reply`, author: 'agent', type: 'result', text: `Answer from ${session}`, content: {}, metadata: {} },
];

window.fetch = async (input, init) => {
  const url = new URL(
    typeof input === 'string' ? input : input instanceof URL ? input.href : input.url,
    location.origin,
  );
  if (url.pathname === '/api/asr/status') {
    return new Response(JSON.stringify({ available: true, max_file_bytes: 25_000_000 }), {
      headers: { 'Content-Type': 'application/json' },
    });
  }
  if (url.pathname === '/api/cloud/token') {
    return new Response(JSON.stringify({ error: 'fixture local ASR only' }), { status: 503 });
  }
  if (url.pathname === '/api/asr/transcribe') {
    const body = JSON.parse(String(init?.body ?? '{}')) as { reply?: string; user_previous?: string };
    window.voiceShortcutTest.transcribed.push({ reply: body.reply, user_previous: body.user_previous });
    return new Response(JSON.stringify({ text: 'Send this transcript', cleanup: 'success' }), {
      headers: { 'Content-Type': 'application/json' },
    });
  }
  const posted = url.pathname.match(/^\/api\/sessions\/([^/]+)\/messages$/);
  if (posted && (init?.method ?? 'GET') === 'GET') {
    const session = decodeURIComponent(posted[1]);
    window.voiceShortcutTest.contextReads.push(session);
    return new Response(JSON.stringify({ messages: conversationTail(session), next_after_id: null }), {
      headers: { 'Content-Type': 'application/json' },
    });
  }
  if (posted && init?.method === 'POST') {
    const body = JSON.parse(String(init.body)) as { text: string; metadata: Record<string, unknown> };
    window.voiceShortcutTest.posted.push({ session: decodeURIComponent(posted[1]), text: body.text, metadata: body.metadata });
    return new Response(JSON.stringify({ id: `msg_${window.voiceShortcutTest.posted.length}`, session_id: posted[1], text: body.text }), {
      status: 201,
      headers: { 'Content-Type': 'application/json' },
    });
  }
  if (url.pathname === '/api/inbox') {
    return new Response(JSON.stringify({ sessions: [], next_cursor: null, unread_by_session: {}, unread_total: 0, unread_sessions: 0 }), {
      headers: { 'Content-Type': 'application/json' },
    });
  }
  if (url.pathname === '/api/csrf-token') {
    return new Response(JSON.stringify({ csrf_token: 'fixture' }), {
      headers: { 'Content-Type': 'application/json' },
    });
  }
  throw new Error(`Unexpected fixture request: ${init?.method ?? 'GET'} ${url.pathname}`);
};

export function VoiceShortcutHarness() {
  const composerRef = useRef<ComposerHandle>(null);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      composerRef.current?.handleVoiceShortcut(event, true);
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, []);
  // At the foot of the page, as in Chat: the take's card and its toast open above it.
  return (
    <div className="flex min-h-dvh flex-col justify-end p-3">
      <Composer
        ref={composerRef}
        sessionId="voice-shortcut-session"
        onSend={(text, _attachments, _references, metadata) => {
          window.voiceShortcutTest.sent.push(text);
          if (metadata) window.voiceShortcutTest.spoken.push({ text, metadata });
          return true;
        }}
        voiceTarget={{ name: 'Fix the cache', title: 'To “Fix the cache”' }}
        readVoiceContext={() => ({ reply: 'The cache is fixed on prod-7.', userPrevious: 'Is the cache fixed?' })}
        onSearchAgents={async () => []}
        onSearchSessions={async () => []}
      />
    </div>
  );
}

/** The phone tab bar's main Bot (`?surface=center`): hold it to speak, as MainBotTabBar wires it. */
export function MainBotCenterHarness() {
  const bot = { name: 'Vibey', avatar: { kind: 'mascot' as const } };
  const open = () => {
    window.voiceShortcutTest.opened += 1;
  };
  const hold = useHoldToTalk({
    send: async (text, metadata) => {
      window.voiceShortcutTest.sent.push(text);
      window.voiceShortcutTest.spoken.push({ text, metadata });
      return true;
    },
    onTap: open,
    context: () => ({ reply: 'Answer from ses_main', userPrevious: 'Question for ses_main' }),
  });
  const phase = { idle: 'idle', pressing: 'pressed', listening: 'recording', recording: 'recording', armed: 'armed', settling: 'settling', interrupted: 'idle' } as const;
  return (
    <>
      <nav className="fixed inset-x-0 bottom-0 flex h-(--mobile-nav-height) justify-center border-t border-border bg-surface">
        <MobileBotCenter avatar={bot.avatar} name={bot.name} label="Open Vibey" unread={0} running={false} onClick={open} phase={phase[hold.phase]} hold={hold.bind} />
      </nav>
      <MainBotHoldOverlay hold={hold} bot={bot} />
      <MainBotVoiceToast hold={hold} bot={bot} onView={open} />
    </>
  );
}

const fixtureBot = (handle: string, name: string, emoji: string, hue: number): Bot => ({
  handle,
  display_name: name,
  avatar: handle === 'main' ? { kind: 'mascot' } : { kind: 'emoji', emoji, hue },
  role: '',
  agent_name: handle,
  session_id: `ses_${handle}`,
  builtin: handle === 'main',
  hide_details: true,
});
const STRIP_BOTS = [
  fixtureBot('main', 'Vibey', '', 0),
  fixtureBot('pm', 'Product', '🧭', 42),
  fixtureBot('cso', 'Security', '🛡️', 200),
  fixtureBot('research', 'Research', '🔭', 265),
  fixtureBot('writer', 'Writer', '✍️', 150),
  fixtureBot('legal', 'Legal', '⚖️', 12),
];
const stripBots: BotsState = {
  enabled: true,
  bots: STRIP_BOTS,
  loaded: true,
  listed: true,
  mainBot: STRIP_BOTS[0],
  limit: { count: 5, max: 5 },
  atLimit: true,
  statusBySession: {},
  botForSession: (sessionId) => STRIP_BOTS.find((bot) => bot.session_id === sessionId),
  botForAgent: (name) => STRIP_BOTS.find((bot) => bot.agent_name === name),
  botForHandle: (handle) => STRIP_BOTS.find((bot) => bot.handle === handle),
  displayName: (bot) => bot.display_name,
  afterWrite: async () => undefined,
  holdOrder: () => () => undefined,
  setHideDetails: async () => true,
  openProfile: () => undefined,
  profileRequest: null,
  closeProfile: () => undefined,
};

/** Where a tap or the sent toast's View took the reader. */
function Opened() {
  return <output data-opened={useLocation().pathname} className="sr-only" />;
}

/** The Bots strip (`?surface=strip`): hold any Bot's avatar to speak to it, as BotHoldProvider wires
 *  it over the shell. More Bots than fit, so the strip also scrolls sideways. */
export function BotStripHarness() {
  return (
    <MemoryRouter>
      <ApiProvider>
        <UnsavedChangesContext.Provider value={{ setRegistration: () => {}, authorizeRouteAction: () => ({ runNavigation: (navigation) => navigation() }) }}>
          <BotsContext.Provider value={stripBots}>
            <BotHoldProvider>
              <div className="w-[248px] p-4 pt-24">
                <BotStrip unreadBySession={{ ses_cso: 2 }} />
              </div>
              <Routes>
                <Route path="*" element={<Opened />} />
              </Routes>
            </BotHoldProvider>
          </BotsContext.Provider>
        </UnsavedChangesContext.Provider>
      </ApiProvider>
    </MemoryRouter>
  );
}

const surface = new URLSearchParams(location.search).get('surface');
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ToastProvider>
      {surface === 'center' ? <MainBotCenterHarness /> : surface === 'strip' ? <BotStripHarness /> : <VoiceShortcutHarness />}
    </ToastProvider>
  </StrictMode>,
);
