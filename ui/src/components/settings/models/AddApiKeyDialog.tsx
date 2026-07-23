// 添加 API Key dialog (frame 06r): the only form dialog for adding a source.
// Vendor picker prefills the official base URL (editable for compatible /
// relay endpoints); the primary button is test-and-add — it validates the key,
// discovers models, and reports the count before the dialog dismisses.
import * as React from 'react';
import { CheckCircle2, Globe, KeyRound, Plus, Shield, TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { cn } from '@/lib/utils';
import { modelsApi } from './modelsApi';
import { DEFAULT_VENDOR, VENDOR_OPTIONS } from './vendorMeta';
import type { Source } from './types';

type Phase = 'edit' | 'submitting' | 'done' | 'error';

const FieldLabel: React.FC<{ mono?: boolean; children: React.ReactNode }> = ({ mono, children }) => (
  <label
    className={cn(
      'text-muted',
      mono
        ? 'font-mono text-[11px] font-medium uppercase tracking-wide'
        : 'text-[12px] font-semibold text-foreground',
    )}
  >
    {children}
  </label>
);

const IconField: React.FC<{
  icon: React.ComponentType<{ className?: string }>;
  children: React.ReactNode;
}> = ({ icon: Icon, children }) => (
  <div className="relative">
    <Icon className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
    {children}
  </div>
);

export const AddApiKeyDialog: React.FC<{
  open: boolean;
  onClose: () => void;
  onAdded: (source: Source) => void;
}> = ({ open, onClose, onAdded }) => {
  const { t } = useTranslation();
  const [vendor, setVendor] = React.useState(DEFAULT_VENDOR.value);
  const [apiKey, setApiKey] = React.useState('');
  const [baseUrl, setBaseUrl] = React.useState('');
  const [phase, setPhase] = React.useState<Phase>('edit');
  const [discovered, setDiscovered] = React.useState(0);
  const [error, setError] = React.useState<string | null>(null);
  const closeTimer = React.useRef<number | null>(null);
  // Bumped on every open/close so a test-and-add resolving after the dialog was
  // closed or reopened is dropped instead of clobbering the new state.
  const submitSeq = React.useRef(0);

  // Reset the form each time the dialog opens; clear any pending auto-close.
  React.useEffect(() => {
    submitSeq.current += 1;
    if (open) {
      setVendor(DEFAULT_VENDOR.value);
      setApiKey('');
      setBaseUrl(DEFAULT_VENDOR.base_url ?? '');
      setPhase('edit');
      setDiscovered(0);
      setError(null);
    }
    return () => {
      if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    };
  }, [open]);

  const onVendorChange = (value: string) => {
    setVendor(value);
    const meta = VENDOR_OPTIONS.find((v) => v.value === value);
    // Official vendors prefill their base URL (editable); 自定义 clears it.
    setBaseUrl(meta?.base_url ?? '');
  };

  const submit = async () => {
    if (!apiKey.trim()) return;
    const seq = submitSeq.current;
    setPhase('submitting');
    setError(null);
    try {
      const source = await modelsApi.createApiKeySource({
        kind: 'api_key',
        vendor,
        base_url: baseUrl.trim() || null,
        key: apiKey.trim(),
      });
      if (submitSeq.current !== seq) return; // dialog closed/reopened mid-request
      setDiscovered(source.models.length);
      setPhase('done');
      onAdded(source);
      closeTimer.current = window.setTimeout(onClose, 1500);
    } catch (e: any) {
      if (submitSeq.current !== seq) return;
      setError(e?.code || e?.message || 'discovery_failed');
      setPhase('error');
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-[560px] gap-5">
        <DialogHeader>
          <DialogTitle className="text-[18px] font-bold">{t('settings.models.addKey.title')}</DialogTitle>
          <DialogDescription>{t('settings.models.addKey.subtitle')}</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-2">
          <FieldLabel>{t('settings.models.addKey.vendorLabel')}</FieldLabel>
          <Select value={vendor} onChange={(e) => onVendorChange(e.target.value)} className="h-11 text-[14px]">
            {VENDOR_OPTIONS.map((v) => (
              <option key={v.value} value={v.value}>
                {t(v.labelKey)}
              </option>
            ))}
          </Select>
          <p className="text-[12px] leading-relaxed text-muted">{t('settings.models.addKey.vendorHint')}</p>
        </div>

        <div className="flex flex-col gap-2">
          <FieldLabel mono>{t('settings.models.addKey.keyLabel')}</FieldLabel>
          <IconField icon={KeyRound}>
            <Input
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder="sk-…"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              className="h-11 pl-9 font-mono text-[14px]"
              disabled={phase === 'submitting' || phase === 'done'}
            />
          </IconField>
        </div>

        <div className="flex flex-col gap-2">
          <FieldLabel mono>{t('settings.models.addKey.baseUrlLabel')}</FieldLabel>
          <IconField icon={Globe}>
            <Input
              type="text"
              autoComplete="off"
              spellCheck={false}
              placeholder="https://api.example.com/v1"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              className="h-11 pl-9 font-mono text-[14px]"
              disabled={phase === 'submitting' || phase === 'done'}
            />
          </IconField>
          <p className="text-[12px] leading-relaxed text-muted">{t('settings.models.addKey.baseUrlHint')}</p>
        </div>

        {phase === 'done' && (
          <div className="flex items-center gap-2 rounded-lg border border-mint/30 bg-mint-soft/50 px-4 py-3 text-[13px] font-medium text-mint">
            <CheckCircle2 className="size-4 shrink-0" />
            <span>{t('settings.models.addKey.discovered', { count: discovered })}</span>
          </div>
        )}
        {phase === 'error' && (
          <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/[0.08] px-4 py-3 text-[13px] text-destructive">
            <TriangleAlert className="mt-0.5 size-4 shrink-0" />
            <span>{t('settings.models.addKey.failed', { detail: error })}</span>
          </div>
        )}

        <DialogFooter className="items-center sm:justify-between">
          <span className="flex items-center gap-1.5 text-[12px] text-mint">
            <Shield className="size-3.5" />
            {t('settings.models.addKey.vaultNote')}
          </span>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="brand"
              size="sm"
              onClick={() => void submit()}
              disabled={phase === 'submitting' || phase === 'done' || !apiKey.trim()}
            >
              <Plus className="size-4" />
              {phase === 'submitting' ? t('settings.models.addKey.testing') : t('settings.models.addSource')}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};
