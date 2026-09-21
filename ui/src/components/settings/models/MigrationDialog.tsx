// One-click native credential takeover. Each backend is one custody boundary;
// shared persisted assignments link those boundaries into one consent group.
import * as React from 'react';
import type { TFunction } from 'i18next';
import { ArrowDownToLine, Bot, CheckCircle2, KeyRound, Loader2, Sparkles } from 'lucide-react';
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
import {
  BACKEND_ORDER,
  appliableItems,
  groupMigrationCandidates,
  groupSelectable,
  isImportable,
  type MigrationSelection,
} from './migrationGrouping';
import { apiFailure, modelsApi } from './modelsApi';
import { serverText } from './serverCopy';
import { VendorGlyph } from './vendorGlyph';
import { ACCENT_ICON, ACCENT_TILE, type Accent } from './vendorMeta';
import type { AgentBackend, MigrationItem } from './types';

// Older payloads cannot identify the exact file or store. Name only the
// backend's configuration when the server supplies no file locators.
const SOURCE_KEY = {
  claude: 'settings.models.migration.source.claude',
  codex: 'settings.models.migration.source.codex',
  opencode: 'settings.models.migration.source.opencode',
} as const satisfies Record<AgentBackend, TranslationKey>;

const sourcePaths = (item: MigrationItem): string[] => [...new Set(item.source_paths ?? [])];

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
  const paths = sourcePaths(item);
  const origin = paths.length === 0 ? t(SOURCE_KEY[item.backend]) : '';
  const detail = [maskedKey, origin].filter(Boolean).join(' · ');
  const label = [title, detail, ...paths].filter(Boolean).join(' · ');
  return (
    <div
      className={cn(
        'flex flex-col gap-2 rounded-xl border px-3.5 py-3 sm:flex-row sm:items-center sm:gap-3',
        checked ? 'border-mint/40 bg-mint-soft/40' : 'border-border',
      )}
    >
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <Checkbox checked={checked} onCheckedChange={onToggle} disabled={!selectable} label={label} />
        <span className={cn('flex size-9 shrink-0 items-center justify-center rounded-[10px]', ACCENT_TILE[accent])}>
          {vendor
            ? <VendorGlyph vendor={providerVendorId(vendor)} className={cn('h-[14px] w-auto shrink-0 aspect-[10/7]', ACCENT_ICON[accent])} />
            : <Icon size={18} className={ACCENT_ICON[accent]} />}
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="truncate text-[14px] font-semibold text-foreground">{title}</span>
          {detail && <span className="truncate text-[12px] text-muted">{detail}</span>}
          {paths.map((path) => (
            <span key={path} className="break-all font-mono text-[12px] text-muted">{path}</span>
          ))}
        </div>
      </div>
    </div>
  );
};

const DEFAULT_SCOPE = () => true;

const MIGRATION_ERROR_KEYS: Record<string, TranslationKey> = {
  migration_native_busy: 'settings.models.migration.errors.nativeBusy',
  migration_permission_needed: 'settings.models.migration.errors.permissionNeeded',
  migration_recovery_pending: 'settings.models.migration.errors.recoveryPending',
  migration_item_conflict: 'settings.models.migration.errors.itemConflict',
  migration_configuration_blocked: 'settings.models.migration.errors.configurationBlocked',
  migration_credentials_invalid: 'settings.models.migration.errors.credentialsInvalid',
  migration_reauthorization_required: 'settings.models.migration.errors.reauthorizationRequired',
};
const BLOCKED_NOTE_KEYS = new Set<string>([
  'settings.models.migration.blocked.config',
  'settings.models.migration.blocked.environment',
  'settings.models.migration.blocked.credential',
  'settings.models.migration.blocked.dynamic_shell',
  'settings.models.migration.blocked.ambiguous_shell',
  'settings.models.migration.blocked.unreadable',
  'settings.models.migration.blocked.reference',
  'settings.models.migration.blocked.helper',
  'settings.models.migration.blocked.token',
  'settings.models.migration.blocked.headers',
  'settings.models.migration.blocked.transport',
] satisfies TranslationKey[]);
const BLOCKED_FALLBACK_KEY = 'settings.models.migration.blocked.fallback' satisfies TranslationKey;
/**
 * Why a row this entry point could otherwise have taken is nonetheless blocked.
 *
 * The server's own reasons all say the credential cannot be imported at all, which
 * is untrue of this one: it is importable, just not from here. Unreachable in
 * Settings, whose scope takes over everything the scan proposes.
 */
