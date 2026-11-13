import { AccountLoginView } from '@/desktop/AccountLoginView';
import { frames } from '../../frame';
import { DesktopInstances } from '../../kit/desktop-instances';
import { desktopInstanceFrames, loginViewProps } from './instances-fixtures';

const designFrames = frames(desktopInstanceFrames.map(({ fixture, ...frame }) => {
  const login = loginViewProps(fixture);
  return login ? {
    ...frame, status: 'shipped', title: `${frame.title} · product card`,
    // Card-only stage: page chrome and native lifecycle remain owned by AccountApp.
    render: () => <div className="flex h-full flex-col overflow-y-auto bg-background px-10 py-5 text-foreground"><AccountLoginView {...login} /></div>,
  } : { ...frame, render: () => <DesktopInstances fixture={fixture} /> };
}));

export default designFrames;
