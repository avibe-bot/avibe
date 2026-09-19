// One-click native credential takeover. Each backend is one custody boundary:
// unsupported native rows stay visible and block that backend as a group.
import * as React from 'react';
import { ArrowDownToLine, Bot, KeyRound, Loader2, Sparkles } from 'lucide-react';
import { useTranslation } from 'react-i18next';

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
import type { TranslationKey } from '@/i18n/types';
import { providerLabel, providerVendorId } from '../providers/providerIdentity';
import { apiFailure, modelsApi } from './modelsApi';
import { serverText } from './serverCopy';
import { VendorGlyph } from './vendorGlyph';
import { ACCENT_ICON, ACCENT_TILE, type Accent } from './vendorMeta';
import type { AgentBackend, MigrationItem } from './types';

const BACKEND_ORDER: AgentBackend[] = ['claude', 'codex', 'opencode'];

// Where a row's credential was read from, named only as far as the payload can
// honestly support. An OpenCode key may come from the config file or from
// auth.json and `backend` + `kind` cannot tell the two apart, so all three say
// "<assistant> configuration" rather than claiming a specific store.
const SOURCE_KEY: Record<AgentBackend, TranslationKey> = {
  claude: 'settings.models.migration.source.claude',
  codex: 'settings.models.migration.source.codex',
  opencode: 'settings.models.migration.source.opencode',
};

/** The provider a row belongs to, or `null` when the server sent no metadata —
 *  in which case the row falls back to `masked_detail` rather than guessing an
 *  identity from the backend that happened to hold the key. */
function migrationProvider(item: MigrationItem): string | null {
  const vendor = item.vendor?.trim() ?? '';
  const name = item.display_name?.trim() ?? '';
  if (!vendor && !name) return null;
  return providerLabel(vendor, name) || null;
}

function migrationVisual(item: MigrationItem): { Icon: React.ComponentType<{ size?: number; className?: string }>; accent: Accent } {
  if (item.kind === 'oauth_native') {
    if (item.backend === 'codex') return { Icon: Bot, accent: 'gold' };
    return { Icon: Sparkles, accent: 'cyan' };
  }
  const accent: Accent = item.backend === 'opencode' ? 'cyan' : item.backend === 'codex' ? 'gold' : 'violet';
  return { Icon: KeyRound, accent };
}

const ItemRow: React.FC<{
  item: MigrationItem;
  checked: boolean;
  onToggle: () => void;
  selectable: boolean;
}> = ({ item, checked, onToggle, selectable }) => {
  const { t } = useTranslation();
  const { Icon, accent } = migrationVisual(item);
  const provider = migrationProvider(item);
  const vendor = item.vendor?.trim();
  // The masked key and the source it came from are the two things that tell two
  // rows of the same provider apart. Without provider metadata there is nothing
  // to put above them, so the composed detail stays the title as before.
  const title = provider ?? item.masked_detail;
  const maskedKey = provider ? (item.masked_credential?.trim() || item.masked_detail) : '';
  const origin = t(SOURCE_KEY[item.backend]);
  const detail = [maskedKey, origin].filter(Boolean).join(' · ');
  return (
    <div
      className={cn(
        'flex flex-col gap-2 rounded-xl border px-3.5 py-3 sm:flex-row sm:items-center sm:gap-3',
        checked ? 'border-mint/40 bg-mint-soft/40' : 'border-border',
      )}
    >
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <Checkbox checked={checked} onCheckedChange={onToggle} disabled={!selectable} label={detail ? `${title} · ${detail}` : title} />
        <span className={cn('flex size-9 shrink-0 items-center justify-center rounded-[10px]', ACCENT_TILE[accent])}>
          {vendor
            ? <VendorGlyph vendor={providerVendorId(vendor)} className={cn('h-[14px] w-auto shrink-0 aspect-[10/7]', ACCENT_ICON[accent])} />
            : <Icon size={18} className={ACCENT_ICON[accent]} />}
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="truncate text-[14px] font-semibold text-foreground">{title}</span>
          {detail && <span className="truncate text-[12px] text-muted">{detail}</span>}
        </div>
      </div>
    </div>
  );
};