const OUT_OF_SCOPE_KEY = 'onboarding.import.outOfScope' satisfies TranslationKey;

function blockedMessages(
  t: TFunction,
  rows: MigrationItem[],
): { key: string; source: string; message: string }[] {
  const messages = new Map<string, { key: string; source: string; message: string }>();
  for (const item of rows) {
    const outOfScope = isImportable(item);
    const noteKey = item.notes_key && BLOCKED_NOTE_KEYS.has(item.notes_key) ? item.notes_key : undefined;
    const reason = outOfScope ? OUT_OF_SCOPE_KEY : item.notes_key ?? BLOCKED_FALLBACK_KEY;
    const message = outOfScope ? t(OUT_OF_SCOPE_KEY) : serverText(t, noteKey, BLOCKED_FALLBACK_KEY) ?? '';
    const paths = sourcePaths(item);
    // Older scans cannot identify a file. Keep their backend-level locator,
    // with each distinct reason, without duplicating an account or key title.
    const sources = paths.length > 0 ? paths : [t(SOURCE_KEY[item.backend])];
    for (const source of sources) {
      const key = JSON.stringify([source, reason]);
      messages.set(key, { key, source, message });
    }
  }
  return [...messages.values()];
}

/**
 * What setup's take-over is doing, and what it landed.
 *
 * It reports rather than promises: the running line names the batch that is in
 * flight, and the finished line names what the refreshed scan proves was taken
 * over plus whatever is still there to come back to. Neither claims anything
 * about rollback, copies or untouched connections — the apply owns those facts,
 * and this screen is not where they are decided.
 */
const MigrationReport: React.FC<{
  phase: SetupPhase;
  completed: number;
  remaining: number;
  onContinue: () => void;
  onDone: () => void;
}> = ({ phase, completed, remaining, onContinue, onDone }) => {
  const { t } = useTranslation();
  const running = phase.kind === 'applying';
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col items-center gap-3.5 py-6 text-center" role="status" aria-live="polite">
        <span className="flex size-8 shrink-0 items-center justify-center">
          {running
            ? <Loader2 className="model-hub-ink-mint size-8 animate-spin" />
            : <CheckCircle2 className="model-hub-ink-mint size-8" />}
        </span>
        <span className="text-[17px] font-semibold text-foreground">
          {running
            ? t('onboarding.import.progress', { count: phase.count })
            : t('onboarding.import.done', { count: completed })}
        </span>
        <span className="text-[12px] leading-relaxed text-muted">
          {t(running ? 'onboarding.import.progressDetail' : 'onboarding.import.doneDetail')}
        </span>
        {!running && remaining > 0 && (
          <span className="text-[12px] leading-relaxed text-muted">
            {t('onboarding.import.remaining', { count: remaining })}
          </span>
        )}
      </div>
      {!running && (
        <div className="flex items-center justify-end gap-2 border-t border-border pt-4">
          {remaining > 0 && (
            <Button variant="outline" size="sm" className="h-10 sm:h-9" onClick={onContinue}>
              {t('onboarding.import.remainingAction')}
            </Button>
          )}
          <Button variant="brand" size="sm" className="h-10 sm:h-9" onClick={onDone}>
            {t('onboarding.import.done')}
          </Button>
        </div>
      )}
    </div>
  );
};

export type MigrationDialogScope = 'settings' | 'setup';

/** The chrome each entry point wears. The rows, the consent groups and the
 *  apply call underneath are the same surface in both: only the words around
 *  them, and setup's progress/done phases, differ. */
const SCOPE_COPY = {
  settings: {
    title: 'settings.models.migration.title',
    description: 'settings.models.migration.subtitle',
    cancel: 'settings.models.migration.later',
    confirm: 'settings.models.migration.apply',
    confirming: 'settings.models.migration.applying',
  },
  setup: {
    title: 'onboarding.import.title',
    description: 'onboarding.import.description',
    cancel: 'onboarding.import.notNow',
    confirm: 'onboarding.import.confirm',
    confirming: 'settings.models.migration.applying',
  },
} as const satisfies Record<MigrationDialogScope, Record<string, TranslationKey>>;

/** Where setup's dialog is in its own take-over: choosing, running, or reporting
 *  what landed. Settings has no phases — it closes on success, as it always
 *  has. */
