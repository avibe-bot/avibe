// 添加自定义模型 (frame 08). Supplements a source's supply list with a model the
// auto-discovery missed, so it appears in the OpenCode menu. Source + model id
// (+ optional display name) → a LIVE identifier preview built with the vendor-id
// rule (`custom/` fallback). Persists via POST /custom-models; also used to edit
// an existing custom entry (same upsert endpoint).
import * as React from 'react';
import { ChevronDown, Copy, Plus } from 'lucide-react';
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
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { useToast } from '@/context/ToastContext';
import { modelsApi } from '../modelsApi';
import { ACCENT_ICON, ACCENT_TILE, isCustomEndpoint, sourceVisual } from '../vendorMeta';
import type { Source } from '../types';
import { buildIdentifier } from './identifiers';

const FieldLabel: React.FC<{ mono?: boolean; children: React.ReactNode }> = ({ mono, children }) => (
  <span
    className={cn(
      'text-muted',
      mono ? 'font-mono text-[11px] font-medium uppercase tracking-wide' : 'text-[12px] font-semibold text-foreground',
    )}
  >
    {children}
  </span>
);

const useEndpointSuffix = () => {
  const { t } = useTranslation();
  return (source: Source): string => {
    if (source.kind === 'subscription') return ''; // subscriptions carry no endpoint suffix
    // Reuse the 来源-list rule (base URL differs from the vendor's official one),
    // so an official vendor edited to a relay reads as 自定义地址, not 官方地址.
    return isCustomEndpoint(source)
      ? (t('settings.models.source.customEndpoint') as string)
      : (t('settings.models.source.officialEndpoint') as string);
  };
};

const SourceSelect: React.FC<{
  sources: Source[];
  value: Source | null;
  onChange: (source: Source) => void;
  disabled?: boolean;
}> = ({ sources, value, onChange, disabled }) => {
  const [open, setOpen] = React.useState(false);
  const suffix = useEndpointSuffix();
  const renderRow = (source: Source) => {
    const { Icon, accent } = sourceVisual(source);
    const suf = suffix(source);
    return (
      <>
        <span className={cn('flex size-7 shrink-0 items-center justify-center rounded-md', ACCENT_TILE[accent])}>
          <Icon size={15} className={ACCENT_ICON[accent]} />
        </span>
        <span className="truncate text-[14px] text-foreground">
          {source.display_name}
          {suf ? <span className="text-muted"> · {suf}</span> : null}
        </span>
      </>
    );
  };
  return (
    <Popover open={open} onOpenChange={(v) => !disabled && setOpen(v)}>
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          className="flex h-11 w-full items-center gap-2.5 rounded-lg border border-border bg-background px-3 text-left transition-colors hover:border-border-strong disabled:opacity-60"
        >
          {value ? renderRow(value) : <span className="text-muted">—</span>}
          <ChevronDown className="ml-auto size-4 shrink-0 text-muted" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" sideOffset={6} className="max-h-[300px] w-[var(--radix-popover-trigger-width)] overflow-y-auto p-1.5">
        {sources.map((source) => (
          <button
            key={source.id}
            type="button"
            onClick={() => {
              onChange(source);
              setOpen(false);
            }}
            className={cn(
              'flex w-full items-center gap-2.5 rounded-md px-2 py-2 text-left hover:bg-surface-2',
              value?.id === source.id && 'bg-surface-2',
            )}
          >
            {renderRow(source)}
          </button>
        ))}
      </PopoverContent>
    </Popover>
  );
};