const DEFAULT_SCOPE = () => true;
const isImportable = (item: MigrationItem) => item.proposed_action === 'import';
const MIGRATION_ERROR_KEYS: Record<string, TranslationKey> = {
  migration_native_busy: 'settings.models.migration.errors.nativeBusy',
  migration_permission_needed: 'settings.models.migration.errors.permissionNeeded',
  migration_recovery_pending: 'settings.models.migration.errors.recoveryPending',
  migration_item_conflict: 'settings.models.migration.errors.itemConflict',
  migration_configuration_blocked: 'settings.models.migration.errors.configurationBlocked',
  migration_credentials_invalid: 'settings.models.migration.errors.credentialsInvalid',
};
const BLOCKED_NOTE_KEYS = new Set([
  'settings.models.migration.blocked.config',
  'settings.models.migration.blocked.environment',
  'settings.models.migration.blocked.credential',
]);
const BLOCKED_FALLBACK_KEY = 'settings.models.migration.blocked.fallback' satisfies TranslationKey;

function blockedMessage(
  t: ReturnType<typeof useTranslation>['t'],
  rows: MigrationItem[],
): string {
  const noteKey = rows
    .map((item) => item.notes_key)
    .find((key): key is string => Boolean(key && BLOCKED_NOTE_KEYS.has(key)));
  return serverText(t, noteKey, BLOCKED_FALLBACK_KEY) ?? '';
}