type SetupPhase = { kind: 'select' } | { kind: 'applying'; count: number } | { kind: 'done' };

export const MigrationDialog: React.FC<{
  open: boolean;
  onClose: () => void;
  /** Fired after a successful apply so callers can refresh sources/agents. */
  onApplied?: (applied: number) => void;
  /** Scopes the entry point, then includes every native row and required
   *  backend so shared-file custody and blockers remain explicit. */
  eligible?: (item: MigrationItem) => boolean;
  /** Which of the rows in view this entry point may actually take over. One in view
   *  that fails it blocks its group — the server migrates a backend whole — so it
   *  stays visible, with its blocker, rather than riding along in someone else's
   *  batch. Omitted means every row the scan proposes importing, which is Settings. */
  takeable?: (item: MigrationItem) => boolean;
  /** Which entry point this is. `settings` is the shipped surface and renders
   *  exactly as it always has. */
  scope?: MigrationDialogScope;
  /** Passing `value` makes the dialog controlled: the caller owns the scan and
   *  the selection, and the dialog neither scans nor remembers. Setup does this
   *  because its screen already holds both (C2). */
  value?: MigrationSelection;
  onChange?: (next: MigrationSelection) => void;
}> = ({ open, onClose, onApplied, eligible, takeable, scope = 'settings', value, onChange }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const copy = SCOPE_COPY[scope];
  const controlled = value !== undefined;

  const [ownItems, setOwnItems] = React.useState<MigrationItem[]>([]);
  const [ownLoading, setOwnLoading] = React.useState(true);
  const [applying, setApplying] = React.useState(false);
  const [phase, setPhase] = React.useState<SetupPhase>({ kind: 'select' });
  /** Everything this dialog session has taken over, across re-entries. */
  const [completed, setCompleted] = React.useState(0);
  const aliveRef = React.useRef(true);
  React.useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  React.useEffect(() => {
    if (!open) return;
    setPhase({ kind: 'select' });
    setCompleted(0);
  }, [open]);

  React.useEffect(() => {
    if (!open || controlled) return;
    let cancelled = false;
    setOwnLoading(true);
    // Drop any rows from a prior scan so a failed rescan can't display — or
    // submit — stale migration ids.
    setOwnItems([]);
    modelsApi
      .scanMigration()
      .then((scan) => {
        if (cancelled) return;
        setOwnItems(scan.items);
        setOwnLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setOwnLoading(false);
        showToast(t('settings.models.migration.scanFailed') as string, 'error');
      });
    return () => {
      cancelled = true;
    };
  }, [open, controlled, showToast, t]);

  const items = controlled ? (value.scan?.items ?? []) : ownItems;
  const loading = controlled ? value.scan === null : ownLoading;
  const scopePredicate = eligible ?? DEFAULT_SCOPE;
  const grouped = groupMigrationCandidates(items, scopePredicate, takeable);
  // Uncontrolled selection lives on the rows, as it always has. A controlled
  // caller names backends instead, and a backend it named that this scope
  // cannot consent to selects nothing.
  const selectedBackends = new Set(
    grouped
      .filter((group) => (
        groupSelectable(group)
        && (controlled
          ? value.selectedBackends.includes(group.backend)
          : group.linkedImportRows.every((item) => item.selected))
      ))
      .map((group) => group.backend),
  );
  const toggle = (backend: AgentBackend) => {
    const group = grouped.find((item) => item.backend === backend);
    if (!group || !groupSelectable(group)) return;
    if (controlled) {
      // Toggling any member selects or deselects the whole linked group.
      const next = new Set(value.selectedBackends);
      const on = selectedBackends.has(backend);
      for (const linked of group.required) {
        if (on) next.delete(linked);
        else next.add(linked);
      }
      onChange?.({ scan: value.scan, selectedBackends: [...next] });
      return;
    }
    setOwnItems((prev) => {
      const selected = group.linkedImportRows.every((item) => item.selected);
      return prev.map((item) => (
        group.required.has(item.backend) && isImportable(item)
          ? { ...item, selected: !selected }
          : item
      ));
    });
  };
  const appliable = appliableItems(items, selectedBackends);
  const selectedCount = appliable.length;
  /** What a re-entry would still find, once a batch has landed. */
  const remaining = grouped.filter(groupSelectable)
    .reduce((total, group) => total + group.importRows.length, 0);

  const apply = async () => {
    if (applying || selectedCount === 0) return;
    setApplying(true);
    if (scope === 'setup') setPhase({ kind: 'applying', count: selectedCount });
    try {
      const ids = appliable.map((i) => i.id);
      const result = await modelsApi.applyMigration(ids);
      if (!aliveRef.current) return;
      showToast(t('settings.models.migration.applied', { count: result.applied }) as string, 'success');
      setCompleted((prior) => prior + result.applied);
      onApplied?.(result.applied);
      // Setup reports what landed and what is left rather than vanishing; the
      // caller's refreshed scan is what the remainder is counted from.
      if (scope === 'setup') setPhase({ kind: 'done' });
      else onClose();
    } catch (error) {
      if (aliveRef.current) {
        // The selection is left exactly as it was: a failed batch is retried
        // from the same rows, not rebuilt.
        if (scope === 'setup') setPhase({ kind: 'select' });
        const code = apiFailure(error)?.code;
        const key =
          MIGRATION_ERROR_KEYS[code ?? ''] ??
          'settings.models.migration.applyFailed';
        showToast(t(key) as string, 'error');
        if (code === 'migration_credentials_invalid') {
          onApplied?.(0);
          onClose();
        }
      }
    } finally {
      if (aliveRef.current) setApplying(false);
    }
  };

  const reporting = scope === 'setup' && phase.kind !== 'select';

  return (
    <Dialog open={open} onOpenChange={(v) => !v && !applying && onClose()}>
      <DialogContent className="max-w-[640px] gap-5">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-3 text-[18px] font-bold">
            <span className="flex size-10 shrink-0 items-center justify-center rounded-[12px] bg-mint-soft">
              <ArrowDownToLine className="model-hub-ink-mint size-5" />
            </span>
            {t(copy.title)}
          </DialogTitle>
          <DialogDescription className="sm:pl-[52px]">{t(copy.description)}</DialogDescription>
        </DialogHeader>

        {reporting ? (
          <MigrationReport
            phase={phase}
            completed={completed}
            remaining={remaining}
            onContinue={() => setPhase({ kind: 'select' })}
            onDone={onClose}
          />
        ) : loading ? (
          <div className="py-8 text-center text-[13px] text-muted">{t('common.loading')}</div>
        ) : grouped.length === 0 ? (
          <div className="py-8 text-center text-[13px] text-muted">{t('settings.models.migration.empty')}</div>
        ) : (
          <div className="flex flex-col gap-4">
            {scope === 'setup' && (
              <p className="text-[12px] leading-relaxed text-muted">{t('onboarding.import.scopeNote')}</p>
            )}
            {grouped.map((group) => (
              <div key={group.backend} className="flex flex-col gap-2">
                <span className="px-1 font-mono text-[11px] font-semibold uppercase tracking-normal text-muted">
                  {t(`settings.models.backends.${group.backend}`, { defaultValue: group.backend })}
                </span>
                {group.required.size > 1 && (
                  <p className="px-1 text-[12px] leading-relaxed text-muted">
                    {t('settings.models.migration.sharedFiles', {
                      backends: BACKEND_ORDER
                        .filter((backend) => group.required.has(backend))
                        .map((backend) => t(`settings.models.backends.${backend}`))
                        .join(', '),
                    })}
                  </p>
                )}
                {group.blocked && (
                  <div
                    role="status"
                    className="rounded-lg border border-gold/40 bg-gold/[0.08] px-3 py-2 text-[12px] leading-relaxed text-foreground"
                  >
                    <ul className="flex flex-col gap-2">
                      {blockedMessages(t, group.blockedRows).map(({ key, source, message }) => (
                        <li key={key} className="flex flex-col gap-0.5">
                          <span className="break-all font-mono">{source}</span>
                          <span>{message}</span>
                        </li>
                      ))}
                    </ul>
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

        {!reporting && (
          <div className="flex items-center justify-end gap-2 border-t border-border pt-4">
            <Button variant="outline" size="sm" className="h-10 sm:h-9" onClick={onClose} disabled={applying}>
              {t(copy.cancel)}
            </Button>
            <Button variant="brand" size="sm" className="h-10 sm:h-9" onClick={() => void apply()} disabled={selectedCount === 0 || applying}>
              {applying ? <Loader2 className="size-4 animate-spin" /> : <ArrowDownToLine className="size-4" />}
              {t(applying ? copy.confirming : copy.confirm)}
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
};
