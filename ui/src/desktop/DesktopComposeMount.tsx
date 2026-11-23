import { useContext, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';
import { useComposeTarget } from '../context/ComposerBridgeContext';
import { useStandaloneAppTab } from '../context/StandaloneAppTabContext';
import { ToastContext } from '../context/ToastContext';
import { botSessionPath, MAIN_BOT_SESSION_ID } from '../lib/bots';
import { apiFetch } from '../lib/apiFetch';
import { ANSWER_RECEIVER, createAnswerReceiver } from './desktopAnswerReceiver';
import { createReceiver, RECEIVER } from './desktopComposeReceiver';

/**
 * Installs the desktop shell's two document receivers while mounted, Ask Vibey's
 * and the notification answers'; renders nothing. DesktopCompose decides where it mounts.
 */
export function DesktopComposeMount() {
  const standalone = useStandaloneAppTab();
  const { t } = useTranslation();
  const showToast = useContext(ToastContext)?.showToast;
  const navigate = useNavigate();
  const location = useLocation();
  const target = useComposeTarget();
  const live = useRef({ target, location, navigate, showToast, t });
  // The receiver outlives renders; it reads the latest route and target here.
  useEffect(() => {
    live.current = { target, location, navigate, showToast, t };
  });

  useEffect(() => {
    if (standalone) return;
    const path = botSessionPath({ session_id: MAIN_BOT_SESSION_ID });
    const receiver = createReceiver({
      target: () => live.current.target,
      openVibey: () => {
        if (live.current.location.pathname !== path) live.current.navigate(path);
      },
      toast: (key) => live.current.showToast?.(live.current.t(key), 'error'),
    });
    const host = window as unknown as Record<string, unknown>;
    host[RECEIVER] = receiver;
    return () => {
      if (host[RECEIVER] === receiver) delete host[RECEIVER];
    };
  }, [standalone]);

  useEffect(() => {
    if (standalone) return;
    const receiver = createAnswerReceiver((path, body) => apiFetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }));
    const host = window as unknown as Record<string, unknown>;
    host[ANSWER_RECEIVER] = receiver;
    return () => {
      if (host[ANSWER_RECEIVER] === receiver) delete host[ANSWER_RECEIVER];
    };
  }, [standalone]);
  return null;
}
