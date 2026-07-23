// 迁移对话框 · 接入模型中枢 (frame 03). Non-destructive: a per-backend checklist of
// detected native configs. Actions follow migration-scan.schema (spec v1.1,
// Option 1): API keys / base URLs → import; Claude account OAuth → keep_native
// (sanctioned as-is); Codex auth.json → controlled_import behind the
// experimental flag, else keep_native. Originals are never modified or deleted.
// Scans via POST /migration/scan; applies the selection via /migration/apply.
import * as React from 'react';
import { ArrowDownToLine, Bot, KeyRound, ShieldCheck, Sparkles } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { cn } from '@/lib/utils';
import { useToast } from '@/context/ToastContext';
import { modelsApi } from './modelsApi';
import { ACCENT_ICON, ACCENT_TILE, type Accent } from './vendorMeta';
import type { AgentBackend, MigrationItem, MigrationAction } from './types';

const BACKEND_ORDER: AgentBackend[] = ['claude', 'codex', 'opencode'];

function migrationVisual(item: MigrationItem): { Icon: React.ComponentType<{ size?: number; className?: string }>; accent: Accent } {
  if (item.kind === 'oauth_native') {
    if (item.backend === 'codex') return { Icon: Bot, accent: 'gold' };
    return { Icon: Sparkles, accent: 'cyan' };
  }
  const accent: Accent = item.backend === 'opencode' ? 'cyan' : item.backend === 'codex' ? 'gold' : 'violet';
  return { Icon: KeyRound, accent };
}

const ActionBadge: React.FC<{ action: MigrationAction }> = ({ action }) => {
  const { t } = useTranslation();
  const map: Record<MigrationAction, { variant: 'success' | 'warning' | 'secondary'; key: string }> = {
    import: { variant: 'success', key: 'settings.models.migration.action.import' },
    controlled_import: { variant: 'warning', key: 'settings.models.migration.action.controlledImport' },
    keep_native: { variant: 'secondary', key: 'settings.models.migration.action.keepNative' },
    reauth: { variant: 'warning', key: 'settings.models.migration.action.reauth' },
  };
  const { variant, key } = map[action];
  return (
    <Badge variant={variant} className="shrink-0 rounded-lg px-2.5 py-1 text-[12px] font-medium">
      {t(key)}
    </Badge>
  );
};

const ItemRow: React.FC<{ item: MigrationItem; onToggle: () => void }> = ({ item, onToggle }) => {
  const { t } = useTranslation();
  const { Icon, accent } = migrationVisual(item);
  // reauth needs the interactive browser flow, so it can't be bulk-applied here.
  const selectable = item.proposed_action !== 'reauth';
  return (
    <div
      className={cn(
        'flex items-center gap-3 rounded-xl border px-3.5 py-3',
        item.selected ? 'border-mint/40 bg-mint-soft/40' : 'border-border',
      )}
    >
      <Checkbox checked={item.selected} onCheckedChange={onToggle} disabled={!selectable} label={item.masked_detail} />
      <span className={cn('flex size-9 shrink-0 items-center justify-center rounded-[10px]', ACCENT_TILE[accent])}>
        <Icon size={18} className={ACCENT_ICON[accent]} />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="truncate text-[14px] font-semibold text-foreground">{item.masked_detail}</span>
        {item.notes_key && <span className="truncate text-[12px] text-muted">{t(item.notes_key)}</span>}
      </div>
      <ActionBadge action={item.proposed_action} />
    </div>
  );
};

export const MigrationDialog: React.FC<{
  open: boolean;
  onClose: () => void;
  /** Fired after a successful apply so callers can refresh sources/agents. */
  onApplied?: (applied: number) => void;
}> = ({ open, onClose, onApplied }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [items, setItems] = React.useState<MigrationItem[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [applying, setApplying] = React.useState(false);
  const aliveRef = React.useRef(true);
  React.useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  React.useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    modelsApi
      .scanMigration()
      .then((scan) => {
        if (cancelled) return;
        setItems(scan.items);
        setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setLoading(false);
        showToast(t('settings.models.migration.scanFailed') as string, 'error');
      });
    return () => {
      cancelled = true;
    };
  }, [open, showToast, t]);

  const toggle = (id: string) =>
    setItems((prev) => prev.map((i) => (i.id === id ? { ...i, selected: !i.selected } : i)));

  const selectedCount = items.filter((i) => i.selected).length;

  const apply = async () => {
    if (applying || selectedCount === 0) return;
    setApplying(true);
    try {
      const ids = items.filter((i) => i.selected).map((i) => i.id);
      const result = await modelsApi.applyMigration(ids);
      if (!aliveRef.current) return;
      showToast(t('settings.models.migration.applied', { count: result.applied }) as string, 'success');
      onApplied?.(result.applied);
      onClose();
    } catch {
      if (aliveRef.current) showToast(t('settings.models.migration.applyFailed') as string, 'error');
    } finally {
      if (aliveRef.current) setApplying(false);
    }
  };

  const grouped = BACKEND_ORDER.map((backend) => ({
    backend,
    rows: items.filter((i) => i.backend === backend),
  })).filter((g) => g.rows.length > 0);

  return (
    <Dialog open={open} onOpenChange={(v) => !v && !applying && onClose()}>
      <DialogContent className="max-w-[640px] gap-5">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-3 text-[18px] font-bold">
            <span className="flex size-10 shrink-0 items-center justify-center rounded-[12px] bg-mint-soft">
              <ArrowDownToLine className="size-5 text-mint" />
            </span>
            {t('settings.models.migration.title')}
          </DialogTitle>
          <DialogDescription className="pl-[52px]">{t('settings.models.migration.subtitle')}</DialogDescription>
        </DialogHeader>

        {loading ? (
          <div className="py-8 text-center text-[13px] text-muted">{t('common.loading')}</div>
        ) : grouped.length === 0 ? (
          <div className="py-8 text-center text-[13px] text-muted">{t('settings.models.migration.empty')}</div>
        ) : (
          <div className="flex flex-col gap-4">
            {grouped.map((group) => (
              <div key={group.backend} className="flex flex-col gap-2">
                <span className="px-1 font-mono text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">
                  {t(`settings.models.backends.${group.backend}`, { defaultValue: group.backend })}
                </span>
                {group.rows.map((item) => (
                  <ItemRow key={item.id} item={item} onToggle={() => toggle(item.id)} />
                ))}
              </div>
            ))}
          </div>
        )}

        <p className="flex items-start gap-2 text-[12px] leading-relaxed text-mint">
          <ShieldCheck className="mt-0.5 size-4 shrink-0" />
          {t('settings.models.migration.nonDestructive')}
        </p>

        <div className="flex items-center justify-end gap-2 border-t border-border pt-4">
          <Button variant="outline" size="sm" onClick={onClose} disabled={applying}>
            {t('settings.models.migration.later')}
          </Button>
          <Button variant="brand" size="sm" onClick={() => void apply()} disabled={selectedCount === 0 || applying}>
            <ArrowDownToLine className="size-4" />
            {t('settings.models.migration.apply', { count: selectedCount })}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
};
