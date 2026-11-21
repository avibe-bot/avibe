// Turning remote access on where the surface itself names the Avibe (the Desktop App; not wired
// to a page yet): the account and the name that becomes the domain.
import { useTranslation } from 'react-i18next';

import { accountInitialFor } from '../AccountAvatar';
import { Input } from '../ui/input';
import { DOMAIN_SUFFIX } from './rules';
import type { NameState } from './types';

/** The account, drawn as the App's own account row draws it: the initial and the email. */
function Account({ email, note }: { email: string; note?: string }) {
  return (
    <span className="flex min-w-0 items-center gap-2">
      <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-mint-soft text-[11px] font-semibold text-mint-ink">
        {accountInitialFor(email)}
      </span>
      <span className="min-w-0 truncate text-[13px] font-medium text-foreground">{email}</span>
      {note && <span className="shrink-0 text-[11px] text-muted">{note}</span>}
    </span>
  );
}

/**
 * The claim where the surface itself names the Avibe: the account this App is signed in to, and the
 * name that becomes the domain. Every new binding passes through a name; no domain is ever assigned
 * at random. The field is prefilled and always editable (owner, 2026-11-20). When someone else holds
 * the name it says 「已被占用」 and the person changes it; a number is never appended for them.
 */
export function ClaimFields({ account, name, nameState, onNameChange }: {
  account: string;
  name: string;
  nameState?: NameState;
  onNameChange?: (name: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="grid gap-x-6 gap-y-2 px-5 py-4 md:grid-cols-[72px_minmax(0,1fr)] md:items-center md:gap-y-3">
      <span className="text-[12px] text-muted">{t('devices.claim.account')}</span>
      <Account email={account} note={t('devices.claim.signedInHere')} />
      <span className="text-[12px] text-muted max-md:mt-2">{t('devices.claim.name')}</span>
      <div className="flex min-w-0 items-center gap-2">
        <Input
          className="w-44 text-[13px]"
          value={name}
          readOnly={!onNameChange}
          onChange={onNameChange ? (event) => onNameChange(event.target.value) : undefined}
          aria-label={t('devices.claim.name')}
          aria-invalid={nameState === 'taken' || undefined}
        />
        <span className="truncate font-mono text-[12px] text-muted">{t('devices.claim.suffix')}</span>
      </div>
      <span className="max-md:hidden" />
      {nameState === 'taken' ? (
        <span className="text-[11px] text-destructive-ink md:-mt-1.5">{t('devices.claim.taken')}</span>
      ) : (
        <span className="text-[11px] text-muted md:-mt-1.5">{t('devices.claim.domain', { host: `${name}${DOMAIN_SUFFIX}` })}</span>
      )}
    </div>
  );
}
