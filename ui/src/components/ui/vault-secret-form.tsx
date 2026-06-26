import { useEffect, useMemo, useState } from 'react';
import type { FormEvent } from 'react';
import { Eye, EyeOff, Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useApi, type DependencyItem } from '@/context/ApiContext';
import { cn } from '@/lib/utils';
import { sealStandardCreateBlindBox } from '@/lib/vaultBlindBox';
import { Button } from './button';
import { Input } from './input';

const AVAULT_P2_MIN_VERSION = '0.1.3';

function versionAtLeast(current: string | null | undefined, minimum: string): boolean {
  if (!current) return false;
  const parse = (value: string) =>
    value
      .split('+', 1)[0]
      .split('-', 1)[0]
      .split('.')
      .map((part) => Number.parseInt(part, 10));
  const cur = parse(current);
  const min = parse(minimum);
  if (cur.some(Number.isNaN) || min.some(Number.isNaN)) return false;
  const width = Math.max(cur.length, min.length);
  for (let i = 0; i < width; i += 1) {
    const left = cur[i] ?? 0;
    const right = min[i] ?? 0;
    if (left !== right) return left > right;
  }
  return true;
}

function avaultP2Ready(dep: DependencyItem | null): boolean {
  return dep?.status === 'ready' && versionAtLeast(dep.version, AVAULT_P2_MIN_VERSION);
}

export const VaultSecretForm: React.FC<{
  fixedName?: string;
  onCancel: () => void;
  onCreated: (name: string) => void;
  className?: string;
}> = ({ fixedName, onCancel, onCreated, className }) => {
  const { t } = useTranslation();
  const api = useApi();
  const [name, setName] = useState(fixedName ?? '');
  const [value, setValue] = useState('');
  const [group, setGroup] = useState('');
  const [description, setDescription] = useState('');
  const [allowHosts, setAllowHosts] = useState('');
  const [showValue, setShowValue] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [checkingAvault, setCheckingAvault] = useState(true);
  const [avaultDep, setAvaultDep] = useState<DependencyItem | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setCheckingAvault(true);
    api
      .listDependencies()
      .then((res) => {
        if (!alive) return;
        setAvaultDep(res.deps.find((dep) => dep.id === 'avault') ?? null);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (alive) setCheckingAvault(false);
      });
    return () => {
      alive = false;
    };
  }, [api]);

  const p2Ready = useMemo(() => avaultP2Ready(avaultDep), [avaultDep]);
  const secretName = (fixedName ?? name).trim().toUpperCase();
  const canSubmit = p2Ready && Boolean(secretName && value) && !submitting;

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const hosts = allowHosts
        .split(',')
        .map((host) => host.trim())
        .filter(Boolean);
      const pubkey = await api.getVaultPubkey();
      const blindBox = await sealStandardCreateBlindBox(secretName, value, pubkey);
      await api.createVaultSecret({
        name: secretName,
        protection: 'standard',
        blind_box: blindBox,
        group: group.trim() || undefined,
        description: description.trim() || undefined,
        policy: hosts.length ? { allowed_hosts: hosts } : undefined,
      });
      setValue('');
      onCreated(secretName);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form className={cn('flex flex-col gap-3', className)} onSubmit={onSubmit}>
      {!fixedName && (
        <label className="flex flex-col gap-1.5 text-sm font-medium">
          {t('vaults.dialog.name')}
          <Input value={name} onChange={(event) => setName(event.target.value)} autoFocus required />
        </label>
      )}
      <label className="flex flex-col gap-1.5 text-sm font-medium">
        {t('vaults.dialog.value')}
        <div className="flex items-center gap-2">
          <Input
            type={showValue ? 'text' : 'password'}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder={t('vaults.dialog.valuePlaceholder')}
            autoFocus={Boolean(fixedName)}
            required
            className="min-w-0 flex-1 font-mono"
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={() => setShowValue((current) => !current)}
            aria-label={showValue ? t('vaults.dialog.hideValue') : t('vaults.dialog.showValue')}
          >
            {showValue ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
          </Button>
        </div>
      </label>
      <label className="flex flex-col gap-1.5 text-sm font-medium">
        {t('vaults.dialog.group')}
        <Input value={group} onChange={(event) => setGroup(event.target.value)} />
      </label>
      <label className="flex flex-col gap-1.5 text-sm font-medium">
        {t('vaults.dialog.description')}
        <Input value={description} onChange={(event) => setDescription(event.target.value)} />
      </label>
      <label className="flex flex-col gap-1.5 text-sm font-medium">
        {t('vaults.dialog.allowHosts')}
        <Input value={allowHosts} onChange={(event) => setAllowHosts(event.target.value)} />
        <span className="text-xs text-muted-foreground">{t('vaults.dialog.allowHostsHelp')}</span>
      </label>
      {checkingAvault && (
        <div className="flex items-center gap-2 rounded-lg border border-border bg-surface-2 px-3 py-2 text-sm text-muted">
          <Loader2 className="size-4 animate-spin" />
          {t('vaults.dialog.checkingAvault')}
        </div>
      )}
      {!checkingAvault && !p2Ready && (
        <div className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning">
          {t('vaults.dialog.p2Unavailable', { version: AVAULT_P2_MIN_VERSION, installed: avaultDep?.version ?? 'unknown' })}
        </div>
      )}
      {error && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}
      <div className="mt-2 flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onCancel} disabled={submitting}>
          {t('vaults.dialog.cancel')}
        </Button>
        <Button type="submit" disabled={!canSubmit}>
          {submitting && <Loader2 className="size-4 animate-spin" />}
          {t('vaults.dialog.save')}
        </Button>
      </div>
    </form>
  );
};
