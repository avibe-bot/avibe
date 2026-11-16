import { Route, Routes } from 'react-router-dom';
import { AppShell, AppShellFooter } from '@/components/AppShell';
import { VersionBadge } from '@/components/VersionBadge';
import { Workbench } from '@/components/Workbench';
import { DesktopAccountRowView } from '@/desktop/DesktopAccountRowView';
import { InstanceSwitcherView } from '@/desktop/InstanceSwitcherView';
import { AccountSecondFactorView } from '@/desktop/AccountSecondFactorView';
import { AccountSignOutView } from '@/desktop/AccountSignOutView';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { AccountDetailsView } from '@/desktop/AccountDetailsView';
import { AddInstanceView } from '@/desktop/AddInstanceView';
import { AccountLoginView } from '@/desktop/AccountLoginView';
import { frames } from '../../frame';
import { DesktopInstances } from '../../kit/desktop-instances';
import { accountRowViewProps, desktopInstanceFrames, loginViewProps, switcherViewProps, factorViewProps, profileViewProps } from './instances-fixtures';

const designFrames = frames(desktopInstanceFrames.map(({ fixture, ...frame }) => {
  const footer = accountRowViewProps(fixture);
  if (footer) return {
    ...frame, status: 'shipped',
    render: () => <Routes>
      <Route element={<AppShell footer={({ tools }) => <AppShellFooter tools={tools}>
        <DesktopAccountRowView {...footer} localUpdater={<VersionBadge openUpward />} />
      </AppShellFooter>} />}>
        <Route path="/" element={<Workbench />} />
      </Route>
    </Routes>,
  };
  if (fixture.state === 'sign-out-error') return { ...frame, status: 'shipped', render: () => <div className="h-full overflow-y-auto bg-background px-10 py-5 text-foreground"><AccountSignOutView signingOut={false} errorMessage={(fixture.lang === 'en' ? en : zh).desktopClient.errors.request} onBack={() => {}} onRetry={() => {}} /></div> };
  const profile = profileViewProps(fixture);
  if (profile) return { ...frame, status: 'shipped', render: () => <div className="h-full overflow-y-auto bg-background px-10 py-5 text-foreground"><AccountDetailsView {...profile} /></div> };
  if (fixture.state === 'add') return { ...frame, status: 'shipped', render: () => <div className="h-full overflow-y-auto bg-background px-10 py-5 text-foreground"><AddInstanceView onBack={() => {}} onRefresh={() => {}} busy={false} /></div> };
  const switcher = switcherViewProps(fixture);
  if (switcher) return { ...frame, status: 'shipped', render: () => <div className="h-full overflow-y-auto bg-background px-10 py-5 text-foreground"><InstanceSwitcherView {...switcher} /></div> };
  const factor = factorViewProps(fixture);
  if (factor) return { ...frame, status: 'shipped', render: () => <div className="flex h-full flex-col overflow-y-auto bg-background px-10 py-5 text-foreground"><AccountSecondFactorView {...factor} /></div> };
  const login = loginViewProps(fixture);
  return login ? {
    ...frame, status: 'shipped', title: `${frame.title} · product card`,
    // Card-only stage: page chrome and native lifecycle remain owned by AccountApp.
    render: () => <div className="flex h-full flex-col overflow-y-auto bg-background px-10 py-5 text-foreground"><AccountLoginView {...login} /></div>,
  } : { ...frame, render: () => <DesktopInstances fixture={fixture} /> };
}));

export default designFrames;
