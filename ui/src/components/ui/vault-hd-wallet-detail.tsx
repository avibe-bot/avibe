import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
// A seed-phrase wallet's accounts (ui/design: vault/mnemonic/wallet/*): one card per account with
// its three addresses (owner decision 2026-11-12: 3 = A), a name, its own ask-before-signing, and
// the Session that last signed. The addresses are the ones avault derived at import, shown as the
// server lists them: the UI never computes or rewrites an address it shows here.
import { useEffect, useRef, useState } from 'react';
import { Bot, Clock3, Layers, Lock, Pencil, Plus, Sparkles, WalletCards } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useApi, type VaultHdAccount, type VaultSecret } from '@/context/ApiContext';
import { formatRelativeTime } from '@/lib/relativeTime';
import { vaultErrorMessage } from '@/lib/vaultErrors';
import { cn } from '@/lib/utils';
import { useAccountName } from '@/lib/vaultHdAccounts';
import { Badge } from './badge';
import { Button } from './button';
import { CopyButton } from './copy-button';
import { Dialog, DialogContent, DialogTitle } from './dialog';
import { InlineNameInput } from './inline-name-input';
import { Switch } from './switch';
import { HdAccountAddressList } from './vault-hd-wallet-steps';

export { AccountIndex } from './vault-hd-account-index';
import { AccountIndex } from './vault-hd-account-index';

export function HdWalletBadges({ count }: { count: number }) {
  const { t } = useTranslation();
  return (
    <>
      <Badge variant="secondary">{t('vaults.standard')}</Badge>
      <Badge variant="outline" className="border-violet/40 bg-violet-soft text-violet-ink">
        <Layers className="size-3" />
        {t('vaults.hd.wallet.badge', { count })}
      </Badge>
    </>
  );
}

/** The requesting Session's title, read with the reader's own access; its id when it cannot be read. */
function useSessionTitle(sessionId: string | null | undefined): string | null {
  const api = useApi();
  // The title is kept with the id it was read for, so a new signer never shows the last one's name.
  const [read, setRead] = useState<{ id: string; title: string | null } | null>(null);
  useEffect(() => {
    if (!sessionId) return undefined;
    let alive = true;
    api.getSession(sessionId, { handleError: false })
      .then((session) => { if (alive) setRead({ id: sessionId, title: session?.title || null }); })
      .catch(() => { if (alive) setRead({ id: sessionId, title: null }); });
    return () => { alive = false; };
  }, [api, sessionId]);
  if (!sessionId) return null;
  return (read?.id === sessionId ? read.title : null) || sessionId;
}

function LastSigned({ account }: { account: VaultHdAccount }) {
  const { t } = useTranslation();
  const session = useSessionTitle(account.last_signer?.session_id);
  if (!account.last_signer) {
    return (
      <span className="flex items-center gap-1.5 text-[11.5px] text-muted">
        <Clock3 className="size-3 shrink-0" />
        {t('vaults.hd.wallet.neverSigned')}
      </span>
    );
  }
  const when = formatRelativeTime(account.last_signer.at, t, undefined, { alwaysRelative: true });
  const chain = account.last_signer.chain;
  return (
    <span className="flex min-w-0 items-center gap-1.5 text-[11.5px] text-muted">
      <Clock3 className="size-3 shrink-0" />
      <span className="truncate">
        {chain
          ? t('vaults.hd.wallet.lastSignedChain', { chain: t(chain === 'evm' ? 'vaults.addresses.eth' : chain === 'bitcoin' ? 'vaults.addresses.btcTaproot' : 'vaults.addresses.solana'), when, session })
          : t('vaults.hd.wallet.lastSigned', { when, session })}
      </span>
    </span>
  );
}

