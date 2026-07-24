// 供给方式 card for a backend detail page (frame 02). Two radio options — 中枢模式
// Hub (推荐·默认; supply managed on the Models page, native config untouched) and
// 直连模式 Direct (legacy behavior preserved). When a native config is detected in
// Direct mode, a strip offers a one-click 导入中枢 into the migration dialog.
// Switches mode via PATCH /agents/<backend>/mode (never silent, plan §4).
import * as React from 'react';
import { Link } from 'react-router-dom';
import { ArrowDownToLine, ArrowRight, CheckCircle2, Info } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { cn } from '@/lib/utils';
import { useToast } from '@/context/ToastContext';
import { modelsApi } from './modelsApi';
import { MigrationDialog } from './MigrationDialog';
import type { AgentBackend, AgentMode, AgentSupply, MigrationItem } from './types';

const RadioDot: React.FC<{ selected: boolean }> = ({ selected }) => (
  <span
    className={cn(
      'mt-0.5 flex size-[18px] shrink-0 items-center justify-center rounded-full border-2 transition-colors',
      selected ? 'border-mint' : 'border-border-strong',
    )}
  >
    {selected && <span className="size-2 rounded-full bg-mint" />}
  </span>
);

const OptionCard: React.FC<{
  selected: boolean;
  disabled?: boolean;
  onSelect: () => void;
  title: React.ReactNode;
  description: string;
  headerRight?: React.ReactNode;
  children?: React.ReactNode;
}> = ({ selected, disabled, onSelect, title, description, headerRight, children }) => (
  <div
    role="radio"
    aria-checked={selected}
    tabIndex={disabled ? -1 : 0}
    onClick={() => !disabled && onSelect()}
    onKeyDown={(e) => {
      if (disabled) return;
      // Only act when the radio row itself is focused — Enter/Space on a nested
      // control (the Models-page link / Import button) must drive that control,
      // not switch the supply mode.
      if (e.target !== e.currentTarget) return;
      if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        onSelect();
      }
    }}
    className={cn(
      'flex cursor-pointer flex-col gap-3 rounded-xl border p-4 transition-colors',
      selected ? 'border-mint/50 bg-mint-soft/40' : 'border-border hover:border-border-strong',
      disabled && 'cursor-default opacity-70',
    )}
  >
    <div className="flex items-start gap-3">
      <RadioDot selected={selected} />
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">{title}</div>
          {headerRight}
        </div>
        <p className="text-[13px] leading-relaxed text-muted">{description}</p>
      </div>
    </div>
    {children}
  </div>
);

