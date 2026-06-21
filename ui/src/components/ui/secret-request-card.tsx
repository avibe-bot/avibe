import { useState } from 'react';
import { KeyRound } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Badge } from './badge';
import { Button } from './button';
import { Input } from './input';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from './dialog';
import { useApi } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';

/**
 * Inline rendering of a `$<NAME>` dynamic-ask marker in an agent message. The agent
 * asked for a secret; the user provides it here over TLS — the value goes straight to
 * the vault via the normal create endpoint and never re-enters the chat transcript.
 * Self-contained (its own dialog) so it can live inline inside the markdown renderer.
 */
export const SecretRequestCard: React.FC<{ name: string }> = ({ name }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState('');
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    setSaving(true);
    setErr(null);
    try {
      await api.createVaultSecret({ name, value });
      setSaved(true);
      setOpen(false);
      showToast(t('vaults.created', { name }), 'success');
    } catch (e: any) {
      const msg = e?.message ?? String(e);
      if (/exist/i.test(msg)) {
        // Already in the vault — the request is effectively satisfied.
        setSaved(true);
        setOpen(false);
        showToast(t('vaults.created', { name }), 'success');
      } else {
        setErr(msg);
      }
    } finally {
      setSaving(false);
    }
  };

  if (saved) {
    return (
      <Badge variant="success" className="align-baseline font-medium">
        ✓ {name}
      </Badge>
    );
  }

  return (
    <>
      <button type="button" onClick={() => setOpen(true)} className="align-baseline">
        <Badge variant="warning" className="cursor-pointer align-baseline font-medium">
          <KeyRound className="mr-1 inline size-3" />
          {name} — {t('vaults.request.provide')}
        </Badge>
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('vaults.request.title', { name })}</DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-3">
            <p className="text-sm text-muted">{t('vaults.request.help')}</p>
            {err && (
              <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">{err}</div>
            )}
            <Input
              type="password"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder={t('vaults.dialog.valuePlaceholder')}
              autoFocus
            />
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setOpen(false)}>
                {t('vaults.dialog.cancel')}
              </Button>
              <Button onClick={submit} disabled={saving || !value}>
                {t('vaults.dialog.save')}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
};
