import { ArrowRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';

import type { WorkbenchMessage } from '../../context/ApiContext';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import { isBackendFailureNotice } from '../../lib/chatMessageTypes';
import { settingsOverlayContinuationState } from '../../lib/settingsOverlay';
import { MODEL_HUB_SETTINGS_PATH } from '../settings/models/modelHubRoutes';
import { Button } from '../ui/button';

/** The step a failure notice names as data (`failure_next_step`), drawn from the Workbench's
 *  own copy: a notice is plain text, so any link in it comes from here, never from its body.
 *  Models is for those who manage the instance; anyone else is told who can fix it. Opening Models
 *  writes nothing to the Session, so a read-only transcript shows the step too. */
export function FailureNextStep({ message }: { message: WorkbenchMessage }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { capabilities } = useInstanceAuthorization();
  if (!isBackendFailureNotice(message) || message.metadata?.failure_next_step !== 'models') return null;
  if (!capabilities.can_manage_instance) {
    return <span className="px-0.5 text-[11px] text-muted-foreground">{t('chat.failureNextStep.askAdmin')}</span>;
  }
  return (
    <Button
      type="button"
      size="2xs"
      variant="secondary"
      onClick={() => navigate(MODEL_HUB_SETTINGS_PATH, { state: settingsOverlayContinuationState(location) })}
    >
      {t('chat.failureNextStep.openModels')}
      <ArrowRight />
    </Button>
  );
}
