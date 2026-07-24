// 模型菜单 · Claude Code / Codex — the fixed-menu mapping drawer (frame 04).
// Left column: the backend's built-in model ids (mono, not editable). Right
// column: the supply each id resolves to — 跟随原生 · 按优先级供给 by default, or
// an override to another model (violet, with a per-row reset and a persistent
// capability warning). Per-agent scope; commits via PUT .../mappings on 完成.
import * as React from 'react';
import { ArrowRight, CheckCircle2, ChevronDown, Info, RotateCcw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { useToast } from '@/context/ToastContext';
import { modelsApi } from '../modelsApi';
import { backendVisual } from '../vendorMeta';
import type { AgentBackend, AgentMapping, AgentSupply, Source } from '../types';
import { MenuDrawer } from './MenuDrawer';
import { buildTargetModels, isSourceEligible, type TargetModel } from './identifiers';
import { SupplyDots } from './supplyBits';
import { useCompactSourceLabel } from './sourceLabel';

// Fixed-menu backends expose their built-in model catalog via agent.builtin_models
// (real ids from vibe/backend_model_catalog.py, agent-supply v1.2). The contract
// stores only OVERRIDES in agent.mappings (absent entry = 跟随原生), so the row list
// comes from this server-populated catalog — the UI never hardcodes it.
const resolveBuiltinIds = (agent: AgentSupply): string[] => agent.builtin_models ?? [];

const seedDraft = (agent: AgentSupply): AgentMapping[] => {
  const byId = new Map((agent.mappings ?? []).map((m) => [m.builtin_id, m]));
  const ids = resolveBuiltinIds(agent);
  // Preserve any stored builtin id we don't know about, appended after the
  // canonical list, so a backend that adds ids stays visible.
  const extra = (agent.mappings ?? []).map((m) => m.builtin_id).filter((id) => !ids.includes(id));
  return [...ids, ...extra].map(
    (builtin_id) => byId.get(builtin_id) ?? { builtin_id, target_model_id: '', enabled: false },
  );
};

const sameDraft = (a: AgentMapping[], b: AgentMapping[]): boolean =>
  a.length === b.length &&
  a.every((m, i) => m.builtin_id === b[i].builtin_id && m.enabled === b[i].enabled && m.target_model_id === b[i].target_model_id);

const BuiltinChip: React.FC<{ id: string }> = ({ id }) => (
  <span className="shrink-0 whitespace-nowrap rounded-lg border border-border bg-surface-2 px-3 py-2 font-mono text-[13px] font-medium text-foreground">
    {id}
  </span>
);

const SupplySelector: React.FC<{
  mapping: AgentMapping;
  targets: TargetModel[];
  onFollowNative: () => void;
  onOverride: (targetId: string) => void;
}> = ({ mapping, targets, onFollowNative, onOverride }) => {
  const { t } = useTranslation();
  const compactLabel = useCompactSourceLabel();
  const [open, setOpen] = React.useState(false);
  const override = mapping.enabled && Boolean(mapping.target_model_id);
  const target = targets.find((x) => x.id === mapping.target_model_id);
  const candidateLabel = target
    ? (t('settings.models.menus.mapping.candidates', {
        sources: target.sources.map(compactLabel).join(' / '),
      }) as string)
    : (t('settings.models.menus.mapping.candidatesUnavailable') as string);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            'flex h-11 w-full items-center gap-2.5 rounded-lg border px-3.5 text-left text-[13px] transition-colors',
            override
              ? 'border-violet/40 bg-violet-soft/60 hover:border-violet/60'
              : 'border-border bg-background hover:border-border-strong',
          )}
        >
          {override ? (
            <>
              <span className="shrink-0 font-mono font-semibold text-violet">{mapping.target_model_id}</span>
              {target && <SupplyDots accents={target.accents} className="flex shrink-0 items-center gap-1" />}
              <span className="truncate text-muted">{candidateLabel}</span>
            </>
          ) : (
            <>
              <CheckCircle2 className="size-4 shrink-0 text-mint" />
              <span className="truncate text-foreground">{t('settings.models.menus.mapping.followNative')}</span>
            </>
          )}
          <ChevronDown className="ml-auto size-4 shrink-0 text-muted" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        sideOffset={6}
        className="max-h-[320px] w-[var(--radix-popover-trigger-width)] overflow-y-auto p-1.5"
      >
        <button
          type="button"
          onClick={() => {
            onFollowNative();
            setOpen(false);
          }}
          className="flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-[13px] hover:bg-surface-2"
        >
          <CheckCircle2 className={cn('size-4 shrink-0', override ? 'text-muted/40' : 'text-mint')} />
          <span className="text-foreground">{t('settings.models.menus.mapping.followNative')}</span>
        </button>
        <div className="my-1 h-px bg-border" />
        {targets.map((tg) => {
          const active = override && tg.id === mapping.target_model_id;
          return (
            <button
              key={tg.id}
              type="button"
              onClick={() => {
                onOverride(tg.id);
                setOpen(false);
              }}
              className={cn(
                'flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left hover:bg-surface-2',
                active && 'bg-violet-soft/60',
              )}
            >
              <span className="font-mono text-[13px] font-medium text-foreground">{tg.id}</span>
              {tg.displayName && <span className="truncate text-[12px] text-muted">{tg.displayName}</span>}
              <SupplyDots accents={tg.accents} className="ml-auto flex shrink-0 items-center gap-1" />
            </button>
          );
        })}
      </PopoverContent>
    </Popover>
  );
};

