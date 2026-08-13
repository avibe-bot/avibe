import {
  ArrowLeft,
  Boxes,
  Building2,
  Cloud,
  CloudOff,
  Grid2x2,
  LoaderCircle,
  LogOut,
  Server,
  ShieldAlert,
  Users,
  WifiOff,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import clsx from 'clsx';

import { Button } from '@/components/ui/button';
import { Select } from '@/components/ui/select';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { adminLandingPath } from '@/lib/adminNavigation';

import { useOrganization } from './context';
import { OrganizationProvider } from './OrganizationProvider';
import { EmptyState, InitialsAvatar } from './components';
import { organizationSwitchDestination } from './policy';

const NAV = [
  { path: '/admin/organization/overview', key: 'overview', icon: Grid2x2 },
  { path: '/admin/organization/instances', key: 'instances', icon: Server },
  { path: '/admin/organization/members', key: 'members', icon: Users },
  { path: '/admin/organization/groups', key: 'groups', icon: Boxes },
  { path: '/admin/organization/resources', key: 'resources', icon: Building2 },
] as const;

// Resolve "back to control panel" against the current Instance capability.
function useBackToControlPanelPath(): string {
  const { capabilities } = useInstanceAuthorization();
  return adminLandingPath(capabilities.can_use_system);
}

function GateState() {
  const { t } = useTranslation();
  const { gate, signIn, retry } = useOrganization();
  const { capabilities } = useInstanceAuthorization();
  const backToControlPanelPath = useBackToControlPanelPath();
  const cloudNotConnectedPath = capabilities.can_use_system ? '/admin/remote-access' : backToControlPanelPath;
  const cloudNotConnectedLabel = capabilities.can_use_system
    ? t('organization.actions.openRemoteAccess')
    : t('organization.actions.backToControlPanel');
  if (gate === 'loading' || gate === 'reauthorizing') {
    return (
      <div className="grid min-h-[100dvh] place-items-center bg-background p-6 text-center">
        <div>
          <LoaderCircle className="mx-auto size-8 animate-spin text-mint" />
          <h1 className="mt-4 text-lg font-semibold">
            {t(gate === 'reauthorizing' ? 'organization.states.reauthorizingTitle' : 'organization.states.loadingTitle')}
          </h1>
          <p className="mt-1 text-[13px] text-muted">
            {t(gate === 'reauthorizing' ? 'organization.states.reauthorizingBody' : 'organization.states.loadingBody')}
          </p>
        </div>
      </div>
    );
  }
  const config = {
    cloud_not_connected: {
      icon: CloudOff,
      title: t('organization.states.notConnectedTitle'),
      body: t('organization.states.notConnectedBody'),
      action: <Button asChild variant="brand"><Link to={cloudNotConnectedPath}>{cloudNotConnectedLabel}</Link></Button>,
    },
    authorization_required: {
      icon: Cloud,
      title: t('organization.states.signInTitle'),
      body: t('organization.states.signInBody'),
      action: <Button variant="brand" onClick={() => void signIn()}>{t('organization.actions.signIn')}</Button>,
    },
    subject_mismatch: {
      icon: ShieldAlert,
      title: t('organization.states.mismatchTitle'),
      body: t('organization.states.mismatchBody'),
      action: <Button variant="brand" onClick={() => void signIn()}>{t('organization.actions.signInAgain')}</Button>,
    },
    unreachable: {
      icon: WifiOff,
      title: t('organization.states.unreachableTitle'),
      body: t('organization.states.unreachableBody'),
      action: <Button variant="outline" onClick={() => void retry()}>{t('common.retry')}</Button>,
    },
    revoked: {
      icon: ShieldAlert,
      title: t('organization.states.revokedTitle'),
      body: t('organization.states.revokedBody'),
      action: <Button variant="brand" onClick={() => void signIn()}>{t('organization.actions.signIn')}</Button>,
    },
    error: {
      icon: ShieldAlert,
      title: t('organization.states.errorTitle'),
      body: t('organization.errors.generic'),
      action: <Button variant="outline" onClick={() => void retry()}>{t('common.retry')}</Button>,
    },
  } as const;
  const state = config[gate as keyof typeof config] ?? config.error;
  const Icon = state.icon;
  return (
    <div className="min-h-[100dvh] bg-background p-4 md:p-8">
      <Link to={backToControlPanelPath} className="inline-flex items-center gap-2 text-[13px] text-muted hover:text-foreground">
        <ArrowLeft className="size-4" />
        {t('organization.actions.backToControlPanel')}
      </Link>
      <div className="mx-auto mt-20 max-w-xl">
        <EmptyState
          title={state.title}
          body={state.body}
          action={state.action}
        />
        <Icon className="sr-only" />
      </div>
    </div>
  );
}

function OrganizationNavbar() {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const {
    organizations,
    selectedOrganizationId,
    selectOrganization,
    detail,
    session,
    signOut,
  } = useOrganization();
  const switchOrganization = async (organizationId: string) => {
    const destination = organizationSwitchDestination(location.pathname);
    if (destination) navigate(destination, { replace: true });
    await selectOrganization(organizationId);
  };
  return (
    <header className="border-b border-border bg-background/95 px-4 pt-5 backdrop-blur md:px-10 md:pt-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex min-w-0 items-start gap-3">
          <InitialsAvatar value={detail?.organization.name ?? ''} tone="violet" />
          <div className="min-w-0">
            <h1 className="truncate text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground">
              {detail?.organization.name}
            </h1>
            <p className="mt-1 text-[14px] leading-[1.55] text-muted">
              {t(`organization.roles.${detail?.membership.role ?? 'member'}`)}
              {organizations.length > 1 ? ` · ${t('organization.sidebar.organizationCount', { count: organizations.length })}` : ''}
            </p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {organizations.length > 1 ? (
            <Select
              className="w-auto min-w-[150px]"
              value={selectedOrganizationId ?? ''}
              aria-label={t('organization.sidebar.switchOrganization')}
              onChange={(event) => void switchOrganization(event.target.value)}
            >
              {organizations.map((organization) => (
                <option key={organization.id} value={organization.id}>{organization.name}</option>
              ))}
            </Select>
          ) : null}
          <div className="hidden items-center gap-2 sm:flex">
            <div className="rounded-lg border border-mint/30 bg-mint/10 px-3 py-2">
              <div className="flex items-center gap-2 text-[12px] font-semibold">
                <span className="size-2 rounded-full bg-mint shadow-[0_0_8px_rgba(91,255,160,0.8)]" />
                {t('organization.sidebar.cloudConnected')}
                <span className="font-mono text-[10px] font-normal text-muted">
                  · {t('organization.sidebar.sessionRemaining', { minutes: Math.max(1, Math.ceil((session?.expires_in ?? 0) / 60)) })}
                </span>
              </div>
            </div>
            <InitialsAvatar value={session?.user.email ?? ''} className="size-8 rounded-full" />
          </div>
          <Button
            variant="ghost"
            size="sm"
            aria-label={t('organization.actions.signOut')}
            onClick={() => void signOut()}
          >
            <LogOut className="size-4" />
            <span>{t('organization.actions.signOut')}</span>
          </Button>
        </div>
      </div>
      <nav className="mt-6 -mb-px overflow-x-auto" aria-label={t('organization.sidebar.section')}>
        <div className="flex min-w-max gap-1">
          {NAV.map((item) => {
            const Icon = item.icon;
            return (
              <NavLink key={item.path} to={item.path} className={({ isActive }) => clsx(
                'inline-flex h-11 shrink-0 items-center gap-2 border-b-2 px-4 text-[13px] transition-colors',
                isActive ? 'border-mint font-semibold text-foreground' : 'border-transparent font-medium text-muted hover:border-border-strong hover:text-foreground',
              )}>
                <Icon className="size-4" />
                {t(`organization.nav.${item.key}`)}
              </NavLink>
            );
          })}
        </div>
      </nav>
    </header>
  );
}

function ConnectedShell() {
  const { t } = useTranslation();
  const location = useLocation();
  const { organizations, detail } = useOrganization();
  if (organizations.length === 0 || !detail) {
    return (
      <div className="min-h-[100dvh] bg-background p-6">
        <EmptyState title={t('organization.states.emptyTitle')} body={t('organization.states.emptyBody')} />
      </div>
    );
  }
  return (
    <div className="min-h-[100dvh] bg-background text-foreground">
      <OrganizationNavbar />
      <main key={location.pathname} className="mx-auto w-full max-w-[1560px] px-4 py-5 md:px-8 md:py-7">
        <Outlet />
      </main>
    </div>
  );
}

function OrganizationShellInner() {
  const { gate } = useOrganization();
  return gate === 'connected' ? <ConnectedShell /> : <GateState />;
}

export function OrganizationShell() {
  return (
    <OrganizationProvider>
      <OrganizationShellInner />
    </OrganizationProvider>
  );
}
