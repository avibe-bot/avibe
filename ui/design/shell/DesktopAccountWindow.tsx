import { ArrowLeft } from 'lucide-react';
import type { CSSProperties, ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';

// AccountApp's native chrome: 34px titlebar + 60px Workbench header. Keep the
// full window in Welcome frames so its height variants see the native viewport.
export function DesktopAccountWindow({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  return <div className="flex h-dvh flex-col bg-background text-foreground" style={{ '--shell-titlebar-inset': '34px' } as CSSProperties}>
    <div aria-hidden="true" className="h-[var(--shell-titlebar-inset)] shrink-0 bg-surface" />
    <header className="flex shrink-0 items-center px-8 py-3"><Button variant="ghost" size="sm"><ArrowLeft className="size-4" />{t('desktopAccount.backWorkbench')}</Button></header>
    <main className="flex min-h-0 flex-1 flex-col overflow-y-auto px-10 pb-5">{children}</main>
  </div>;
}
