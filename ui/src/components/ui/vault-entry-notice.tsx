import { AlertTriangle } from 'lucide-react';
import { Link, useLocation } from 'react-router-dom';
import { settingsOverlayContinuationState } from '@/lib/settingsOverlay';
import { useTranslation } from 'react-i18next';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import type { VaultEntryIssue } from '@/lib/vaultEntry';
import { Button } from './button';

function RemoteAccessLink() {
  const { t } = useTranslation();
  const location = useLocation();
  return <Button asChild variant="outline" size="sm" className="self-start">
    <Link to="/settings/remote-access" state={settingsOverlayContinuationState(location)}>{t('vaults.entry.remoteAccess')}</Link>
  </Button>;
}

/** ui/design: vault/entry-notice/owner, vault/entry-notice/member-phone.
 * Navigation only: no config changes, handoff parameters, draft or secret data.
 * Desktop handoff has its own flow and must not be replaced with address advice.
 */
export function VaultEntryNotice({ issue, warnDraftDiscard = false }: { issue: VaultEntryIssue | null; warnDraftDiscard?: boolean }) {
  const { t } = useTranslation();
  const { capabilities } = useInstanceAuthorization();
  if (!issue || issue === 'desktop') return null;
  return (
    <div role="alert" className="flex flex-col gap-2 rounded-lg border border-gold/40 bg-gold/10 p-3 text-xs leading-relaxed text-gold-ink">
      <div className="flex items-start gap-2 font-medium">
        <AlertTriangle className="mt-0.5 size-4 shrink-0" />
        <span>{t(issue === 'address' ? 'vaults.entry.addressTitle' : 'vaults.entry.browserTitle')}</span>
      </div>
      <p>{t(issue === 'browser' ? 'vaults.entry.browserHelp' : capabilities.can_manage_instance ? 'vaults.entry.ownerHelp' : 'vaults.entry.memberHelp')}</p>
      <p>{t('vaults.protectedUnlock.unlockUnavailableHere')}</p>
      {issue === 'address' && capabilities.can_manage_instance && <>
        {warnDraftDiscard && <p>{t('vaults.entry.draftWarning')}</p>}
        <RemoteAccessLink />
      </>}
    </div>
  );
}
