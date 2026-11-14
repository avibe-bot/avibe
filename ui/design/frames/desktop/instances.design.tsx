import { Route, Routes } from 'react-router-dom';
import { AppShell, AppShellFooter } from '@/components/AppShell';
import { VersionBadge } from '@/components/VersionBadge';
import { Workbench } from '@/components/Workbench';
import { DesktopAccountRowView } from '@/desktop/DesktopAccountRowView';
import { AccountLoginView } from '@/desktop/AccountLoginView';
import { frames } from '../../frame';
import { DesktopInstances } from '../../kit/desktop-instances';
import { accountRowViewProps, desktopInstanceFrames, loginViewProps } from './instances-fixtures';

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
  const login = loginViewProps(fixture);
  return login ? {
    ...frame, status: 'shipped', title: `${frame.title} · product card`,
    // Card-only stage: page chrome and native lifecycle remain owned by AccountApp.
    render: () => <div className="flex h-full flex-col overflow-y-auto bg-background px-10 py-5 text-foreground"><AccountLoginView {...login} /></div>,
  } : { ...frame, render: () => <DesktopInstances fixture={fixture} /> };
}));

export default designFrames;
