import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';

import { sessionChatPath } from '@/apps/showPageAvatar';
import { isDesktopShell, isNativeAppWindow } from '@/lib/desktopShell';

export const DESKTOP_SHOW_CHAT_EVENT = 'avibe:desktop-show-chat';

/** Navigate in the existing document: reloading main retires its native App windows. */
export function DesktopShowChatCommand() {
  const navigate = useNavigate();
  useEffect(() => {
    if (!isDesktopShell() || isNativeAppWindow()) return;
    const open = (event: Event) => {
      const sessionId: unknown = (event as CustomEvent).detail;
      if (typeof sessionId !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$/.test(sessionId)) return;
      navigate(sessionChatPath(sessionId, { showChat: true }));
    };
    window.addEventListener(DESKTOP_SHOW_CHAT_EVENT, open);
    return () => window.removeEventListener(DESKTOP_SHOW_CHAT_EVENT, open);
  }, [navigate]);
  return null;
}