export const AddCustomModelDialog: React.FC<{
  open: boolean;
  sources: Source[];
  /** When set, prefill for editing an existing custom entry. */
  edit?: { sourceId: string; modelId: string; displayName: string | null } | null;
  onClose: () => void;
  onSaved: (identifier: string) => void;
}> = ({ open, sources, edit, onClose, onSaved }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [source, setSource] = React.useState<Source | null>(null);
  const [modelId, setModelId] = React.useState('');
  const [displayName, setDisplayName] = React.useState('');
  const [saving, setSaving] = React.useState(false);

  // Seed on open: edit prefill, else the first source that can carry custom
  // models (an api_key source), else the first source.
  React.useEffect(() => {
    if (!open) return;
    const editing = edit ? sources.find((s) => s.id === edit.sourceId) ?? null : null;
    const preferred = editing ?? sources.find((s) => s.kind === 'api_key') ?? sources[0] ?? null;
    setSource(preferred);
    setModelId(edit?.modelId ?? '');
    setDisplayName(edit?.displayName ?? '');
    setSaving(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const trimmedId = modelId.trim();
  const identifier = source && trimmedId ? buildIdentifier(source.vendor, trimmedId) : '';

  const copyIdentifier = () => {
    if (!identifier || !navigator.clipboard?.writeText) {
      if (identifier) showToast(t('common.copyFailed') as string, 'error');
      return;
    }
    navigator.clipboard
      .writeText(identifier)
      .then(() => showToast(t('common.copied') as string, 'success'))
      .catch(() => showToast(t('common.copyFailed') as string, 'error'));
  };

  const submit = async () => {
    if (!source || !trimmedId || saving) return;
    setSaving(true);
    try {
      await modelsApi.addCustomModel({ source_id: source.id, model_id: trimmedId, display_name: displayName.trim() || null });
      onSaved(identifier);
      onClose();
    } catch {
      showToast(t('settings.models.menus.custom.failed') as string, 'error');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !v && !saving && onClose()}>
      <DialogContent className="max-w-[560px] gap-5">
        <DialogHeader>
          <DialogTitle className="text-[18px] font-bold">{t('settings.models.menus.custom.title')}</DialogTitle>
          <DialogDescription>{t('settings.models.menus.custom.subtitle')}</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-2">
          <FieldLabel>{t('settings.models.menus.custom.sourceLabel')}</FieldLabel>
          <SourceSelect sources={sources} value={source} onChange={setSource} disabled={Boolean(edit)} />
        </div>

        <div className="flex flex-col gap-2">
          <FieldLabel mono>{t('settings.models.menus.custom.modelIdLabel')}</FieldLabel>
          <Input
            value={modelId}
            onChange={(e) => setModelId(e.target.value)}
            placeholder={t('settings.models.menus.custom.modelIdPlaceholder') as string}
            autoComplete="off"
            spellCheck={false}
            disabled={Boolean(edit)}
            className="h-11 font-mono text-[14px]"
          />
        </div>

        <div className="flex flex-col gap-2">
          <FieldLabel>{t('settings.models.menus.custom.displayNameLabel')}</FieldLabel>
          <Input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder={t('settings.models.menus.custom.displayNamePlaceholder') as string}
            autoComplete="off"
            className="h-11 text-[14px]"
          />
        </div>

        <div className="flex flex-col gap-2 rounded-xl border border-violet/30 bg-violet-soft/40 px-4 py-3.5">
          <div className="flex items-center justify-between gap-2">
            <span className="font-mono text-[11px] font-semibold uppercase tracking-wide text-violet">
              {t('settings.models.menus.custom.previewLabel')}
            </span>
            <button
              type="button"
              aria-label={t('common.copy') as string}
              onClick={copyIdentifier}
              disabled={!identifier}
              className="rounded-md p-1 text-muted transition-colors hover:text-foreground disabled:opacity-40"
            >
              <Copy className="size-4" />
            </button>
          </div>
          <span className="font-mono text-[16px] font-semibold text-foreground">
            {identifier || t('settings.models.menus.custom.previewEmpty')}
          </span>
          <p className="text-[12px] leading-relaxed text-muted">{t('settings.models.menus.custom.previewHint')}</p>
        </div>

        <DialogFooter className="sm:justify-end">
          <Button variant="outline" size="sm" onClick={onClose} disabled={saving}>
            {t('common.cancel')}
          </Button>
          <Button variant="brand" size="sm" onClick={() => void submit()} disabled={!source || !trimmedId || saving}>
            <Plus className="size-4" />
            {t('settings.models.menus.custom.add')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};
