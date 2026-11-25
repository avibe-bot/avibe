// Setup's optional remote-access step, wired (`/setup/remote-access`): the same claim as Settings ·
// Devices. In a browser, turning it on leaves for avibe.bot once and comes back to the Devices page,
// where the phone is added. In the Desktop App it asks the shell for its claim dialog and, bound,
// goes on to the Devices page. Skipping goes to the workbench. Setup is already complete here.
import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';

import { hasDesktopClient } from '../../desktop/bridge';
import { useDesktopClaimRequest } from '../../desktop/useDesktopClaimRequest';
import { onPageReactivated } from '../../lib/pageActivity';
import { devicesApi } from '../devices/api';
import { DEVICES_PATH } from '../devices/paths';
import type { ClaimProblem } from '../devices/types';
import { useClaimFlow, type ClaimEnd } from '../devices/useClaimFlow';
import { useRebindOffered } from '../devices/useRebindOffered';
import { claimProblem } from '../devices/wire';
import { SetupRemoteAccessView } from './SetupRemoteAccessView';

export function SetupRemoteAccess() {
  const { i18n } = useTranslation();
  const navigate = useNavigate();
  const [problem, setProblem] = useState<ClaimProblem | undefined>();
  const terminal = useRebindOffered();

  const onEnd = useCallback((end: ClaimEnd) => {
    // Bound (here, or already): the Devices page shows it and offers the phone.
    if (end.state === 'bound' || end.code === 'already_bound') navigate(DEVICES_PATH, { replace: true });
    else setProblem(claimProblem(end.code === 'no_answer' ? undefined : end.code, {
        claimId: end.claimId, seconds: end.seconds, confirmingEmail: end.claim?.confirmingEmail,
      }));
  }, [navigate]);
  const flow = useClaimFlow({ kind: 'claim', locale: i18n.language, onEnd });

  const desktop = hasDesktopClient();
  const claimRequest = useDesktopClaimRequest();
  // Opened again while bound (the back button after turning it on), or shown again after the
  // Desktop's claim dialog bound it: nothing to offer here.
  useEffect(() => {
    let current = true;
    const check = () => {
      void devicesApi.status().then((status) => {
        if (current && status.paired === true) navigate(DEVICES_PATH, { replace: true });
      }).catch(() => undefined);
    };
    check();
    const stop = onPageReactivated(check);
    return () => { current = false; stop(); };
  }, [navigate]);

  // A claim this tab left open is followed on the Devices page, with its way to cancel: also one
  // the instance is still trying to hand to avibe.bot, which would otherwise hold this optional
  // step with both of its buttons off and nothing said.
  const open = flow.phase === 'waiting' || flow.phase === 'retrying' || flow.phase === 'binding' || flow.lost !== null;
  useEffect(() => {
    if (open) navigate(DEVICES_PATH, { replace: true });
  }, [navigate, open]);

  if (desktop) {
    return <SetupRemoteAccessView surface="desktop" busy={claimRequest.requested} onTurnOn={claimRequest.request}
      onSkip={() => navigate('/', { replace: true })} />;
  }
  return (
    <SetupRemoteAccessView
      surface="browser"
      busy={flow.phase !== 'idle'}
      problem={problem}
      terminal={terminal}
      onTurnOn={() => { setProblem(undefined); void flow.start(); }}
      onSkip={() => navigate('/', { replace: true })}
    />
  );
}

export default SetupRemoteAccess;
