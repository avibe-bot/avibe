import { useContext, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';
import { useComposeTarget } from '../context/ComposerBridgeContext';
import { useStandaloneAppTab } from '../context/StandaloneAppTabContext';
import { ToastContext } from '../context/ToastContext';
import { botSessionPath, MAIN_BOT_SESSION_ID } from '../lib/bots';
import { createReceiver, RECEIVER } from './desktopComposeReceiver';

/** Installs the receiver while mounted; renders nothing. DesktopCompose decides where it mounts. */
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
  return null;
}
