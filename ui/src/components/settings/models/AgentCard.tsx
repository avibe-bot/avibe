// The Agent band (frame 01r): one row per backend — icon + name + menu-kind
// badge, the current supply as a composite pill (hub only), the supply-mode
// chip, and the row action (模型菜单 for hub / 接入中枢 for direct). 模型菜单
// links into L5's drawers; until L5 lands (MODEL_MENUS_ENABLED) it explains
// itself rather than opening a missing surface.
import * as React from 'react';
import { Link } from 'react-router-dom';
import { ArrowDownToLine, ArrowRight, ChevronRight } from 'lucide-react';
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
  }

  return (
    <div className="flex items-center gap-4 border-b border-border px-5 py-4 last:border-b-0">
      <span className={cn('flex size-11 shrink-0 items-center justify-center rounded-[10px]', ACCENT_TILE[accent])}>
        <Icon size={22} className={ACCENT_ICON[accent]} />
      </span>

      <div className="flex min-w-0 flex-1 flex-col items-start gap-2">
        <span className="text-[15px] font-semibold text-foreground">
          {t(`settings.models.backends.${agent.backend}`, { defaultValue: agent.backend })}
        </span>
        {pill}
      </div>

      <div className="flex shrink-0 items-center gap-2.5">
        <ModeChip mode={agent.mode} />
        {agent.mode === 'hub' ? (
          <Button variant="secondary" size="sm" onClick={openMenu}>
            {t('settings.models.agents.modelMenu')}
            <ChevronRight className="size-3.5" />
          </Button>
        ) : (
          <Button variant="brand" size="sm" onClick={() => onConnectHub(agent)} disabled={connecting}>
            <ArrowDownToLine className="size-3.5" />
            {t('settings.models.agents.connectHub')}
          </Button>
        )}
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
      <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 className="text-[15px] font-semibold text-foreground">{t('settings.models.agents.title')}</h2>
          <p className="text-[12px] leading-relaxed text-muted">{t('settings.models.agents.subtitle')}</p>
        </div>
        <Link
          to="/admin/settings/backends"
          className="inline-flex shrink-0 items-center gap-1 text-[13px] font-medium text-mint transition-colors hover:text-mint/80"
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
