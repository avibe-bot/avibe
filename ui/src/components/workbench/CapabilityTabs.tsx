import { useEffect, useRef } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Activity, Bot, KeyRound, WandSparkles } from 'lucide-react';
import clsx from 'clsx';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import { canUseHarness } from '../../lib/remoteAuth';

const TABS = [
  { to: '/agents', icon: Bot, key: 'workbench.modules.agents.title' },
  { to: '/skills', icon: WandSparkles, key: 'workbench.modules.skills.title' },
  { to: '/harness', icon: Activity, key: 'workbench.modules.harness.title' },
  { to: '/vaults', icon: KeyRound, key: 'workbench.modules.vaults.title' },
] as const;

// Mobile-only capability sub-tab strip. On desktop these capabilities live in
// the WorkbenchSidebar; on mobile the 能力 (Capabilities) bottom tab lands on
// a capability page and this strip switches between Agents / Skills / Harness /
// Vaults. Design: design.pen `wdtCs` sub-tabs.
export const CapabilityTabs: React.FC = () => {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const activeTabRef = useRef<HTMLAnchorElement | null>(null);
  const { capabilities, remote } = useInstanceAuthorization();
  const tabs = TABS.filter(({ to }) => {
    // Harness is the exception to the owner check: its page opens entirely on
    // local-only routes (`/api/harness/bootstrap`), so `can_manage_agents`
    // (which stays true for a remote owner) would navigate a remote owner into
    // the AppShell local-only redirect. The trusted-local `canUseHarness`
    // predicate keeps the tab local-only, matching the desktop sidebar.
    if (to === '/harness') return capabilities.can_manage_agents && canUseHarness({ remote });
    if (to === '/agents') return capabilities.can_manage_agents;
    if (to === '/skills') return capabilities.can_use_skills;
    if (to === '/vaults') return capabilities.can_use_vault_secrets;
    return false;
  });

  useEffect(() => {
    activeTabRef.current?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }, [pathname]);

  return (
    <div className="-mx-4 flex gap-2 overflow-x-auto px-4 pb-0.5 md:hidden">
      {tabs.map(({ to, icon: Icon, key }) => {
        const active = pathname.startsWith(to);
        return (
          <NavLink
            key={to}
            ref={active ? activeTabRef : undefined}
            to={to}
            className={clsx(
              'flex shrink-0 items-center gap-1.5 rounded-full border px-3.5 py-1.5 text-[12.5px] font-medium transition',
              active ? 'border-mint/40 bg-mint-soft text-mint' : 'border-border-strong text-muted',
            )}
          >
            <Icon className="size-3.5" />
            <span>{t(key)}</span>
          </NavLink>
        );
      })}
    </div>
  );
};
