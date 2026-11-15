// Approved 2026-11-14 (owner): the iOS App's account and instance pages as one sheet. The pages
// have shipped (ui/src/mobile, drawn by the frames); what stays here is what the web cannot render:
// the native sheet around them (UISheetPresentationController), a gesture drawn on a frame, and
// the native screen shown when an instance fails to load.
// Plan: docs/plans/2026-11-13-ios-instance-pages.md. Static: no handlers, no effects.
import { createContext, useContext, type ReactNode } from 'react';
import { WifiOff } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Workbench } from '@/components/Workbench';
import { cn } from '@/lib/utils';
import { phoneShell } from '../frames/workbench/fixtures';

export type SheetLocale = 'zh' | 'en';

const Locale = createContext<SheetLocale>('zh');
export function SheetLanguage({ lang, children }: { lang: SheetLocale; children: ReactNode }) {
  return <Locale.Provider value={lang}><div lang={lang} className="h-full">{children}</div></Locale.Provider>;
}
function useCopy() {
  const lang = useContext(Locale);
  return (zh: string, en: string) => (lang === 'en' ? en : zh);
}

const home = phoneShell('/', <Workbench />);

/** The instance underneath, shrunk and dimmed the way iOS presents a large sheet. */
export function Underneath({ children }: { children?: ReactNode }) {
  return (
    <div className="relative h-full overflow-hidden bg-surface-3">
      <div className="h-full origin-top scale-95 overflow-hidden rounded-3xl opacity-80">
        <div className="relative h-full">{home()}</div>
      </div>
      {children}
    </div>
  );
}

/**
 * The native sheet's card (drawn here), holding a shipped page that draws its own header.
 * `atEnd` draws the page scrolled to its end.
 */
export function SheetPanel({ children, className, atEnd = false }: { children: ReactNode; className?: string; atEnd?: boolean }) {
  return (
    <section
      aria-modal="true"
      className={cn('absolute inset-x-0 bottom-0 top-[calc(env(safe-area-inset-top)+0.75rem)] flex flex-col overflow-hidden rounded-t-3xl border-t border-edge bg-background shadow-elevation-3-up', className)}
    >
      <span aria-hidden="true" className="mx-auto mt-2 h-1.5 w-10 shrink-0 rounded-full bg-muted/40" />
      <div className={cn('min-h-0 flex-1', atEnd ? 'flex flex-col justify-end overflow-hidden [&>*]:shrink-0' : 'overflow-y-auto')}>{children}</div>
    </section>
  );
}

/** The native sheet over the shipped Home, holding a shipped page. */
export function NativeSheet({ children, className, atEnd }: { children: ReactNode; className?: string; atEnd?: boolean }) {
  return (
    <Underneath>
      <SheetPanel className={className} atEnd={atEnd}>{children}</SheetPanel>
    </Underneath>
  );
}

/** A labelled gesture, drawn on the frame: what the finger does and where it leads. */
export function GestureNote({ icon, zh, en, className }: { icon: ReactNode; zh: string; en: string; className?: string }) {
  const t = useCopy();
  return (
    <div aria-hidden="true" className={cn('absolute z-10 flex items-center gap-2 rounded-full bg-foreground px-3 py-1.5 text-xs font-medium text-background shadow-elevation-2', className)}>
      {icon}{t(zh, en)}
    </div>
  );
}

/** The native screen when an instance fails to load, in the same words as the sheet. */
export function RuntimeFailed({ instance }: { instance: { name: string; host: string } }) {
  const t = useCopy();
  return (
    <div className="flex h-full flex-col items-center justify-center bg-background px-8 pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] text-center text-foreground">
      <WifiOff className="mb-5 size-9 text-gold-ink" />
      <h1 className="text-xl font-semibold leading-tight">{t(`暂时连不上 ${instance.name}`, `Can’t reach ${instance.name}`)}</h1>
      <p className="mt-3 text-sm leading-6 text-muted">{t('运行它的电脑可能离线了，或者这台手机的网络有问题。', 'The computer running it may be offline, or this phone’s connection has a problem.')}</p>
      <p className="mt-2 break-all text-xs text-muted">{instance.host}</p>
      <div className="mt-8 flex w-full max-w-xs flex-col gap-3">
        <Button variant="brand" size="lg" className="rounded-full">{t('重试', 'Try again')}</Button>
        <Button variant="outline" size="lg" className="rounded-full">{t('其他实例', 'Other instances')}</Button>
      </div>
    </div>
  );
}
