// Exercise the shipped lazy DesktopCompose -> DesktopComposeMount path in
// WKWebView. Only router/composer context and the final input are test-owned.
import { useEffect } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, useLocation } from 'react-router-dom';
import '../../src/i18n';
import { DesktopCompose } from '../../src/desktop/DesktopCompose';
import { ComposerBridgeContext, type ComposeTarget } from '../../src/context/ComposerBridgeContext';
import { ToastContext } from '../../src/context/ToastContext';
import type { Receiver } from '../../src/desktop/desktopComposeReceiver';

const observed = { lines: [] as string[], files: [] as { name: string; text: string }[], notices: [] as string[], mounted: false, route: '' };
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
Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true });
let receiver: Receiver | undefined;
Object.defineProperty(window, '__AVIBE_DESKTOP_COMPOSE__', {
  configurable: true,
  get: () => (window as unknown as { __APP_LOCK_COMPOSE_PAUSED__?: boolean }).__APP_LOCK_COMPOSE_PAUSED__ ? undefined : receiver,
  set: (value: Receiver) => { receiver = value; observed.mounted = true; },
});
export function Fixture() {
  const location = useLocation();
  useEffect(() => { observed.route = location.pathname; }, [location.pathname]);
  return (
    <ToastContext.Provider value={{ showToast: (message) => { observed.notices.push(message); } }}>
      <ComposerBridgeContext.Provider value={{ target: null, setTarget: () => {}, compose: target }}>
        <DesktopCompose />
      </ComposerBridgeContext.Provider>
    </ToastContext.Provider>
  );
}
createRoot(document.getElementById('root')!).render(
  <MemoryRouter initialEntries={['/fixture']}><Fixture /></MemoryRouter>,
);
