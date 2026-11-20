// The actual Ask Vibey receiver runs inside WKWebView, driven by the production
// Rust queue/eval/streaming path. Only the conversation's final compose target
// is a fixture; it records staged input and has no message-send operation.
import { createReceiver, RECEIVER } from '../../src/desktop/desktopComposeReceiver';
import type { ComposeTarget } from '../../src/context/ComposerBridgeContext';

const observed = { lines: [] as string[], files: [] as { name: string; text: string }[], notices: [] as string[], opened: 0 };
const target: ComposeTarget = {
  sessionId: 'ses_main', composerReady: true, showChat: () => {},
  appendLine: (line) => {
    observed.lines.push(line);
    document.querySelector('textarea')!.value = observed.lines.join('\n');
    return true;
  },
  addFiles: (files) => {
    for (const file of files) void file.text().then((text) => { observed.files.push({ name: file.name, text }); });
    return true;
  },
};
Object.defineProperty(window, '__APP_LOCK_COMPOSE_OBSERVED__', { value: observed });
const receiver = createReceiver({
  target: () => target, openVibey: () => { observed.opened += 1; }, toast: (key) => { observed.notices.push(key); },
});
Object.defineProperty(window, RECEIVER, { get: () => (window as unknown as { __APP_LOCK_COMPOSE_PAUSED__?: boolean }).__APP_LOCK_COMPOSE_PAUSED__ ? undefined : receiver });
