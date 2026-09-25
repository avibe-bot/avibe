import { lazy, Suspense, useEffect, useState } from 'react';

const STORAGE_KEY = 'vibe-remote:annotate';
const HASH_ENABLE = '#ai';
const HASH_DISABLE = '#ai-off';

const Agentation = lazy(() =>
  import('agentation').then((m) => ({ default: m.Agentation }))
);

// navigator.clipboard.writeText is unavailable on insecure contexts
// (e.g. http:// LAN IPs like http://192.168.x.x:15130). Vibe Remote is
// commonly accessed that way, so we fall back to execCommand('copy').
const copyText = (text: string) => {
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).catch(() => execCommandCopy(text));
    return;
  }
  execCommandCopy(text);
};

const execCommandCopy = (text: string) => {
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  textarea.style.top = '0';
  document.body.appendChild(textarea);
  textarea.select();
  try {
    document.execCommand('copy');
  } catch {
    // best-effort; nothing else to do
  } finally {
    document.body.removeChild(textarea);
  }
};

export const AgentationToggle = () => {
  const [enabled, setEnabled] = useState(false);

  useEffect(() => {
    const sync = () => {
      const hash = window.location.hash;
      if (hash === HASH_ENABLE) {
        window.localStorage.setItem(STORAGE_KEY, '1');
        setEnabled(true);
      } else if (hash === HASH_DISABLE) {
        window.localStorage.removeItem(STORAGE_KEY);
        setEnabled(false);
      } else {
        setEnabled(window.localStorage.getItem(STORAGE_KEY) === '1');
      }
    };

    sync();
    window.addEventListener('hashchange', sync);
    return () => window.removeEventListener('hashchange', sync);
  }, []);

  if (!enabled) return null;
  return (
    <div data-settings-interaction-owner="true">
      <Suspense fallback={null}>
        <Agentation
          copyToClipboard={false}
          onCopy={copyText}
          onSubmit={(output) => copyText(output)}
        />
      </Suspense>
    </div>
  );
};