export const MappingDrawer: React.FC<{
  open: boolean;
  backend: Extract<AgentBackend, 'claude' | 'codex'>;
  agent: AgentSupply;
  sources: Source[];
  onClose: () => void;
  onSaved: () => void;
}> = ({ open, backend, agent, sources, onClose, onSaved }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { Icon, accent } = backendVisual(backend);
  // Override targets follow the backend eligibility predicate (isSourceEligible):
  // hub-supplied API-key sources PLUS this backend's own native subscription.
  // Anything else is rejected by the live API with `mapping_target_unavailable`.
  const targets = React.useMemo(
    () => buildTargetModels(sources.filter((s) => isSourceEligible(s, backend))),
    [sources, backend],
  );

  const [draft, setDraft] = React.useState<AgentMapping[]>(() => seedDraft(agent));
  const initialRef = React.useRef<AgentMapping[]>(draft);
  const [saving, setSaving] = React.useState(false);

  // Reseed only on open so an in-flight edit isn't clobbered by a background
  // refresh of the agent prop.
  React.useEffect(() => {
    if (!open) return;
    const seed = seedDraft(agent);
    setDraft(seed);
    initialRef.current = seed;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const backendName = t(`settings.models.backends.${backend}`, { defaultValue: backend }) as string;

  const setMapping = (builtinId: string, next: Partial<AgentMapping>) =>
    setDraft((prev) => prev.map((m) => (m.builtin_id === builtinId ? { ...m, ...next } : m)));

  const resetAll = () => setDraft((prev) => prev.map((m) => ({ ...m, enabled: false, target_model_id: '' })));

  const commitAndClose = async () => {
    if (saving) return;
    if (sameDraft(draft, initialRef.current)) {
      onClose();
      return;
    }
    setSaving(true);
    try {
      // Contract: absent entry = 跟随原生 (identity). Send only enabled overrides
      // — disabled rows carry an empty target_model_id that from_payload rejects.
      // Self-heal: drop an override whose target is no longer available (its
      // supplier source was deleted); it reverts to 跟随原生 instead of being
      // resubmitted and rejected as mapping_target_unavailable.
      const targetIds = new Set(targets.map((tg) => tg.id));
      await modelsApi.putMappings(backend, draft.filter((m) => m.enabled && targetIds.has(m.target_model_id)));
      onSaved();
      onClose();
    } catch {
      showToast(t('settings.models.menus.saveFailed') as string, 'error');
    } finally {
      setSaving(false);
    }
  };

  const anyOverride = draft.some((m) => m.enabled);

  return (
    <MenuDrawer
      open={open}
      onClose={() => void commitAndClose()}
      Icon={Icon}
      accent={accent}
      title={t('settings.models.menus.mapping.title', { backend: backendName }) as string}
      subtitle={t('settings.models.menus.mapping.subtitle', { backend: backendName }) as string}
      footer={
        <>
          <Button variant="outline" size="sm" onClick={resetAll} disabled={!anyOverride || saving}>
            <RotateCcw className="size-3.5" />
            {t('settings.models.menus.resetAll')}
          </Button>
          <Button variant="brand" size="sm" onClick={() => void commitAndClose()} disabled={saving}>
            {t('settings.models.menus.done')}
          </Button>
        </>
      }
    >
      <div className="flex items-center gap-3 px-1 pb-2.5">
        <span className="w-[172px] shrink-0 font-mono text-[11px] font-medium uppercase tracking-wide text-muted">
          {t('settings.models.menus.mapping.builtinCol')}
        </span>
        <span className="font-mono text-[11px] font-medium uppercase tracking-wide text-muted">
          {t('settings.models.menus.mapping.supplyCol')}
        </span>
      </div>

      <div className="flex flex-col gap-2.5">
        {draft.map((mapping) => {
          const override = mapping.enabled && Boolean(mapping.target_model_id);
          return (
            <div key={mapping.builtin_id} className="flex items-start gap-2.5">
              <div
                className={cn(
                  'flex flex-1 flex-col gap-2 rounded-xl border p-2.5',
                  override ? 'border-violet/40 bg-violet-soft/40' : 'border-border',
                )}
              >
                <div className="flex items-center gap-2.5">
                  <div className="w-[172px] shrink-0">
                    <BuiltinChip id={mapping.builtin_id} />
                  </div>
                  <ArrowRight className={cn('size-4 shrink-0', override ? 'text-violet' : 'text-muted/60')} />
                  <div className="min-w-0 flex-1">
                    <SupplySelector
                      mapping={mapping}
                      targets={targets}
                      onFollowNative={() => setMapping(mapping.builtin_id, { enabled: false, target_model_id: '' })}
                      onOverride={(targetId) => setMapping(mapping.builtin_id, { enabled: true, target_model_id: targetId })}
                    />
                  </div>
                </div>
                {override && (
                  <p className="flex items-center gap-1.5 pl-[208px] text-[12px] text-gold">
                    <Info className="size-3.5 shrink-0" />
                    {t('settings.models.menus.mapping.warning')}
                  </p>
                )}
              </div>
              <button
                type="button"
                aria-label={t('settings.models.menus.mapping.resetRow') as string}
                onClick={() => setMapping(mapping.builtin_id, { enabled: false, target_model_id: '' })}
                disabled={!override}
                className={cn(
                  'mt-0.5 flex size-11 shrink-0 items-center justify-center rounded-lg border border-border text-muted transition-colors',
                  override ? 'hover:border-border-strong hover:text-foreground' : 'opacity-0',
                )}
              >
                <RotateCcw className="size-4" />
              </button>
            </div>
          );
        })}
      </div>

      <p className="mt-4 flex items-start gap-1.5 px-1 text-[12px] leading-relaxed text-muted">
        <Info className="mt-0.5 size-3.5 shrink-0" />
        {t('settings.models.menus.mapping.footnote', { backend: backendName })}
      </p>
    </MenuDrawer>
  );
};
