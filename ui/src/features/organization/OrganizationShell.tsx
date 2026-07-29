import { useState } from 'react';
import {
  ArrowLeft,
  Boxes,
  Building2,
  Cloud,
  CloudOff,
  Grid2x2,
  LoaderCircle,
  LogOut,
  Menu,
  Server,
  ShieldAlert,
  Users,
  WifiOff,
  X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom';
import clsx from 'clsx';

import { Button } from '@/components/ui/button';
import { Select } from '@/components/ui/select';

import { OrganizationProvider, useOrganization } from './context';
import { EmptyState, InitialsAvatar } from './components';

const NAV = [
  { path: '/admin/organization/overview', key: 'overview', icon: Grid2x2 },
  { path: '/admin/organization/instances', key: 'instances', icon: Server },
  { path: '/admin/organization/members', key: 'members', icon: Users },
  { path: '/admin/organization/groups', key: 'groups', icon: Boxes },
  { path: '/admin/organization/resources', key: 'resources', icon: Building2 },
] as const;

function GateState() {
  const { t } = useTranslation();
  const { gate, signIn, retry } = useOrganization();
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
      action: <Button asChild variant="brand"><Link to="/admin/remote-access">{t('organization.actions.openRemoteAccess')}</Link></Button>,
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
      <Link to="/admin/dashboard" className="inline-flex items-center gap-2 text-[13px] text-muted hover:text-foreground">
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

function OrganizationSidebar({ mobile = false, onNavigate }: { mobile?: boolean; onNavigate?: () => void }) {
  const { t } = useTranslation();
  const {
    organizations,
    selectedOrganizationId,
    selectOrganization,
    detail,
    session,
    signOut,
  } = useOrganization();
  return (
    <div className="flex h-full flex-col bg-surface px-4 py-5">
      <Link to="/admin/dashboard" className="flex items-center gap-2 text-[13px] text-muted hover:text-foreground" onClick={onNavigate}>
        <ArrowLeft className="size-4" />
        {t('organization.actions.backToControlPanel')}
      </Link>
      <div className="mt-6 rounded-lg border border-border bg-card p-3">
        <div className="flex items-center gap-3">
          <InitialsAvatar value={detail?.organization.name ?? ''} tone="violet" />
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-semibold">{detail?.organization.name}</div>
            <div className="mt-0.5 text-[11px] text-muted">
              {t(`organization.roles.${detail?.membership.role ?? 'member'}`)}
              {organizations.length > 1 ? ` · ${t('organization.sidebar.organizationCount', { count: organizations.length })}` : ''}
            </div>
          </div>
        </div>
        {organizations.length > 1 ? (
          <Select
            className="mt-3"
            value={selectedOrganizationId ?? ''}
            aria-label={t('organization.sidebar.switchOrganization')}
            onChange={(event) => void selectOrganization(event.target.value)}
          >
            {organizations.map((organization) => (
              <option key={organization.id} value={organization.id}>{organization.name}</option>
            ))}
          </Select>
        ) : null}
      </div>
      <div className="mt-5 px-1 font-mono text-[10px] font-bold uppercase text-muted">
        {t('organization.sidebar.section')}
      </div>
      <nav className="mt-3 flex flex-col gap-1">
        {NAV.map((item) => {
          const Icon = item.icon;
          return (
            <NavLink
              key={item.path}
              to={item.path}
              onClick={onNavigate}
              className={({ isActive }) => clsx(
                'flex items-center gap-3 rounded-lg border px-3 py-2.5 text-[13px] font-medium transition-colors',
                isActive
                  ? 'border-mint/30 bg-mint/10 text-foreground'
                  : 'border-transparent text-muted hover:bg-foreground/[0.04] hover:text-foreground',
              )}
            >
              <Icon className="size-4" />
              {t(`organization.nav.${item.key}`)}
            </NavLink>
          );
        })}
      </nav>
      <div className="mt-auto pt-6">
        <div className="rounded-lg border border-mint/30 bg-mint/10 p-3">
          <div className="flex items-center gap-2 text-[12px] font-semibold">
            <span className="size-2 rounded-full bg-mint shadow-[0_0_8px_rgba(91,255,160,0.8)]" />
            {t('organization.sidebar.cloudConnected')}
          </div>
          <div className="mt-1 font-mono text-[10px] text-muted">
            {t('organization.sidebar.sessionRemaining', { minutes: Math.max(1, Math.ceil((session?.expires_in ?? 0) / 60)) })}
          </div>
        </div>
        <div className="mt-3 flex items-center gap-2">
          <InitialsAvatar value={session?.user.email ?? ''} className="size-8 rounded-full" />
          <div className="min-w-0 flex-1 truncate text-[11px] text-muted">{session?.user.email}</div>
          <Button
            variant="ghost"
            size="icon"
            aria-label={t('organization.actions.signOut')}
            onClick={() => void signOut()}
          >
            <LogOut className="size-4" />
          </Button>
        </div>
      </div>
      {mobile ? <div className="h-[env(safe-area-inset-bottom)]" /> : null}
    </div>
  );
}

function ConnectedShell() {
  const { t } = useTranslation();
  const location = useLocation();
  const { organizations, detail } = useOrganization();
  const [drawerOpen, setDrawerOpen] = useState(false);
  if (organizations.length === 0 || !detail) {
    return (
      <div className="min-h-[100dvh] bg-background p-6">
        <EmptyState title={t('organization.states.emptyTitle')} body={t('organization.states.emptyBody')} />
      </div>
    );
  }
  return (
    <div className="h-[100dvh] overflow-y-auto bg-background text-foreground md:grid md:grid-cols-[260px_minmax(0,1fr)] md:overflow-hidden">
      <aside className="sticky top-0 hidden h-[100dvh] border-r border-border md:block">
        <OrganizationSidebar />
      </aside>
      <div className="min-w-0 md:h-[100dvh] md:overflow-y-auto">
        <header className="sticky top-0 z-30 flex h-14 items-center justify-between border-b border-border bg-background/95 px-4 backdrop-blur md:hidden">
          <Button variant="ghost" size="icon" aria-label={t('organization.sidebar.openMenu')} onClick={() => setDrawerOpen(true)}>
            <Menu className="size-5" />
          </Button>
          <div className="truncate text-[13px] font-semibold">{detail.organization.name}</div>
          <div className="size-9" />
        </header>
        <main key={location.pathname} className="mx-auto w-full max-w-[1560px] px-4 py-5 md:px-8 md:py-7">
          <Outlet />
        </main>
      </div>
      {drawerOpen ? (
        <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-modal="true">
          <button className="absolute inset-0 bg-background/70 backdrop-blur-sm" aria-label={t('common.close')} onClick={() => setDrawerOpen(false)} />
          <aside className="absolute inset-y-0 left-0 w-[min(86vw,300px)] border-r border-border shadow-2xl">
            <Button variant="ghost" size="icon" className="absolute right-3 top-3 z-10" aria-label={t('common.close')} onClick={() => setDrawerOpen(false)}>
              <X className="size-4" />
            </Button>
            <OrganizationSidebar mobile onNavigate={() => setDrawerOpen(false)} />
          </aside>
        </div>
      ) : null}
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
