// The Agent band (frame 01r): one row per backend — icon + name + menu-kind
// badge, the current supply as a composite pill (hub only), the supply-mode
// chip, and the row action (模型菜单 for hub / 接入中枢 for direct). 模型菜单
// links into L5's drawers; until L5 lands (MODEL_MENUS_ENABLED) it explains
// itself rather than opening a missing surface.
import * as React from 'react';
import { Link } from 'react-router-dom';
import { ArrowDownToLine, ArrowRight, ChevronRight, TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { useToast } from '@/context/ToastContext';
import { CompositePill, ModeChip } from './chips';
import { ACCENT_ICON, ACCENT_TILE, backendVisual, sourceAccent } from './vendorMeta';
import { friendlyModelName } from './format';
import { MODEL_MENUS_ENABLED } from './featureFlags';
import type { AgentSupply, Source } from './types';

const AgentRow: React.FC<{
  agent: AgentSupply;
  sources: Source[];
  onConnectHub: (agent: AgentSupply) => void;
  onOpenMenu?: (agent: AgentSupply) => void;
  connecting: boolean;
}> = ({ agent, sources, onConnectHub, onOpenMenu, connecting }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { Icon, accent } = backendVisual(agent.backend);

  const openMenu = () => {
    // L5's mapping / menu drawers are gated by MODEL_MENUS_ENABLED; until it
    // flips, keep the buttons visible (pixel fidelity) but explain themselves.
    if (!MODEL_MENUS_ENABLED || !onOpenMenu) {
      showToast(t('settings.models.agents.menuComingSoon') as string, 'warning');
      return;
    }
    onOpenMenu(agent);
  };

  // Composite pill content. Fixed-menu → model ｜ source; open-menu → count ｜
  // multi-source (+ custom count).
  let pill: React.ReactNode = null;
  if (agent.mode === 'hub' && agent.current) {
    if (agent.menu_kind === 'open' && agent.menu) {
      const customCount = sources.reduce(
        (n, s) => n + s.models.filter((m) => m.provenance === 'manual').length,
        0,
      );
      const right =
        customCount > 0
          ? (t('settings.models.agents.multiSourceCustom', { count: customCount }) as string)
          : (t('settings.models.agents.multiSource') as string);
      pill = <CompositePill left={t('settings.models.agents.modelCount', { count: agent.menu.checked.length }) as string} dot={accent} right={right} />;
    } else {
      const source = sources.find((s) => s.id === agent.current?.source_id);
      pill = (
        <CompositePill
          left={friendlyModelName(agent, sources)}
          dot={source ? sourceAccent(source) : accent}
          right={source?.display_name ?? agent.current.source_id}
        />
      );
    }
  } else if (agent.mode === 'hub') {
    // Honest state: hub is selected but no eligible source can supply this agent
    // yet, so the next turn silently falls back to Direct. Say so plainly instead
    // of showing an empty (falsely "fine") row.
    pill = (
      <span className="flex items-center gap-1.5 text-[12px] font-medium text-gold">
        <TriangleAlert className="size-3.5 shrink-0" />
        {t('settings.models.agents.hubNoSupply')}
      </span>
    );
  }

  // Direct mode had no second line at all, which read as "nothing to see here" —
  // the truth is that this backend is still supplied by its own CLI config and the
  // Hub isn't involved. Phones only: the desktop row keeps 直连 Direct beside its
  // 接入中枢 button on one line, where a sentence has nowhere to go.
  const mobileNote =
    agent.mode === 'hub' ? null : (
      <span className="text-[11.5px] leading-relaxed text-muted">{t('settings.models.agents.directNote')}</span>
    );

  const action =
    agent.mode === 'hub' ? (
      <Button variant="secondary" size="sm" className="h-11 w-full sm:h-9 sm:w-auto" onClick={openMenu}>
        {t('settings.models.agents.modelMenu')}
        <ChevronRight className="size-3.5" />
      </Button>
    ) : (
      <Button
        variant="brand"
        size="sm"
        className="h-11 w-full sm:h-9 sm:w-auto"
        onClick={() => onConnectHub(agent)}
        disabled={connecting}
      >
        <ArrowDownToLine className="size-3.5" />
        {t('settings.models.agents.connectHub')}
      </Button>
    );

  return (
    // Mobile is three full-width tiers (design.pen M01 m01Ag*): identity + mode
    // badge, the supply line, then the action. Squeezed into one row at 390px the
    // mode chip overlapped the composite pill and the pill's model id wrapped to
    // three lines; nesting the pill under the name (the desktop shape) also left
    // it inset by the icon tile instead of spanning the row.
    //
    // The supply pill has two mount points because the desktop frame nests it
    // under the name while mobile needs it as a full-width sibling — CSS cannot
    // re-parent. It's stateless presentation and the inactive copy is
    // display:none (so out of the a11y tree), which is the cheap half of the
    // trade; the win is that the sm+ DOM stays exactly the reviewed desktop row.
    <div className="flex flex-col gap-2.5 border-b border-border px-4 py-4 last:border-b-0 sm:flex-row sm:items-center sm:gap-4 sm:px-5">
      <div className="flex min-w-0 items-center gap-2.5 sm:flex-1 sm:gap-4">
        <span className={cn('flex size-[34px] shrink-0 items-center justify-center rounded-[10px] sm:size-11', ACCENT_TILE[accent])}>
          <Icon className={cn('size-[18px] sm:size-[22px]', ACCENT_ICON[accent])} />
        </span>

        <div className="flex min-w-0 flex-1 flex-col items-start gap-2">
          {/* truncate only on phones; sm+ keeps the desktop row's wrap behaviour. */}
          <span className="truncate text-[14px] font-bold text-foreground sm:whitespace-normal sm:text-[15px] sm:font-semibold">
            {t(`settings.models.backends.${agent.backend}`, { defaultValue: agent.backend })}
          </span>
          {pill && <span className="hidden sm:block">{pill}</span>}
        </div>

        {/* The mode badge belongs on the identity line on phones. */}
        <span className="shrink-0 sm:hidden">
          <ModeChip mode={agent.mode} />
        </span>
      </div>

      <div className="sm:hidden">{pill ?? mobileNote}</div>

      <div className="flex items-center gap-2.5 sm:shrink-0">
        <span className="hidden sm:inline-flex">
          <ModeChip mode={agent.mode} />
        </span>
        {action}
      </div>
    </div>
  );
};

export const AgentCard: React.FC<{
  agents: AgentSupply[];
  sources: Source[];
  onConnectHub: (agent: AgentSupply) => void;
  onOpenMenu?: (agent: AgentSupply) => void;
  connectingBackend: string | null;
}> = ({ agents, sources, onConnectHub, onOpenMenu, connectingBackend }) => {
  const { t } = useTranslation();
  return (
    <section className="rounded-xl border border-border bg-background">
      <div className="flex items-start justify-between gap-4 border-b border-border px-4 py-4 sm:px-5">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 className="text-[15px] font-semibold text-foreground">{t('settings.models.agents.title')}</h2>
          <p className="text-[12px] leading-relaxed text-muted">{t('settings.models.agents.subtitle')}</p>
        </div>
        <Link
          to="/admin/settings/backends"
          className="inline-flex min-h-10 shrink-0 items-center gap-1 text-[13px] font-medium text-mint transition-colors hover:text-mint/80 sm:min-h-0"
        >
          {t('settings.models.agents.backendSettings')}
          <ArrowRight className="size-3.5" />
        </Link>
      </div>
      <div className="flex flex-col">
        {agents.map((agent) => (
          <AgentRow
            key={agent.backend}
            agent={agent}
            sources={sources}
            onConnectHub={onConnectHub}
            onOpenMenu={onOpenMenu}
            connecting={connectingBackend === agent.backend}
          />
        ))}
      </div>
    </section>
  );
};
