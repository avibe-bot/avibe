import { StrictMode, useEffect, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { Composer, type ComposerHandle } from '../../src/components/workbench/Composer';
import { ToastProvider } from '../../src/context/ToastProvider';
import i18n from '../../src/i18n';
import '../../src/index.css';

void i18n.changeLanguage('en');

declare global {
  interface Window {
    voiceShortcutTest: {
      sent: string[];
    };
  }
}

window.voiceShortcutTest = { sent: [] };

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
  return (
    <Composer
      ref={composerRef}
      sessionId="voice-shortcut-session"
      onSend={(text) => {
        window.voiceShortcutTest.sent.push(text);
        return true;
      }}
      onSearchAgents={async () => []}
      onSearchSessions={async () => []}
    />
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ToastProvider>
      <VoiceShortcutHarness />
    </ToastProvider>
  </StrictMode>,
);