export const BackendSupplyModeCard: React.FC<{ backend: AgentBackend }> = ({ backend }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [agent, setAgent] = React.useState<AgentSupply | null>(null);
  const [detected, setDetected] = React.useState<MigrationItem[]>([]);
  const [switching, setSwitching] = React.useState<AgentMode | null>(null);
  const [migrateOpen, setMigrateOpen] = React.useState(false);
  const aliveRef = React.useRef(true);
  React.useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const load = React.useCallback(async () => {
    try {
      const agents = await modelsApi.listAgents();
      if (aliveRef.current) setAgent(agents.find((a) => a.backend === backend) ?? null);
    } catch {
      /* card simply stays hidden if the hub is unreachable */
    }
  }, [backend]);

  const scan = React.useCallback(async () => {
    try {
      const s = await modelsApi.scanMigration();
      if (aliveRef.current) setDetected(s.items.filter((i) => i.backend === backend));
    } catch {
      /* detect strip is best-effort */
    }
  }, [backend]);

  React.useEffect(() => {
    void load();
    void scan();
  }, [load, scan]);

  const setMode = async (mode: AgentMode) => {
    if (!agent || agent.mode === mode || switching) return;
    setSwitching(mode);
    try {
      const next = await modelsApi.setAgentMode(backend, mode);
      if (!aliveRef.current) return;
      setAgent(next);
      if (mode === 'direct') {
        showToast(t('settings.models.supplyMode.switchedDirect') as string, 'success');
      } else if (next.mode === 'hub' && next.current) {
        showToast(t('settings.models.supplyMode.switchedHub') as string, 'success');
      } else {
        // Hub selected but nothing can supply this backend yet → the launch
        // silently falls back to Direct. Tell the truth, don't claim success.
        showToast(t('settings.models.supplyMode.switchedHubNoSupply') as string, 'warning');
      }
    } catch {
      if (aliveRef.current) showToast(t('settings.models.supplyMode.switchFailed') as string, 'error');
    } finally {
      if (aliveRef.current) setSwitching(null);
    }
  };

  if (!agent) return null;

  const mode = agent.mode;
  // Only surface the import strip for configs the migration dialog can actually
  // apply — a reauth-only scan would open a dead-end dialog (reauth rows are
  // disabled and excluded from apply), so those don't count as importable.
  const importable = detected.filter((i) => i.proposed_action !== 'reauth');
  const detectItem = importable.find((i) => i.kind === 'api_key' || i.kind === 'opencode_provider') ?? importable[0] ?? null;

  return (
    <Card>
      <CardContent className="flex flex-col gap-4 p-6">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-[15px] font-semibold text-foreground">{t('settings.models.supplyMode.title')}</h2>
          <Badge variant={mode === 'hub' ? 'success' : 'secondary'} className="gap-1.5 rounded-full px-3 py-1.5 text-[12px]">
            {mode === 'hub' && <CheckCircle2 className="size-3.5" />}
            {t(mode === 'hub' ? 'settings.models.supplyMode.currentHub' : 'settings.models.supplyMode.currentDirect')}
          </Badge>
        </div>

        <OptionCard
          selected={mode === 'hub'}
          disabled={switching !== null}
          onSelect={() => void setMode('hub')}
          title={
            <>
              <span className="text-[15px] font-semibold text-foreground">{t('settings.models.supplyMode.hub.title')}</span>
              <Badge variant="success" className="rounded-md px-2 py-0.5 text-[10px] font-semibold">
                {t('settings.models.supplyMode.hub.recommended')}
              </Badge>
            </>
          }
          headerRight={
            <Link
              to="/admin/settings/models"
              onClick={(e) => e.stopPropagation()}
              className="inline-flex shrink-0 items-center gap-1 text-[13px] font-medium text-mint transition-colors hover:text-mint/80"
            >
              {t('settings.models.supplyMode.hub.openModels')}
              <ArrowRight className="size-3.5" />
            </Link>
          }
          description={t('settings.models.supplyMode.hub.description', {
            backend: t(`settings.models.backends.${backend}`, { defaultValue: backend }),
          }) as string}
        >
          {mode === 'hub' && !agent.current && (
            <div className="flex items-start gap-2 rounded-lg border border-gold/40 bg-gold/[0.08] px-3.5 py-2.5 text-[12px] leading-relaxed text-foreground">
              <Info className="mt-0.5 size-3.5 shrink-0 text-gold" />
              <span>{t('settings.models.supplyMode.hubNoSupply')}</span>
            </div>
          )}
        </OptionCard>

        <OptionCard
          selected={mode === 'direct'}
          disabled={switching !== null}
          onSelect={() => void setMode('direct')}
          title={<span className="text-[15px] font-semibold text-foreground">{t('settings.models.supplyMode.direct.title')}</span>}
          description={t('settings.models.supplyMode.direct.description', {
            backend: t(`settings.models.backends.${backend}`, { defaultValue: backend }),
          }) as string}
        >
          {mode === 'direct' && detectItem && (
            <div className="flex items-center justify-between gap-3 rounded-lg border border-gold/40 bg-gold/[0.08] px-3.5 py-2.5">
              <span className="flex min-w-0 items-center gap-2 text-[12px] leading-relaxed text-foreground">
                <Info className="size-3.5 shrink-0 text-gold" />
                <span className="truncate">
                  {t('settings.models.supplyMode.direct.detected', { detail: detectItem.masked_detail })}
                </span>
              </span>
              <Button
                variant="outline"
                size="sm"
                className="shrink-0"
                onClick={(e) => {
                  e.stopPropagation();
                  setMigrateOpen(true);
                }}
              >
                <ArrowDownToLine className="size-3.5" />
                {t('settings.models.supplyMode.direct.import')}
              </Button>
            </div>
          )}
        </OptionCard>
      </CardContent>

      <MigrationDialog
        open={migrateOpen}
        onClose={() => setMigrateOpen(false)}
        onApplied={() => {
          void load();
          void scan();
        }}
      />
    </Card>
  );
};