export const MigrationDialog: React.FC<{
  open: boolean;
  onClose: () => void;
  /** Fired after a successful apply so callers can refresh sources/agents. */
  onApplied?: (applied: number) => void;
  /** Scopes the dialog to backends represented by an entry point. It never
   *  hides other native rows from those backends: a blocked row must remain
   *  visible so the whole CLI cannot be partially migrated. */
  eligible?: (item: MigrationItem) => boolean;
}> = ({ open, onClose, onApplied, eligible }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [items, setItems] = React.useState<MigrationItem[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [applying, setApplying] = React.useState(false);
  const eligibleRef = React.useRef(eligible);
  eligibleRef.current = eligible;
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
    // Drop any rows from a prior scan so a failed rescan can't display — or
    // submit — stale migration ids.
    setItems([]);
    modelsApi
      .scanMigration()
      .then((scan) => {
        if (cancelled) return;
        const scopePredicate = eligibleRef.current ?? DEFAULT_SCOPE;
        const scopedBackends = new Set(
          scan.items.filter(scopePredicate).map((item) => item.backend),
        );
        const importable = scan.items.filter(
          (item) => scopedBackends.has(item.backend) && isImportable(item),
        );
        const blockedBackends = new Set(
          scan.items
            .filter((item) => scopedBackends.has(item.backend) && !isImportable(item))
            .map((item) => item.backend),
        );
        const backendSelection = new Map<AgentBackend, boolean>();
        for (const backend of BACKEND_ORDER) {
          const rows = importable.filter((item) => item.backend === backend);
          if (rows.length > 0) {
            backendSelection.set(
              backend,
              !blockedBackends.has(backend) && rows.every((item) => item.selected),
            );
          }
        }
        setItems(scan.items.map((item) => (
          scopedBackends.has(item.backend) && isImportable(item)
            ? { ...item, selected: backendSelection.get(item.backend) ?? false }
            : item
        )));
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

  const scopePredicate = eligible ?? DEFAULT_SCOPE;
  const scopedBackends = new Set(
    items.filter(scopePredicate).map((item) => item.backend),
  );
  const candidates = items.filter((item) => scopedBackends.has(item.backend));
  const grouped = BACKEND_ORDER.map((backend) => {
    const rows = candidates.filter((item) => item.backend === backend);
    const importRows = rows.filter(isImportable);
    const blockedRows = rows.filter((item) => !isImportable(item));
    return {
      backend,
      rows,
      importRows,
      blockedRows,
      blocked: blockedRows.length > 0,
    };
  }).filter((group) => group.rows.length > 0);
  const selectedBackends = new Set(
    grouped
      .filter((group) => (
        !group.blocked
        && group.importRows.length > 0
        && group.importRows.every((item) => item.selected)
      ))
      .map((group) => group.backend)
  );
  const toggle = (backend: AgentBackend) =>
    setItems((prev) => {
      const group = grouped.find((item) => item.backend === backend);
      if (!group || group.blocked || group.importRows.length === 0) return prev;
      const selected = group.importRows.every((item) => item.selected);
      return prev.map((item) => (
        item.backend === backend && isImportable(item)
          ? { ...item, selected: !selected }
          : item
      ));
    });
  const appliable = items.filter(
    (item) => selectedBackends.has(item.backend) && isImportable(item),
  );
  const selectedCount = appliable.length;

  const apply = async () => {
    if (applying || selectedCount === 0) return;
    setApplying(true);
    try {
      const ids = appliable.map((i) => i.id);
      const result = await modelsApi.applyMigration(ids);
      if (!aliveRef.current) return;
      showToast(t('settings.models.migration.applied', { count: result.applied }) as string, 'success');
      onApplied?.(result.applied);
      onClose();
    } catch (error) {
      if (aliveRef.current) {
        const code = apiFailure(error)?.code;
        const key =
          MIGRATION_ERROR_KEYS[code ?? ''] ??
          'settings.models.migration.applyFailed';
        showToast(t(key) as string, 'error');
      }
    } finally {
      if (aliveRef.current) setApplying(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !v && !applying && onClose()}>
      <DialogContent className="max-w-[640px] gap-5">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-3 text-[18px] font-bold">
            <span className="flex size-10 shrink-0 items-center justify-center rounded-[12px] bg-mint-soft">
              <ArrowDownToLine className="model-hub-ink-mint size-5" />
            </span>
            {t('settings.models.migration.title')}
          </DialogTitle>
          <DialogDescription className="sm:pl-[52px]">{t('settings.models.migration.subtitle')}</DialogDescription>
        </DialogHeader>

        {loading ? (
          <div className="py-8 text-center text-[13px] text-muted">{t('common.loading')}</div>
        ) : grouped.length === 0 ? (
          <div className="py-8 text-center text-[13px] text-muted">{t('settings.models.migration.empty')}</div>
        ) : (
          <div className="flex flex-col gap-4">
            {grouped.map((group) => (
              <div key={group.backend} className="flex flex-col gap-2">
                <span className="px-1 font-mono text-[11px] font-semibold uppercase tracking-normal text-muted">
                  {t(`settings.models.backends.${group.backend}`, { defaultValue: group.backend })}
                </span>
                {group.blocked && (
                  <div
                    role="status"
                    className="rounded-lg border border-gold/40 bg-gold/[0.08] px-3 py-2 text-[12px] leading-relaxed text-foreground"
                  >
                    {blockedMessage(t, group.blockedRows)}
                  </div>
                )}
                {group.rows.map((item) => (
                  <ItemRow
                    key={item.id}
                    item={item}
                    checked={!group.blocked && selectedBackends.has(group.backend)}
                    selectable={!group.blocked && isImportable(item)}
                    onToggle={() => toggle(group.backend)}
                  />
                ))}
              </div>
            ))}
          </div>
        )}

        <div className="flex items-center justify-end gap-2 border-t border-border pt-4">
          <Button variant="outline" size="sm" className="h-10 sm:h-9" onClick={onClose} disabled={applying}>
            {t('settings.models.migration.later')}
          </Button>
          <Button variant="brand" size="sm" className="h-10 sm:h-9" onClick={() => void apply()} disabled={selectedCount === 0 || applying}>
            {applying ? <Loader2 className="size-4 animate-spin" /> : <ArrowDownToLine className="size-4" />}
            {t(applying ? 'settings.models.migration.applying' : 'settings.models.migration.apply')}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
};
