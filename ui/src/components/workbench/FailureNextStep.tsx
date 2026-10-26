import { useRef, useState } from 'react';
import { ArrowRight, Check } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';

import type { WorkbenchMessage } from '../../context/ApiContext';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import { ADMITTED_RETRY_STATES, isBackendFailureNotice, offersContinueOnVibey } from '../../lib/chatMessageTypes';
import { settingsOverlayContinuationState } from '../../lib/settingsOverlay';
import { MODEL_HUB_SETTINGS_PATH } from '../settings/models/modelHubRoutes';
import { Button } from '../ui/button';
import type { FailureRetryOptions } from './FailureRetry';

/** The step a failure notice names as data (`failure_next_step`), drawn from the Workbench's
 *  own copy: a notice is plain text, so any link in it comes from here, never from its body.
 *  Models is for those who manage the instance; anyone else is told who can fix it. Opening Models
 *  writes nothing to the Session, so a read-only transcript shows the step too. Continuing on
 *  Vibey writes the Session, so it needs a writable transcript. */
export function FailureNextStep({
  message,
  readOnly,
  disabled,
  onRetry,
}: {
  message: WorkbenchMessage;
  readOnly?: boolean;
  disabled?: boolean;
  onRetry?: (messageId: string, options?: FailureRetryOptions) => Promise<boolean>;
}) {
  if (!isBackendFailureNotice(message)) return null;
  if (message.metadata?.failure_next_step === 'models') return <OpenModelsStep />;
  if (!readOnly && onRetry && offersContinueOnVibey(message)) {
    return <ContinueOnVibeyStep message={message} disabled={disabled} onRetry={onRetry} />;
  }
  return null;
}

function OpenModelsStep() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { capabilities } = useInstanceAuthorization();
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

function ContinueOnVibeyStep({
  message,
  disabled,
  onRetry,
}: {
  message: WorkbenchMessage;
  disabled?: boolean;
  onRetry: (messageId: string, options?: FailureRetryOptions) => Promise<boolean>;
}) {
  const { t } = useTranslation();
  const pending = useRef(false);
  const [sending, setSending] = useState(false);
  const [requested, setRequested] = useState(false);
  const action = message.content?.failure_retry as { state?: string } | undefined;
  // As with Retry: the durable retry state decides once present; local success only
  // bridges the response.
  const admitted = action?.state ? ADMITTED_RETRY_STATES.includes(action.state) : requested;
  return (
    <Button
      type="button"
      size="2xs"
      variant="secondary"
      disabled={disabled || sending || admitted}
      aria-busy={sending}
      onClick={async () => {
        if (pending.current || admitted || disabled) return;
        pending.current = true;
        setSending(true);
        try {
          setRequested(await onRetry(message.id, { continueOnBackend: 'vibey' }));
        } finally {
          pending.current = false;
          setSending(false);
        }
      }}
    >
      {admitted ? <Check /> : <ArrowRight />}
      {t(admitted ? 'chat.failureNextStep.continuedOnVibey' : 'chat.failureNextStep.continueOnVibey')}
    </Button>
  );
}
