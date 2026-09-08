import { useRef, useState } from 'react';
import { Check, RotateCcw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { WorkbenchMessage } from '../../context/ApiContext';
import { isRetryableFailureNotice } from '../../lib/chatMessageTypes';
import { Button } from '../ui/button';

export function FailureRetry({
  message,
  readOnly,
  disabled,
  onRetry,
}: {
  message: WorkbenchMessage;
  readOnly?: boolean;
  disabled?: boolean;
  onRetry: (messageId: string) => Promise<boolean>;
}) {
  const { t } = useTranslation();
  const pending = useRef(false);
  const [sending, setSending] = useState(false);
  const [requested, setRequested] = useState(false);
  const action = message.content?.failure_retry as { state?: string } | undefined;
  // Local success bridges the response only until a durable state is present.
  // In particular, a later retirement must unlock this same mounted row.
  const admitted = action?.state
    ? ['queued', 'claimed', 'accepted'].includes(action.state)
    : requested;
  if (readOnly || !isRetryableFailureNotice(message)) return null;
  return (
    <Button
      type="button"
      size="sm"
      variant="secondary"
      disabled={disabled || sending || admitted}
      aria-busy={sending}
      onClick={async () => {
        if (pending.current || admitted || disabled) return;
        pending.current = true;
        setSending(true);
        try {
          setRequested(await onRetry(message.id));
        } finally {
          pending.current = false;
          setSending(false);
        }
      }}
    >
      {admitted ? <Check className="size-3.5" /> : <RotateCcw className="size-3.5" />}
      {t(admitted ? 'chat.retryRequested' : 'common.retry')}
    </Button>
  );
}