function AccountCard({ wallet, account, fresh, canManage, onChanged }: {
  wallet: string;
  account: VaultHdAccount;
  fresh?: boolean;
  /** Renaming and ask-before-signing are a manager's actions (the account PATCH needs it). */
  canManage: boolean;
  onChanged: (secret: VaultSecret) => void;
}) {
  const { t } = useTranslation();
  const api = useApi();
  const nameOf = useAccountName();
  const [renaming, setRenaming] = useState(Boolean(fresh) && canManage);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const patch = async (payload: { name?: string | null; always_ask?: boolean }) => {
    setBusy(true);
    setError(null);
    try {
      const result = await api.updateVaultHdAccount(wallet, account.account, payload, { handleError: false });
      if (!result.ok || !result.secret) {
        setError(vaultErrorMessage(t, result, t('vaults.dialog.errors.updateFailed')));
        return;
      }
      onChanged(result.secret);
    } catch (err: unknown) {
      // A dropped connection is a failure too: the change did not happen as far as this page knows.
      setError(vaultErrorMessage(t, err, t('vaults.dialog.errors.updateFailed')));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className={cn('flex min-w-0 flex-col gap-2 rounded-[10px] border bg-surface p-3 shadow-elevation-0', fresh ? 'border-[1.5px] border-mint' : 'border-edge')}>
      <div className="flex min-w-0 items-center gap-2">
        <AccountIndex index={account.account} />
        {renaming ? (
          <span className="flex min-w-0 max-w-60 flex-1">
            <InlineNameInput
              initial={account.name ?? ''}
              placeholder={t('vaults.hd.wallet.namePlaceholder')}
              onCommit={(value) => {
                setRenaming(false);
                const next = value.trim() || null;
                if (next !== account.name) void patch({ name: next });
              }}
              onCancel={() => setRenaming(false)}
            />
          </span>
        ) : (
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="truncate text-[13.5px] font-semibold text-foreground">{nameOf(account)}</span>
            {canManage ? (
              <button
                type="button"
                aria-label={t('vaults.hd.wallet.rename')}
                title={t('vaults.hd.wallet.rename')}
                disabled={busy}
                onClick={() => setRenaming(true)}
                className="text-muted hover:text-foreground"
              >
                <Pencil className="size-3" />
              </button>
            ) : null}
          </span>
        )}
        <span className="ml-auto flex shrink-0 items-center gap-2">
          <span className="flex items-center gap-1 text-[11.5px] text-muted">
            {/* Turning it off needs a Passkey (owner, 2026-11-12); until that check exists it stays on. */}
            {account.always_ask ? (
              <span title={t('vaults.hd.wallet.askLocked')} className="inline-flex">
                <Lock className="size-3" aria-label={t('vaults.hd.wallet.askLocked')} />
              </span>
            ) : null}
            {t('vaults.hd.wallet.ask')}
          </span>
          <Switch
            size="sm"
            checked={account.always_ask}
            disabled={busy || account.always_ask || !canManage}
            onCheckedChange={(checked) => { if (checked) void patch({ always_ask: true }); }}
            label={t('vaults.hd.wallet.ask')}
          />
        </span>
      </div>
      <LastSigned account={account} />
      <HdAccountAddressList account={account} />
      {error ? <span role="alert" className="text-xs text-destructive-ink">{error}</span> : null}
    </div>
  );
}

/** The wallet's accounts, in a dialog opened from its row. */
export function VaultHdWalletDialog({ secret, open, onOpenChange, onChanged }: {
  secret: VaultSecret;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChanged: (secret: VaultSecret) => void;
}) {
  const { t } = useTranslation();
  const api = useApi();
  // Adding, renaming and ask-before-signing need instance management; others can read addresses.
  const canManage = useInstanceAuthorization().capabilities.can_manage_instance;
  const accounts = [...(secret.accounts ?? [])].sort((a, b) => a.account - b.account);
  const limit = secret.account_limit ?? 20;
  const next = accounts.length;
  const [fresh, setFresh] = useState<number | null>(null);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const add = async () => {
    setAdding(true);
    setError(null);
    try {
      const result = await api.enableVaultHdAccount(secret.name, { handleError: false });
      if (!result.ok || !result.secret) {
        setError(vaultErrorMessage(t, result, t('vaults.dialog.errors.updateFailed')));
        return;
      }
      setFresh(next);
      onChanged(result.secret);
    } catch (err: unknown) {
      setError(vaultErrorMessage(t, err, t('vaults.dialog.errors.updateFailed')));
    } finally {
      setAdding(false);
    }
  };
  // A wallet saved before the three Bitcoin types: derive its SegWit and Legacy keys from the sealed
  // seed once, when its accounts are opened by someone who may (never while listing; no words).
  // Until then, and for everyone else, Bitcoin shows Taproot only.
  const [prepare, setPrepare] = useState<'idle' | 'avault-old' | 'failed'>('idle');
  // The latest handler, read when the answer arrives: the page passes a new callback on every render
  // (its grant clock ticks each second), and that must not drop an answer still on its way.
  const onChangedRef = useRef(onChanged);
  useEffect(() => { onChangedRef.current = onChanged; }, [onChanged]);
  useEffect(() => {
    if (!open || !canManage || secret.bitcoin_profile === 'bitcoin-address-types-v1') return;
    let cancelled = false;
    void api.prepareVaultBitcoinAddressTypes(secret.name, { handleError: false }).then((result) => {
      if (cancelled) return;
      if (result.ok && result.secret) {
        onChangedRef.current(result.secret);
        return;
      }
      // Old avault: the existing upgrade notice. A disagreement with what the wallet already holds:
      // say so and offer nothing new. A refusal or a wallet that changed meanwhile: keep what is shown.
      if (result.code === 'avault_upgrade_required') setPrepare('avault-old');
      else if (result.code === 'hd_bitcoin_types_integrity_failed') setPrepare('failed');
    }).catch(() => undefined);
    // Cancelled only when the dialog closes, shows another wallet, or the wallet is prepared.
    return () => { cancelled = true; };
  }, [api, canManage, open, secret.bitcoin_profile, secret.name]);
  const usage = accounts.length > 1 ? 1 : 0;
  const command = `vibe vault sign ${secret.name} --chain solana --account ${usage}`;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogTitle className="sr-only">{secret.name}</DialogTitle>
        <div className="flex min-w-0 flex-col gap-4">
          <div className="flex min-w-0 items-start gap-3 pr-6">
            <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-violet/15 text-violet-ink">
              <WalletCards className="size-5" />
            </span>
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <span className="flex flex-wrap items-center gap-2">
                <span className="truncate font-mono text-[15px] font-semibold text-foreground">{secret.name}</span>
                <HdWalletBadges count={accounts.length} />
              </span>
              <span className="text-xs text-muted">{t('vaults.hd.wallet.used', { count: accounts.length, limit })}</span>
            </div>
          </div>
          <div className="flex min-w-0 flex-col gap-1.5 rounded-[10px] border border-edge bg-surface px-3 py-2.5 shadow-elevation-0">
            <span className="flex items-center gap-1.5 text-[12px] font-medium text-foreground">
              <Bot className="size-3.5 text-muted" />
              {t('vaults.hd.wallet.agentUsage')}
            </span>
            <div className="flex min-w-0 items-center gap-2">
              <code className="block min-w-0 flex-1 break-words rounded-md bg-surface-2 px-2.5 py-1.5 font-mono text-[11.5px] text-foreground">{t('vaults.hd.signCommand', { name: secret.name, account: usage })}</code>
              <CopyButton value={command} label={null} variant="ghost" />
            </div>
          </div>
          {prepare === 'avault-old' ? (
            <span role="note" className="text-[11.5px] text-muted">{t('vaults.hd.wallet.bitcoinTypesAvaultOld')}</span>
          ) : prepare === 'failed' ? (
            <span role="alert" className="text-[11.5px] text-destructive-ink">{t('vaults.hd.wallet.bitcoinTypesFailed')}</span>
          ) : null}
          <div className="flex flex-col gap-2.5">
            {accounts.map((account) => (
              <AccountCard key={account.account} wallet={secret.name} account={account} fresh={fresh === account.account} canManage={canManage} onChanged={onChanged} />
            ))}
          </div>
          {!canManage ? (
            <span role="note" className="self-center text-center text-[11.5px] text-muted">{t('vaults.hd.wallet.needsManager')}</span>
          ) : accounts.length === 1 ? (
            <div className="flex flex-col items-center gap-2 rounded-[10px] border border-dashed border-border-strong px-4 py-4 text-center">
              <span className="flex size-9 items-center justify-center rounded-xl bg-violet-soft text-violet-ink">
                <Sparkles className="size-4" />
              </span>
              <span className="text-[13px] font-semibold text-foreground">{t('vaults.hd.wallet.firstTitle')}</span>
              <span className="max-w-sm text-[12px] leading-snug text-muted">{t('vaults.hd.wallet.firstBody')}</span>
              <Button type="button" variant="secondary" size="sm" disabled={adding} onClick={() => void add()}>
                <Plus className="size-3.5" />
                {t('vaults.hd.wallet.add', { account: next })}
              </Button>
            </div>
          ) : next < limit ? (
            <div className="flex flex-col items-stretch gap-1">
              <Button type="button" variant="ghost" className="w-full justify-center border border-dashed border-border-strong" disabled={adding} onClick={() => void add()}>
                <Plus className="size-4" />
                {t('vaults.hd.wallet.add', { account: next })}
              </Button>
              <span className="self-center text-[11px] text-muted">{t('vaults.hd.wallet.addHint')}</span>
            </div>
          ) : (
            <span className="self-center text-[11.5px] text-muted">{t('vaults.hd.wallet.full', { limit })}</span>
          )}
          {error ? <span role="alert" className="text-xs text-destructive-ink">{error}</span> : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}
