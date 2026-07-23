// Small shared chips for the Model Hub rows — all built on the ui/Badge
// primitive (reuse ladder) with size/width overrides. Kept in one place so the
// 来源 and Agent bands stay visually consistent and the fixed-width chip
// columns (frame 01r) align across rows.
import * as React from 'react';
import { FlaskConical, Hourglass, Zap } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { useTranslation } from 'react-i18next';

import { ACCENT_DOT, type Accent } from './vendorMeta';
import type { AgentMode, SourceState } from './types';

/** Status dot used in the composite pill and the 最近切换 list. */
export const Dot: React.FC<{ accent: Accent; className?: string }> = ({ accent, className }) => (
  <span className={cn('inline-block size-1.5 shrink-0 rounded-full', ACCENT_DOT[accent], className)} aria-hidden />
);

/** 包月 / 按量 ¥ — fixed-width so the column aligns down the source list. */
export const BillingChip: React.FC<{ billing: 'monthly' | 'metered' }> = ({ billing }) => {
  const { t } = useTranslation();
  return billing === 'monthly' ? (
    <Badge variant="secondary" className="w-16 justify-center rounded-md py-1 font-medium">
      {t('settings.models.billing.monthly')}
    </Badge>
  ) : (
    <Badge variant="warning" className="w-16 justify-center rounded-md py-1 font-medium">
      {t('settings.models.billing.metered')}
    </Badge>
  );
};

/** 使用中 / 备用 / 暂不可用 / 不可用 — fixed-width aligned column. */
export const StateChip: React.FC<{ state: SourceState }> = ({ state }) => {
  const { t } = useTranslation();
  const base = 'w-[86px] justify-center rounded-full py-1';
  switch (state.status) {
    case 'active':
      return (
        <Badge variant="success" className={base}>
          <Dot accent="mint" />
          {t('settings.models.state.active')}
        </Badge>
      );
    case 'cooldown':
      return (
        <Badge variant="warning" className={base}>
          <Hourglass className="size-3" />
          {t('settings.models.state.cooldown')}
        </Badge>
      );
    case 'error':
      return (
        <Badge variant="destructive" className={base}>
          {t('settings.models.state.error')}
        </Badge>
      );
    case 'standby':
    default:
      return (
        <Badge variant="secondary" className={base}>
          {t('settings.models.state.standby')}
        </Badge>
      );
  }
};

/** 中枢 Hub / 直连 Direct — sized to sit beside the row's action button. */
export const ModeChip: React.FC<{ mode: AgentMode }> = ({ mode }) => {
  const { t } = useTranslation();
  return mode === 'hub' ? (
    <Badge variant="success" className="h-8 gap-1.5 rounded-lg px-3 text-[12px]">
      <Zap className="size-3.5" />
      {t('settings.models.mode.hub')}
    </Badge>
  ) : (
    <Badge variant="secondary" className="h-8 gap-1.5 rounded-lg px-3 text-[12px]">
      {t('settings.models.mode.direct')}
    </Badge>
  );
};

/** 菜单固定 / 菜单开放 — tiny label next to a backend name. */
export const MenuKindBadge: React.FC<{ kind: 'fixed' | 'open' }> = ({ kind }) => {
  const { t } = useTranslation();
  return (
    <Badge variant="secondary" className="rounded-md px-2 py-0.5 text-[10px] font-medium">
      {kind === 'fixed' ? t('settings.models.menuKind.fixed') : t('settings.models.menuKind.open')}
    </Badge>
  );
};

/** 实验 — marks a consent-gated hub-held subscription source. */
export const ExperimentalChip: React.FC = () => {
  const { t } = useTranslation();
  return (
    <Badge variant="warning" className="rounded-md px-2 py-0.5 text-[10px] font-medium">
      <FlaskConical className="size-3" />
      {t('settings.models.experimental')}
    </Badge>
  );
};

/**
 * Agent-card composite pill: `left ｜ ● right` (UI, not copy). Fixed-menu
 * backends show `model ｜ ● source`; the open-menu backend shows
 * `N 个模型 ｜ ● 多来源…`.
 */
export const CompositePill: React.FC<{ left: string; dot: Accent; right: string }> = ({ left, dot, right }) => (
  <div className="inline-flex items-center gap-2.5 rounded-lg border border-border bg-surface px-3 py-1.5 text-[13px]">
    <span className="font-mono font-medium text-foreground">{left}</span>
    <span className="h-3.5 w-px bg-border-strong" aria-hidden />
    <span className="inline-flex items-center gap-1.5 text-muted">
      <Dot accent={dot} />
      {right}
    </span>
  </div>
);
