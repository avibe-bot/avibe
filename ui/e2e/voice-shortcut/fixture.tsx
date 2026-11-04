import { StrictMode, useEffect, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { MobileBotCenter } from '../../src/components/MobileChrome';
import { MainBotHoldOverlay, MainBotVoiceToast } from '../../src/components/voice/MainBotVoice';
import { useHoldToTalk } from '../../src/components/voice/useHoldToTalk';
import { Composer, type ComposerHandle } from '../../src/components/workbench/Composer';
import { ToastProvider } from '../../src/context/ToastProvider';
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
    };
  }
}

window.voiceShortcutTest = { sent: [], spoken: [], opened: 0 };

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
    return new Response(JSON.stringify({ text: 'Send this transcript', cleanup: 'success' }), {
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

const surface = new URLSearchParams(location.search).get('surface');
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ToastProvider>
      {surface === 'center' ? <MainBotCenterHarness /> : <VoiceShortcutHarness />}
    </ToastProvider>
  </StrictMode>,
);
