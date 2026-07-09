import { useCallback, useEffect, useRef, useState } from 'react';
import { Loader2, ShieldCheck } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useApi, type VaultSettings } from '@/context/ApiContext';
import { setVaultSandboxPolicy } from '@/lib/vaultSandboxPolicy';
import { resetVaultSandboxClient } from '@/lib/vaultSandboxClient';
import { useProtectedVault } from '@/lib/useProtectedVault';
import { Dialog, DialogContent, DialogTitle } from './dialog';
import { SegmentedRadio } from './segmented';
import { Switch } from './switch';

type UnlockWindowChoice = '300' | '600' | '1800';

function unlockWindowChoice(seconds: number | undefined): UnlockWindowChoice {
  if (seconds === 300) return '300';
  if (seconds === 1800) return '1800';
  return '600';
}

/**
 * Vault session settings (protocol v2 §8): the unlock-window length and the Strict-approvals
 * toggle, persisted daemon-side via `GET/PATCH /api/vault/settings`. Saving also refreshes the
 * parent's policy mirror so the next sandbox handshake/unlock runs under the new values (the
 * sandbox is the enforcer; this mirror is display + unlock-hint only).
 */
export const VaultSettingsDialog: React.FC<{ open: boolean; onOpenChange: (open: boolean) => void }> = ({
  open,
  onOpenChange,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const vault = useProtectedVault();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unlockWindow, setUnlockWindow] = useState<UnlockWindowChoice>('600');
  const [strict, setStrict] = useState(false);
  // Monotonic save token: if two changes race (e.g. window + Strict fired before `saving` disables
  // the controls), only the newest PATCH's response is applied to state + the policy mirror, so a
  // slower older response can't overwrite the newer settings.
  const saveGenRef = useRef(0);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setLoading(true);
    setError(null);
    api
      .getVaultSettings()
      .then((res) => {
        if (!alive) return;
        if (res?.ok) {
          setUnlockWindow(unlockWindowChoice(res.settings?.unlock_window_seconds));
          setStrict(Boolean(res.settings?.strict_approvals));
          setVaultSandboxPolicy(res.policy);
        } else {
          setError(res?.message || t('vaults.settings.loadFailed'));
        }
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [open, api, t]);

  // Auto-save on change — a settings dialog with two controls reads best as immediate toggles. The
  // control is updated optimistically for instant feedback, so `revert` restores the prior value if
  // the daemon rejects the change (otherwise the control would show a value that wasn't saved). The
  // daemon returns the normalized settings + policy, so mirror both back on success.
  const save = useCallback(
    async (patch: Partial<VaultSettings>, revert: () => void): Promise<boolean> => {
      const gen = (saveGenRef.current += 1);
      setSaving(true);
      setError(null);
      try {
        const res = await api.saveVaultSettings(patch);
        // A newer save started while this one was in flight — let the newest response own the
        // controls + policy mirror, and don't clear `saving` out from under it.
        if (gen !== saveGenRef.current) return Boolean(res?.ok);
        if (!res?.ok) {
          setError(res?.message || t('vaults.settings.saveFailed'));
          revert();
          return false;
        }
        setUnlockWindow(unlockWindowChoice(res.settings?.unlock_window_seconds));
        setStrict(Boolean(res.settings?.strict_approvals));
        setVaultSandboxPolicy(res.policy);
        return true;
      } catch (err: unknown) {
        if (gen !== saveGenRef.current) return false;
        setError(err instanceof Error ? err.message : String(err));
        revert();
        return false;
      } finally {
        if (gen === saveGenRef.current) setSaving(false);
      }
    },
    [api, t],
  );

  const onUnlockWindowChange = (next: UnlockWindowChoice) => {
    const prev = unlockWindow;
    setUnlockWindow(next);
    void save({ unlock_window_seconds: Number(next) }, () => setUnlockWindow(prev));
  };

  const onStrictChange = (next: boolean) => {
    const prev = strict;
    setStrict(next);
    void save({ strict_approvals: next }, () => setStrict(prev)).then((ok) => {
      if (!(ok && next && !prev)) return;
      // Enabling Strict must bite immediately, everywhere — not only on the next explicit unlock.
      // `vault.lock()` ends this tab's active window and broadcasts a lock to sibling tabs so none
      // keeps an unlocked non-Strict window. But the sandbox pins its enforced policy at handshake
      // and its internal auto-unlock (a protected op run while locked) reuses that pinned policy, so
      // locking alone isn't enough — force a fresh handshake (resetVaultSandboxClient) so the next
      // client re-pins the new Strict policy and every unlock path honors it. Disabling is a
      // relaxation and safely waits for the next unlock, so we don't force this in that direction.
      vault.lock();
      resetVaultSandboxClient();
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle className="sr-only">{t('vaults.settings.title')}</DialogTitle>
        <div className="flex items-start gap-3 pr-6">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-mint-soft text-mint">
            <ShieldCheck className="size-5" />
          </span>
          <div className="flex flex-col gap-0.5">
            <span className="text-[15px] font-semibold text-foreground">{t('vaults.settings.title')}</span>
            <span className="text-xs text-muted-foreground">{t('vaults.settings.subtitle')}</span>
          </div>
        </div>

        {loading ? (
          <div className="flex items-center gap-2 rounded-lg border border-border bg-surface-2 px-3 py-2 text-sm text-muted">
            <Loader2 className="size-4 animate-spin" />
            {t('vaults.settings.loading')}
          </div>
        ) : (
          <div className="flex flex-col gap-5">
            {/* Unlock window — "解锁窗口 / Unlock window" language (§8): the browser session, not the
                agent grant. Changes apply on the next unlock. */}
            <div className="flex flex-col gap-1.5">
              <span className="text-[13px] font-medium text-foreground">{t('vaults.settings.unlockWindow')}</span>
              <SegmentedRadio<UnlockWindowChoice>
                value={unlockWindow}
                onChange={onUnlockWindowChange}
                disabled={saving}
                ariaLabel={t('vaults.settings.unlockWindow')}
                options={[
                  { id: '300', label: t('vaults.settings.minutes', { count: 5 }) },
                  { id: '600', label: t('vaults.settings.minutes', { count: 10 }) },
                  { id: '1800', label: t('vaults.settings.minutes', { count: 30 }) },
                ]}
              />
              <span className="text-[11px] text-muted-foreground">{t('vaults.settings.unlockWindowHelp')}</span>
            </div>

            {/* Strict approvals — R2 behaves like R3 (a passkey every approve/reveal). */}
            <div className="flex items-start gap-3 rounded-[10px] border border-border bg-surface-2 px-3 py-3">
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <span className="text-[13px] font-semibold text-foreground">{t('vaults.settings.strict')}</span>
                <span className="text-[11.5px] leading-snug text-muted-foreground">{t('vaults.settings.strictHelp')}</span>
              </div>
              <Switch checked={strict} onCheckedChange={onStrictChange} disabled={saving} label={t('vaults.settings.strict')} />
            </div>
          </div>
        )}

        {error ? (
          <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
};
