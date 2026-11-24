// Setup's optional remote-access step, wired (`/setup/remote-access`): the same claim as Settings ·
// Devices. Turning it on leaves for avibe.bot once and comes back to the Devices page, where the
// phone is added; skipping goes to the workbench. Setup is already complete when this is shown.
import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';

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
    else setProblem(claimProblem(end.code === 'no_answer' ? undefined : end.code, { claimId: end.claimId, seconds: end.seconds }));
  }, [navigate]);
  const flow = useClaimFlow({ kind: 'claim', locale: i18n.language, onEnd });

  // Opened again while bound (the back button after turning it on): nothing to offer here.
  useEffect(() => {
    let current = true;
    void devicesApi.status().then((status) => {
      if (current && status.paired === true) navigate(DEVICES_PATH, { replace: true });
    }).catch(() => undefined);
    return () => { current = false; };
  }, [navigate]);

  // A claim this tab left open is followed on the Devices page, with its way to cancel: also one
  // the instance is still trying to hand to avibe.bot, which would otherwise hold this optional
  // step with both of its buttons off and nothing said.
  const open = flow.phase === 'waiting' || flow.phase === 'retrying' || flow.phase === 'binding' || flow.lost !== null;
  useEffect(() => {
    if (open) navigate(DEVICES_PATH, { replace: true });
  }, [navigate, open]);

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
