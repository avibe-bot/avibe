import { KeyRound } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { VaultRequest, VaultRequestSpec } from '@/context/ApiContext';
import { Dialog, DialogContent, DialogTitle } from './dialog';
import { VaultSecretForm } from './vault-secret-form';

/**
 * The single add/provide-secret dialog: a consistent branded header (design.pen
 * `F4N19`/`vyed5`) over {@link VaultSecretForm}. Reused by every entry point — the
 * Vaults "Add" button, a Vaults pending-provision row, the `$<NAME>` chat card, and
 * the chat provision card — so they are visually and behaviorally identical.
 *
 * Create mode (no name/request) vs provide mode (a `$<NAME>` ask or a pending
 * provision request) only differ in the header copy and the seeded form fields.
 */
export const VaultSecretDialog: React.FC<{
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Explicit fixed name (e.g. a `$<NAME>` chat ask) when there may be no request object yet. */
  name?: string;
  /** A pending provision request; spec / default protection / id are derived from it. */
  request?: VaultRequest | null;
  /** Rendered in place of the form (loading / ambiguous-provision notices from callers). */
  notice?: React.ReactNode;
  onCreated: (name: string, reason?: 'created' | 'already_exists') => void;
}> = ({ open, onOpenChange, name, request, notice, onCreated }) => {
  const { t } = useTranslation();
  const card = (request?.card ?? null) as { default_protection?: unknown; spec?: VaultRequestSpec } | null;
  const requestSpec = (card?.spec ?? null) as VaultRequestSpec | null;
  const defaultProtection =
    card?.default_protection === 'standard' || card?.default_protection === 'protected' ? card.default_protection : undefined;
  const fixedName = name ?? request?.secret_name ?? undefined;
  const isProvide = Boolean(fixedName);
  const title = isProvide ? t('vaults.request.title') : t('vaults.dialog.title');
  const subtitle = isProvide ? t('vaults.request.help') : t('vaults.dialog.subtitle');

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        {/* Accessible name; the visible heading is the branded header below. */}
        <DialogTitle className="sr-only">{title}</DialogTitle>
        <div className="flex items-start gap-3 pr-6">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-accent/15 text-accent">
            <KeyRound className="size-5" />
          </span>
          <div className="flex flex-col gap-0.5">
            <span className="text-[15px] font-semibold text-foreground">{title}</span>
            <span className="text-xs text-muted-foreground">{subtitle}</span>
          </div>
        </div>
        {notice ?? (
          <VaultSecretForm
            fixedName={fixedName}
            provisionRequestId={request?.id ?? null}
            requestSpec={requestSpec}
            defaultProtection={defaultProtection}
            onCancel={() => onOpenChange(false)}
            onCreated={onCreated}
            treatExistingAsFulfilled={isProvide}
          />
        )}
      </DialogContent>
    </Dialog>
  );
};
