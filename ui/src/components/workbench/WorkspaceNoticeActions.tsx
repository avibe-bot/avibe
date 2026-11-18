import { useCallback } from 'react';
import { ArrowUpRight } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import type { WorkbenchMessage } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { isRetriedFailureNotice } from '../../lib/chatMessageTypes';
import { requestFailureRetry, retryAdmitted } from '../../lib/failureRetryRequest';
import { selectApiErrorFields } from '../../context/apiErrorParse';
import { Button } from '../ui/button';
import { FailureRetry } from './FailureRetry';
import { chatHref } from './useCardOpen';
import type { WorkspaceNoticeSource } from '../../lib/workspaceNoticeSource';

/**
 * Under a failure notice in the 工作区通知 thread: open the Session the failure
 * came from, and 「重试」 it there, with the chat page's own retry (the same
 * request, the same button and its started state). Retry is offered only where
 * the server says that failed Turn can still be retried, or where it already was
 * (shown as started). A notice without a Session draws neither (ui/design:
 * chat/workspace-notices/*).
 */
export function WorkspaceNoticeActions({ message, source }: { message: WorkbenchMessage; source: WorkspaceNoticeSource }) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const retry = useCallback(async (noticeId: string) => {
    try {
      const { response, body } = await requestFailureRetry(source.session_id, noticeId);
      if (response.ok) return retryAdmitted(body);
      const parsed = body ? selectApiErrorFields(body, t('chat.retryFailed')) : null;
      showToast(parsed?.code ? t(`errors.${parsed.code}`, { defaultValue: parsed.fallback }) : parsed?.fallback ?? t('chat.retryFailed'), 'warning');
    } catch {
      showToast(t('chat.retryFailed'), 'warning');
    }
    return false;
  }, [showToast, source.session_id, t]);
  // Retry where it can still start one, or where one already started (drawn as started). A retry that
  // was retired leaves no button: it would be refused.
  const retryShown = source.retry || isRetriedFailureNotice(message);
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <Button asChild type="button" size="2xs" variant="secondary">
        <Link to={chatHref(source.session_id)}>
          <ArrowUpRight />
          {t('chat.workspaceNotice.openSession')}
        </Link>
      </Button>
      {retryShown ? <FailureRetry message={message} eligible onRetry={retry} /> : null}
    </div>
  );
}
